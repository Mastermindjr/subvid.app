#!/usr/bin/env python3
"""subvid-server — local engine that exposes the faster-whisper pipeline to the web UI.

Run:  python subvid_server.py            (listens on http://127.0.0.1:8787)

The web app auto-detects this server and, when present, offers:
  - single-file transcription on your GPU (upload → segments for the editor)
  - batch processing of server-side folders with per-file progress

Security: clients can always upload media for processing (web-local and
client-side batch modes). The endpoints that browse THIS machine's
filesystem ("server mode" in the web UI: /api/browse, /api/pick-folder,
server-path batch jobs) additionally require an access token whenever the
server is bound to a non-loopback address (--token / SUBVID_TOKEN /
[server].token in config.toml; one is generated and printed if none is
configured). To use it over the internet put it behind HTTPS (see the
[server] notes in config.toml — Tailscale or a Caddy reverse proxy) and
consider --fs-root to fence in filesystem access.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from subvid_batch import (
    AUDIO_EXTS,
    NLLB_CODES,
    VIDEO_EXTS,
    JobCancelled,
    Transcriber,
    Translator,
    _enable_windows_cuda_dlls,
    _install_terminate_guards,
    build_parser,
    collect_inputs,
    ct2_cuda_available,
    dedupe_output_collisions,
    finalize_args,
    load_config,
    mlx_available,
    process_file,
    vulkan_available,
)

PICK_FOLDER_LOCK = threading.Lock()

CONFIG = load_config()
SERVER_CFG = CONFIG.get("server", {})
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS

# ── Security ──
# AUTH["token"]: when set (always when binding a non-loopback address), the
# server-filesystem endpoints (see _needs_token) require it. FS_ROOTS:
# optional allowlist of directories the API may browse/process; empty =
# whole filesystem (local trust).
AUTH = {"token": ""}
FS_ROOTS: list[Path] = []
MAX_UPLOAD_MB = max(1, int(SERVER_CFG.get("max_upload_mb", 4096)))
JOB_TTL_SECONDS = 3600

# Origins allowed to call the API from another origin: the astro dev server,
# the hosted site (browser-side engine detection), plus config extras. The
# UI served by this same server is same-origin and needs no CORS at all.
_LOCAL_ORIGIN_RE = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
ALLOWED_ORIGINS = ["https://subvid.app"] + [
    str(o) for o in SERVER_CFG.get("allowed_origins", []) or []
]


def _origin_allowed(origin: str, request: Request) -> bool:
    if origin in ALLOWED_ORIGINS or re.fullmatch(_LOCAL_ORIGIN_RE, origin):
        return True
    # Same-origin: the page was served by this engine (possibly behind an
    # https reverse proxy, so compare host:port only).
    return origin.split("://", 1)[-1] == request.headers.get("host", "")


def _within_roots(path: Path) -> bool:
    if not FS_ROOTS:
        return True
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or resolved.is_relative_to(root) for root in FS_ROOTS)


def require_in_roots(path: Path) -> None:
    if not _within_roots(path):
        raise HTTPException(403, "path is outside the allowed roots (--fs-root)")


app = FastAPI(title="subvid local engine")


def _needs_token(path: str, method: str) -> bool:
    """Only the endpoints that reach the server's own filesystem ("server
    mode" in the web UI) require the token. Uploads, translation and job
    polling are open to any client, like the web-local mode."""
    if path in ("/api/browse", "/api/pick-folder"):
        return True
    return path == "/api/jobs" and method == "POST"


@app.middleware("http")
async def security_gate(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api"):
        # Refuse requests scripted by web pages from unknown origins, so a
        # malicious site open in a browser cannot drive the API (drive-by).
        origin = request.headers.get("origin", "")
        if origin and not _origin_allowed(origin, request):
            return JSONResponse({"detail": "cross-origin request blocked"}, 403)
        token = AUTH["token"]
        if token and _needs_token(path, request.method):
            auth = request.headers.get("authorization", "")
            supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            supplied = supplied or request.headers.get("x-subvid-token", "")
            if not secrets.compare_digest(supplied, token):
                await asyncio.sleep(0.3)  # slow down token guessing
                return JSONResponse({"detail": "invalid or missing token"}, 401)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if not path.startswith("/api"):
        response.headers.setdefault("X-Frame-Options", "DENY")
    return response


# Added after security_gate so CORS runs outermost and cross-origin callers
# can read auth errors (the token prompt in the web UI relies on seeing 401).
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=_LOCAL_ORIGIN_RE,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
TRANSCRIBERS: dict[tuple, Transcriber] = {}
TRANSCRIBERS_LOCK = threading.Lock()
TRANSLATORS: dict[tuple, Translator] = {}


def get_translator(args: argparse.Namespace) -> Translator | None:
    if not args.to_langs:
        return None
    key = (args.translation_model, args.device)
    with TRANSCRIBERS_LOCK:
        if key not in TRANSLATORS:
            TRANSLATORS[key] = Translator(*key)
        return TRANSLATORS[key]


# The only request options a client may override; anything else is dropped so
# remote input can never reach arbitrary Namespace attributes (setattr below).
ALLOWED_JOB_OPTIONS = {
    "mode", "model", "language", "to_langs", "translation_model", "output_dir",
    "recursive", "word_animation", "no_vad", "vad_threshold", "jobs",
    # Subtitle style (mirrors the --style/--sub-* CLI flags).
    "style", "sub_font", "sub_size", "sub_color", "sub_bold", "sub_italic",
    "sub_align", "sub_position", "sub_bg", "sub_bg_color", "sub_bg_opacity",
    "sub_outline", "sub_highlight_color",
}


def make_args(options: dict | None = None) -> argparse.Namespace:
    """Build a Namespace with config.toml defaults, then apply request options."""
    args = build_parser(CONFIG).parse_args(["__dummy__"])
    args.inputs = []
    # Apply options BEFORE finalize_args so it resolves style/translations
    # from them exactly like it does for command-line flags. Only the VAD
    # pair needs post-processing (the config may disable VAD entirely).
    vad_overrides: dict = {}
    for key, value in (options or {}).items():
        attr = key.replace("-", "_")
        if attr not in ALLOWED_JOB_OPTIONS or value is None or value == "":
            continue
        if attr in ("vad_threshold", "no_vad"):
            vad_overrides[attr] = value
        elif hasattr(args, attr):
            setattr(args, attr, value)
    finalize_args(args, CONFIG)
    if vad_overrides.get("no_vad"):
        args.vad_options = None
    elif "vad_threshold" in vad_overrides:
        # Re-enable VAD even when the config disables it by default: the
        # UI sends a threshold only when its VAD checkbox is on.
        threshold = max(0.05, min(0.95, float(vad_overrides["vad_threshold"])))
        if args.vad_options:
            args.vad_options["threshold"] = threshold
        else:
            vad_cfg = CONFIG.get("vad", {})
            args.vad_options = {
                "threshold": threshold,
                "min_silence_duration_ms": int(vad_cfg.get("min_silence_ms", 2000)),
                "speech_pad_ms": int(vad_cfg.get("speech_pad_ms", 400)),
            }
    if not args.language or args.language == "auto":
        args.language = None
    args.jobs = max(1, min(8, int(args.jobs)))
    return args


def get_transcriber(args: argparse.Namespace) -> Transcriber:
    key = (args.model, args.device, args.compute_type)
    with TRANSCRIBERS_LOCK:
        if key not in TRANSCRIBERS:
            TRANSCRIBERS[key] = Transcriber(*key)
        return TRANSCRIBERS[key]


def get_job_or_404(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


def prune_jobs() -> None:
    """Drop finished jobs after a while so JOBS cannot grow forever."""
    now = time.time()
    with JOBS_LOCK:
        for job_id, job in list(JOBS.items()):
            if job["status"] == "running":
                continue
            if now - (job.get("finishedAt") or job["created"]) > JOB_TTL_SECONDS:
                for key in ("_workdir", "_outdir"):
                    if job.get(key):
                        shutil.rmtree(job[key], ignore_errors=True)
                del JOBS[job_id]


@app.get("/api/health")
def health() -> dict:
    if mlx_available():
        device = "mlx"
    elif ct2_cuda_available():
        device = "cuda"
    elif vulkan_available():
        device = "vulkan"
    else:
        device = "cpu"
    return {
        "ok": True,
        "engine": "subvid",
        "device": device,
        "model": CONFIG.get("general", {}).get("model", "large-v3-turbo"),
        # Whether "server mode" (browsing this machine's folders) needs a token.
        "auth": bool(AUTH["token"]),
    }


@app.get("/api/browse")
def browse(path: str = "") -> dict:
    if not path:
        if FS_ROOTS:
            entries = [{"name": str(r), "path": str(r)} for r in FS_ROOTS]
            return {"path": "", "parent": None, "dirs": entries, "files": []}
        entries = [{"name": str(Path.home()), "path": str(Path.home())}]
        if os.name == "nt":
            try:
                entries += [{"name": d, "path": d} for d in os.listdrives()]
            except (OSError, AttributeError):  # listdrives needs Python 3.12+
                pass
        else:
            entries.append({"name": "/", "path": "/"})
        return {"path": "", "parent": None, "dirs": entries, "files": []}

    p = Path(path)
    require_in_roots(p)
    if not p.is_dir():
        raise HTTPException(404, "not a directory")
    dirs: list[dict] = []
    files: list[dict] = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith((".", "$")):
                continue
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
                elif child.suffix.lower() in MEDIA_EXTS:
                    files.append({"name": child.name, "path": str(child), "size": child.stat().st_size})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, "permission denied")
    parent = str(p.parent) if p.parent != p and _within_roots(p.parent) else ""
    return {"path": str(p), "parent": parent, "dirs": dirs, "files": files}


@app.post("/api/pick-folder")
def pick_folder() -> dict:
    """Open a native folder picker on the machine running the engine and
    return the chosen path ("" when cancelled). Note: the dialog pops up on
    the engine machine, which is also where the files live."""
    if not PICK_FOLDER_LOCK.acquire(blocking=False):
        raise HTTPException(409, "a folder picker is already open")
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            path = filedialog.askdirectory(title="subvid — pick a folder") or ""
        finally:
            root.destroy()
        return {"path": path}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"could not open the folder picker: {e}")
    finally:
        PICK_FOLDER_LOCK.release()


@app.post("/api/translate")
def translate_texts(payload: dict) -> dict:
    """Translate a list of lines with NLLB on the engine (GPU when available).
    Japanese output is transliterated to romaji (config [translation].romaji_ja)."""
    texts = [str(x) for x in payload.get("texts") or []]
    src = str(payload.get("src") or "").lower()
    tgt = str(payload.get("tgt") or "").lower()
    if not texts:
        raise HTTPException(400, "no texts")
    if len(texts) > 2000 or sum(len(t) for t in texts) > 500_000:
        raise HTTPException(413, "too many texts in one request")
    if src not in NLLB_CODES or tgt not in NLLB_CODES:
        raise HTTPException(400, f"unsupported translation pair: {src} -> {tgt}")
    args = make_args({})
    key = (args.translation_model, args.device)
    with TRANSCRIBERS_LOCK:
        if key not in TRANSLATORS:
            TRANSLATORS[key] = Translator(*key)
        translator = TRANSLATORS[key]
    try:
        return {"texts": translator.translate(texts, src, tgt)}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Batch jobs (server-side paths) ──

@app.post("/api/jobs")
def create_batch_job(payload: dict) -> dict:
    prune_jobs()
    paths = [str(x) for x in payload.get("paths") or []]
    if not paths:
        raise HTTPException(400, "no input paths")
    for raw in paths:
        require_in_roots(Path(raw))
    args = make_args(payload.get("options") or {})
    if args.output_dir:
        require_in_roots(Path(args.output_dir))
    files = collect_inputs(paths, args.recursive, exclude_dir=args.output_dir)
    files = dedupe_output_collisions(files, args)
    if not files:
        raise HTTPException(400, "no media files found in the given paths")

    job = {
        "id": uuid.uuid4().hex[:12],
        "type": "batch",
        "status": "running",
        "cancelled": False,
        "created": time.time(),
        "startedAt": time.time(),
        "finishedAt": None,
        "files": [
            {"name": f.name, "path": str(f), "status": "queued", "pct": 0, "detail": ""}
            for f in files
        ],
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job
    threading.Thread(target=run_batch_job, args=(job, files, args), daemon=True).start()
    return {"id": job["id"], "fileCount": len(files)}


def run_batch_job(job: dict, files: list[Path], args: argparse.Namespace) -> None:
    transcriber = get_transcriber(args)
    translator = get_translator(args)

    def worker(index: int, path: Path) -> None:
        entry = job["files"][index]
        if job["cancelled"]:
            entry["status"] = "cancelled"
            return
        entry["status"] = "processing"

        def on_event(status: str, pct: float | None, detail: str) -> None:
            entry["status"] = status
            if pct is not None:
                entry["pct"] = round(pct, 1)
            if detail:
                entry["detail"] = detail

        try:
            process_file(
                path, args, transcriber,
                on_event=on_event,
                cancel_check=lambda: job["cancelled"],
                translator=translator,
            )
            if entry["status"] not in ("done", "skipped"):
                entry["status"] = "done"
            entry["pct"] = 100
        except JobCancelled:
            entry["status"] = "cancelled"
        except Exception as e:  # keep the batch alive; report per file
            entry["status"] = "error"
            entry["detail"] = str(e)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for index, path in enumerate(files):
            pool.submit(worker, index, path)
    job["finishedAt"] = time.time()
    job["status"] = "cancelled" if job["cancelled"] else "done"


# ── Batch jobs (files uploaded by the client — no token needed) ──

@app.post("/api/jobs/upload")
async def create_upload_job(
    files: list[UploadFile] = File(...),
    options: str = Form("{}"),
) -> dict:
    prune_jobs()
    try:
        opts = json.loads(options or "{}")
        if not isinstance(opts, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "options must be a JSON object")
    if not files:
        raise HTTPException(400, "no files")

    args = make_args(opts)
    workdir = Path(tempfile.mkdtemp(prefix="subvid-in-"))
    outdir = Path(tempfile.mkdtemp(prefix="subvid-out-"))
    # Outputs always go to the job's own folder (downloaded via /files);
    # client-side batches never write elsewhere on this machine.
    args.output_dir = str(outdir)
    args.recursive = False

    limit = MAX_UPLOAD_MB * (1 << 20)
    saved: list[Path] = []
    try:
        for upload in files:
            name = Path(upload.filename or "media").name or "media"
            if Path(name).suffix.lower() not in MEDIA_EXTS:
                raise HTTPException(400, f"unsupported file type: {name}")
            target = workdir / name
            counter = 1
            while target.exists():
                target = workdir / f"{Path(name).stem}-{counter}{Path(name).suffix}"
                counter += 1
            written = 0
            with target.open("wb") as fh:
                while chunk := await upload.read(1 << 22):
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(413, f"'{name}' larger than {MAX_UPLOAD_MB} MB")
                    fh.write(chunk)
            saved.append(target)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)
        raise

    job = {
        "id": uuid.uuid4().hex[:12],
        "type": "batch",
        "status": "running",
        "cancelled": False,
        "created": time.time(),
        "startedAt": time.time(),
        "finishedAt": None,
        "_workdir": str(workdir),
        "_outdir": str(outdir),
        "files": [
            {"name": f.name, "path": f.name, "status": "queued", "pct": 0, "detail": ""}
            for f in saved
        ],
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job
    threading.Thread(target=run_upload_job, args=(job, saved, args), daemon=True).start()
    return {"id": job["id"], "fileCount": len(saved)}


def run_upload_job(job: dict, files: list[Path], args: argparse.Namespace) -> None:
    try:
        run_batch_job(job, files, args)
    finally:
        # The uploaded inputs are no longer needed; outputs stay in _outdir
        # until the job is pruned so the client can download them.
        shutil.rmtree(job["_workdir"], ignore_errors=True)


# ── Single-file transcription (upload from the browser) ──

@app.post("/api/transcribe")
async def transcribe_upload(
    file: UploadFile = File(...),
    language: str = Form(""),
    model: str = Form(""),
) -> dict:
    prune_jobs()
    options: dict = {}
    if model:
        options["model"] = model
    if language and language != "auto":
        options["language"] = language
    args = make_args(options)

    suffix = Path(file.filename or "media.mp4").suffix or ".mp4"
    limit = MAX_UPLOAD_MB * (1 << 20)
    written = 0
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        while chunk := await file.read(1 << 22):
            written += len(chunk)
            if written > limit:
                raise HTTPException(413, f"upload larger than {MAX_UPLOAD_MB} MB")
            tmp.write(chunk)
    except HTTPException:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise
    finally:
        tmp.close()

    job = {
        "id": uuid.uuid4().hex[:12],
        "type": "transcribe",
        "status": "running",
        "cancelled": False,
        "created": time.time(),
        "finishedAt": None,
        "pct": 0,
        "detail": "",
        "result": None,
        "error": "",
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job
    threading.Thread(
        target=run_transcribe_job,
        args=(job, Path(tmp.name), args, file.filename or "upload"),
        daemon=True,
    ).start()
    return {"id": job["id"]}


def run_transcribe_job(job: dict, tmp_path: Path, args: argparse.Namespace, name: str) -> None:
    try:
        transcriber = get_transcriber(args)

        def on_progress(pct: float, speed: float) -> None:
            job["pct"] = round(pct, 1)
            job["detail"] = f"{speed:.1f}x"

        lang, segments = transcriber.transcribe(
            tmp_path, args.language, "transcribe", name,
            vad_options=args.vad_options,
            on_progress=on_progress,
            cancel_check=lambda: job["cancelled"],
        )
        job["result"] = {"language": lang, "segments": segments}
        job["pct"] = 100
        job["status"] = "done"
    except JobCancelled:
        job["status"] = "cancelled"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        job["finishedAt"] = time.time()
        tmp_path.unlink(missing_ok=True)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    # Underscore keys are server internals (temp dir paths) — not for clients.
    return {k: v for k, v in get_job_or_404(job_id).items() if not k.startswith("_")}


def _job_output_files(job: dict) -> list[Path]:
    outdir = job.get("_outdir")
    if not outdir or not Path(outdir).is_dir():
        return []
    return sorted(
        (p for p in Path(outdir).iterdir() if p.is_file()),
        key=lambda p: p.name.lower(),
    )


@app.get("/api/jobs/{job_id}/files")
def list_job_files(job_id: str) -> dict:
    job = get_job_or_404(job_id)
    if not job.get("_outdir"):
        raise HTTPException(404, "job has no downloadable outputs")
    return {
        "files": [
            {"index": i, "name": p.name, "size": p.stat().st_size}
            for i, p in enumerate(_job_output_files(job))
        ],
    }


@app.get("/api/jobs/{job_id}/files/{index}")
def download_job_file(job_id: str, index: int) -> FileResponse:
    # Outputs are addressed by index into the job's own temp folder, so no
    # client-supplied path ever reaches the filesystem.
    files = _job_output_files(get_job_or_404(job_id))
    if index < 0 or index >= len(files):
        raise HTTPException(404, "no such output file")
    return FileResponse(files[index], filename=files[index].name)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = get_job_or_404(job_id)
    job["cancelled"] = True
    return {"ok": True}


# Serve the built web app when available (pnpm build → dist/client),
# making `python subvid_server.py` a self-contained local app.
DIST = Path(__file__).resolve().parent.parent / "dist" / "client"
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="site")


def main() -> None:
    parser = argparse.ArgumentParser(prog="subvid-server", description=__doc__)
    parser.add_argument("--port", type=int, default=int(SERVER_CFG.get("port", 8787)))
    parser.add_argument("--host", default=str(SERVER_CFG.get("host", "127.0.0.1")),
                        help="bind address; use 0.0.0.0 to reach the app from other devices — "
                             "browsing this machine's folders (server mode) then requires an "
                             "access token (one is generated if not configured)")
    parser.add_argument("--token", default=None,
                        help="access token required for server-filesystem endpoints (server "
                             "mode); also SUBVID_TOKEN env or [server].token in config.toml")
    parser.add_argument("--fs-root", action="append", default=None, metavar="DIR",
                        help="restrict browsing/batch processing to this directory (repeatable); "
                             "default: whole filesystem")
    args = parser.parse_args()

    token = args.token or os.environ.get("SUBVID_TOKEN") or str(SERVER_CFG.get("token", "") or "")
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not token:
        token = secrets.token_urlsafe(24)
        print("· no token configured for a non-loopback bind — generated one for this run:")
        print(f"    {token}")
        print("  set it permanently with --token, SUBVID_TOKEN or [server].token in config.toml")
    AUTH["token"] = token

    roots = args.fs_root if args.fs_root is not None else SERVER_CFG.get("fs_roots", [])
    for raw in roots or []:
        root = Path(raw).expanduser()
        if not root.is_dir():
            sys.exit(f"--fs-root: not a directory: {raw}")
        FS_ROOTS.append(root.resolve())

    _enable_windows_cuda_dlls()
    # Belt-and-braces: uvicorn replaces SIGINT/SIGTERM with a graceful
    # shutdown (which runs atexit → kills tracked ffmpeg children); this
    # covers SIGBREAK/SIGHUP delivered before or outside uvicorn's loop.
    _install_terminate_guards()
    site = f"http://{args.host}:{args.port}"
    print(f"subvid local engine listening on {site}")
    if token:
        print("· token required for server mode (browsing this machine's folders) — "
              "the web UI asks for it when you enable that mode, or open the site "
              "once as …/?engineToken=<token>")
    if not loopback:
        print("· exposing beyond this machine: use HTTPS for internet access "
              "(Tailscale or a reverse proxy — see [server] notes in config.toml) "
              "and consider --fs-root to limit filesystem access")
    if FS_ROOTS:
        print("· filesystem access restricted to: " + ", ".join(str(r) for r in FS_ROOTS))
    if DIST.is_dir():
        print(f"web UI available at {site} (serving dist/client)")
    else:
        print("web UI not built yet — run `npx pnpm build` to serve it from here, "
              "or use `pnpm dev` and it will detect this engine automatically")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
