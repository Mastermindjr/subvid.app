#!/usr/bin/env python3
"""subvid-server — local engine that exposes the faster-whisper pipeline to the web UI.

Run:  python subvid_server.py            (listens on http://127.0.0.1:8787)

The web app auto-detects this server and, when present, offers:
  - single-file transcription on your GPU (upload → segments for the editor)
  - batch processing of server-side folders with per-file progress

Security note: the API can browse the local filesystem and process local
files, so it binds to 127.0.0.1 only. Do not expose it to the network.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
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
    build_parser,
    collect_inputs,
    dedupe_output_collisions,
    finalize_args,
    load_config,
    process_file,
)

CONFIG = load_config()
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS

app = FastAPI(title="subvid local engine")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
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


def make_args(options: dict | None = None) -> argparse.Namespace:
    """Build a Namespace with config.toml defaults, then apply request options."""
    args = build_parser(CONFIG).parse_args(["__dummy__"])
    args.inputs = []
    finalize_args(args, CONFIG)
    for key, value in (options or {}).items():
        attr = key.replace("-", "_")
        if value is None or value == "":
            continue
        if attr == "vad_threshold":
            # Re-enable VAD even when the config disables it by default: the
            # UI sends a threshold only when its VAD checkbox is on.
            threshold = max(0.05, min(0.95, float(value)))
            if args.vad_options:
                args.vad_options["threshold"] = threshold
            else:
                vad_cfg = CONFIG.get("vad", {})
                args.vad_options = {
                    "threshold": threshold,
                    "min_silence_duration_ms": int(vad_cfg.get("min_silence_ms", 2000)),
                    "speech_pad_ms": int(vad_cfg.get("speech_pad_ms", 400)),
                }
        elif attr == "no_vad" and value:
            args.vad_options = None
        elif hasattr(args, attr):
            setattr(args, attr, value)
    if not args.language or args.language == "auto":
        args.language = None
    # Normalize translation targets (the option may arrive as "es,fr" or a list).
    raw_langs = args.to_langs
    if isinstance(raw_langs, str):
        raw_langs = [x.strip() for x in raw_langs.split(",") if x.strip()]
    args.to_langs = [
        lang for lang in (str(x).lower() for x in raw_langs or []) if lang in NLLB_CODES
    ]
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


@app.get("/api/health")
def health() -> dict:
    try:
        import ctranslate2
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        device = "cpu"
    return {
        "ok": True,
        "engine": "subvid",
        "device": device,
        "model": CONFIG.get("general", {}).get("model", "large-v3-turbo"),
    }


@app.get("/api/browse")
def browse(path: str = "") -> dict:
    if not path:
        entries = [{"name": str(Path.home()), "path": str(Path.home())}]
        if os.name == "nt":
            try:
                entries += [{"name": d, "path": d} for d in os.listdrives()]
            except OSError:
                pass
        else:
            entries.append({"name": "/", "path": "/"})
        return {"path": "", "parent": None, "dirs": entries, "files": []}

    p = Path(path)
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
    parent = str(p.parent) if p.parent != p else ""
    return {"path": str(p), "parent": parent, "dirs": dirs, "files": files}


# ── Batch jobs (server-side paths) ──

@app.post("/api/jobs")
def create_batch_job(payload: dict) -> dict:
    paths = [str(x) for x in payload.get("paths") or []]
    if not paths:
        raise HTTPException(400, "no input paths")
    args = make_args(payload.get("options") or {})
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


# ── Single-file transcription (upload from the browser) ──

@app.post("/api/transcribe")
async def transcribe_upload(
    file: UploadFile = File(...),
    language: str = Form(""),
    model: str = Form(""),
) -> dict:
    options: dict = {}
    if model:
        options["model"] = model
    if language and language != "auto":
        options["language"] = language
    args = make_args(options)

    suffix = Path(file.filename or "media.mp4").suffix or ".mp4"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        while chunk := await file.read(1 << 22):
            tmp.write(chunk)
    finally:
        tmp.close()

    job = {
        "id": uuid.uuid4().hex[:12],
        "type": "transcribe",
        "status": "running",
        "cancelled": False,
        "created": time.time(),
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
        tmp_path.unlink(missing_ok=True)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    return get_job_or_404(job_id)


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
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1", help="do not expose beyond localhost")
    args = parser.parse_args()

    _enable_windows_cuda_dlls()
    site = f"http://{args.host}:{args.port}"
    print(f"subvid local engine listening on {site}")
    if DIST.is_dir():
        print(f"web UI available at {site} (serving dist/client)")
    else:
        print("web UI not built yet — run `npx pnpm build` to serve it from here, "
              "or use `pnpm dev` and it will detect this engine automatically")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
