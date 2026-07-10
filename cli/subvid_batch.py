#!/usr/bin/env python3
"""subvid-batch — Batch subtitle generation and muxing for media libraries.

Transcribes video/audio files with faster-whisper (GPU-accelerated when
available) and attaches the subtitles as a *selectable* track (soft subs)
via a lossless ffmpeg stream-copy remux — no re-encoding, no quality loss.
Designed for Jellyfin/Plex-style libraries.

Examples:
    # Transcribe a whole folder (recursive) and produce .mkv files with a
    # toggleable subtitle track next to the originals:
    python subvid_batch.py "D:/Series/MyShow"

    # Only write Jellyfin-style sidecar .srt files (original videos untouched):
    python subvid_batch.py "D:/Movies" --mode sidecar

    # Force Spanish, 2 files in parallel, custom output folder:
    python subvid_batch.py video1.mp4 video2.mkv -l es -j 2 -o out/
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

# Windows can't create symlinks without Developer Mode; the HF cache falls back
# to copies, which works fine — silence the noisy warning. Must be set before
# huggingface_hub gets imported (it happens inside faster_whisper).
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts", ".wmv", ".flv", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus", ".wma"}

# ISO 639-1 -> ISO 639-2/B (what Matroska/Jellyfin expect in stream metadata)
ISO639_2 = {
    "en": "eng", "es": "spa", "fr": "fre", "de": "ger", "pt": "por",
    "it": "ita", "nl": "dut", "ru": "rus", "ja": "jpn", "ko": "kor",
    "zh": "chi", "ar": "ara", "hi": "hin", "pl": "pol", "tr": "tur",
    "ca": "cat", "gl": "glg", "eu": "baq", "sv": "swe", "no": "nor",
    "da": "dan", "fi": "fin", "cs": "cze", "el": "gre", "he": "heb",
    "hu": "hun", "id": "ind", "ro": "rum", "th": "tha", "uk": "ukr",
    "vi": "vie",
}

LANG_TITLES = {
    "en": "English", "es": "Español", "fr": "Français", "de": "Deutsch",
    "pt": "Português", "it": "Italiano", "nl": "Nederlands", "ru": "Русский",
    "ja": "日本語", "ko": "한국어", "zh": "中文", "ar": "العربية",
    "ca": "Català", "gl": "Galego", "eu": "Euskara",
}

# ISO 639-1 -> NLLB-200 (FLORES) codes for the translation model.
NLLB_CODES = {
    "en": "eng_Latn", "es": "spa_Latn", "fr": "fra_Latn", "de": "deu_Latn",
    "pt": "por_Latn", "it": "ita_Latn", "nl": "nld_Latn", "ru": "rus_Cyrl",
    "ja": "jpn_Jpan", "ko": "kor_Hang", "zh": "zho_Hans", "ar": "arb_Arab",
    "hi": "hin_Deva", "pl": "pol_Latn", "tr": "tur_Latn", "ca": "cat_Latn",
    "gl": "glg_Latn", "eu": "eus_Latn", "sv": "swe_Latn", "da": "dan_Latn",
    "fi": "fin_Latn", "no": "nob_Latn", "cs": "ces_Latn", "el": "ell_Grek",
    "he": "heb_Hebr", "hu": "hun_Latn", "id": "ind_Latn", "ro": "ron_Latn",
    "th": "tha_Thai", "uk": "ukr_Cyrl", "vi": "vie_Latn",
}

DEFAULT_TRANSLATION_MODEL = "JustFrederik/nllb-200-distilled-1.3B-ct2-float16"

_print_lock = threading.Lock()

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")

# Every muxed output is tagged with this container-level comment so later
# scans can recognize our own products and never re-transcribe them.
SUBVID_TAG = "subvid-cli"


class JobCancelled(Exception):
    """Raised inside a transcription when a cancel flag is set."""


def load_config(path: Path | None = None) -> dict:
    """Read config.toml (or an explicit --config file). Returns {} if absent."""
    target = path or DEFAULT_CONFIG_PATH
    if not target.exists():
        if path is not None:
            print(f"warning: config file '{path}' not found, using defaults", file=sys.stderr)
        return {}
    import tomllib
    try:
        return tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        print(f"warning: could not parse '{target}': {e}; using defaults", file=sys.stderr)
        return {}


def log(name: str, message: str) -> None:
    with _print_lock:
        print(f"[{name}] {message}", flush=True)


def _enable_windows_cuda_dlls() -> None:
    """Let ctranslate2 find cuBLAS/cuDNN DLLs installed via the nvidia-* pip wheels."""
    if os.name != "nt":
        return
    try:
        import nvidia  # type: ignore
    except ImportError:
        return
    for base in nvidia.__path__:
        root = Path(base)
        for bin_dir in root.glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
            except OSError:
                pass


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = round(seconds * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# Subtitle line shaping (mirrors the web app's heuristics): break on silences,
# keep lines short, and never let a caption linger on screen.
MAX_LINE_CHARS = 46
MAX_LINE_WORDS = 9
MAX_LINE_SECONDS = 5.5
WORD_GAP_BREAK = 0.6


def words_to_lines(words: list[dict]) -> list[dict]:
    """Regroup word-level timestamps into subtitle lines.

    Word timing is what prevents the classic Whisper artifact of a single
    caption spanning a long silence: each line ends when its last word does.
    """
    lines: list[dict] = []
    current: list[dict] = []

    def flush() -> None:
        if not current:
            return
        lines.append({
            "start": current[0]["start"],
            "end": current[-1]["end"],
            "text": " ".join(w["text"] for w in current),
            # Kept for consumers that animate word-by-word (the web editor);
            # the SRT writer ignores this key.
            "words": current.copy(),
        })
        current.clear()

    for word in words:
        if current:
            gap = word["start"] - current[-1]["end"]
            text_length = sum(len(w["text"]) + 1 for w in current) + len(word["text"])
            duration = word["end"] - current[0]["start"]
            if (
                gap > WORD_GAP_BREAK
                or len(current) >= MAX_LINE_WORDS
                or text_length > MAX_LINE_CHARS
                or duration > MAX_LINE_SECONDS
            ):
                flush()
        current.append(word)
        duration = current[-1]["end"] - current[0]["start"]
        if re.search(r"[.!?…]$", word["text"]) and len(current) >= 3 and duration >= 1.0:
            flush()

    flush()
    return lines


def segments_to_srt(segments: list[dict]) -> str:
    blocks = []
    for i, seg in enumerate(segments, start=1):
        blocks.append(f"{i}\n{format_srt_time(seg['start'])} --> {format_srt_time(seg['end'])}\n{seg['text']}")
    return "\n\n".join(blocks) + "\n"


def collect_inputs(paths: list[str], recursive: bool, exclude_dir: str | None = None) -> list[Path]:
    """Gather media files. Anything inside `exclude_dir` (the output folder)
    is ignored so previous outputs are never re-ingested as inputs."""
    excluded = Path(exclude_dir).resolve() if exclude_dir else None
    files: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            candidates = sorted(x for x in it if x.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS)
        elif p.is_file():
            candidates = [p]
        else:
            print(f"warning: '{raw}' not found, skipping", file=sys.stderr)
            continue
        for c in candidates:
            r = c.resolve()
            if r in seen:
                continue
            if excluded and (r == excluded or excluded in r.parents):
                continue
            seen.add(r)
            files.append(c)
    return files


def dedupe_output_collisions(files: list[Path], args: argparse.Namespace) -> list[Path]:
    """Drop inputs whose outputs would land on the same path (same stem into
    the same output folder), which would make parallel muxes overwrite each
    other. The first occurrence wins; the rest are reported and skipped."""
    seen: dict[tuple, Path] = {}
    kept: list[Path] = []
    for f in files:
        out_dir = Path(args.output_dir).resolve() if args.output_dir else f.parent.resolve()
        key = (str(out_dir).lower(), f.stem.lower())
        if key in seen:
            print(
                f"  ! skipping '{f}' — its output name collides with '{seen[key]}' "
                f"in the same output folder; rename it or process it separately",
                file=sys.stderr,
            )
            continue
        seen[key] = f
        kept.append(f)
    return kept


def has_subvid_marker(path: Path) -> bool:
    """True if the file was produced by our own mux (container comment tag)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
        tags = (json.loads(out).get("format", {}) or {}).get("tags", {}) or {}
        comment = next((v for k, v in tags.items() if k.lower() == "comment"), "")
        return SUBVID_TAG in str(comment)
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        return False


def ffprobe_subtitle_stream_count(path: Path) -> int:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=index", "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
        return len(json.loads(out).get("streams", []))
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        return 0


def mux_subtitles(
    source: Path,
    tracks: list[tuple[str, Path]],
    output: Path,
    make_default: bool,
) -> None:
    """Stream-copy remux: attach one SRT per language as subtitle tracks."""
    existing_subs = ffprobe_subtitle_stream_count(source)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    for _, srt_file in tracks:
        cmd += ["-i", str(srt_file)]
    cmd += ["-map", "0"]
    for i in range(len(tracks)):
        cmd += ["-map", f"{i + 1}:0"]
    if output.suffix.lower() == ".mp4":
        cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text"]
    else:
        cmd += ["-c", "copy"]
    for offset, (lang, _) in enumerate(tracks):
        idx = existing_subs + offset
        cmd += [
            f"-metadata:s:s:{idx}", f"language={ISO639_2.get(lang, 'und')}",
            f"-metadata:s:s:{idx}", f"title={LANG_TITLES.get(lang, lang or 'Subtitles')}",
            f"-disposition:s:{idx}", "default" if (make_default and offset == 0) else "0",
        ]
    # Write to a unique temp name first: a crash never leaves a half-written
    # output, and two concurrent muxes can never fight over the same file.
    # The odd extension hides nothing from ffmpeg because -f is explicit.
    tmp = output.with_name(f"{output.name}.{uuid.uuid4().hex[:8]}.part")
    cmd += [
        "-metadata", f"comment={SUBVID_TAG}",
        "-f", "mp4" if output.suffix.lower() == ".mp4" else "matroska",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        tmp.replace(output)
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg mux failed: {e.stderr.strip().splitlines()[-1] if e.stderr else e}") from e


class Transcriber:
    """Lazy, shared faster-whisper model (thread-safe: ctranslate2 releases the GIL)."""

    def __init__(self, model_name: str, device: str, compute_type: str):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model

        # Resolve the model from the local HF cache first; on a cache miss,
        # download it explicitly so the user sees progress bars instead of a
        # silent multi-GB download.
        try:
            model_path = download_model(self.model_name, local_files_only=True)
        except Exception:
            log("model", f"first run: downloading '{self.model_name}' from Hugging Face…")
            log("model", "(progress bars below; the model is cached, next runs start instantly)")
            model_path = download_model(self.model_name)

        device = self.device
        compute = self.compute_type
        if device == "auto":
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"

        log("model", f"loading '{self.model_name}' on {device} ({compute})…")
        try:
            return WhisperModel(model_path, device=device, compute_type=compute)
        except Exception as e:
            if device == "cuda":
                log("model", f"CUDA init failed ({e}); falling back to CPU int8")
                return WhisperModel(model_path, device="cpu", compute_type="int8")
            raise

    @property
    def model(self):
        with self._lock:
            if self._model is None:
                self._model = self._load()
            return self._model

    def transcribe(
        self,
        path: Path,
        language: str | None,
        task: str,
        name: str,
        vad_options: dict | None = None,
        on_progress=None,
        cancel_check=None,
    ) -> tuple[str, list[dict]]:
        segments_iter, info = self.model.transcribe(
            str(path),
            language=language,
            task=task,
            vad_filter=vad_options is not None,
            vad_parameters=vad_options,
            beam_size=5,
            word_timestamps=True,
            # Reduces repetition/hallucination loops on long or noisy audio.
            condition_on_previous_text=False,
        )
        detected = info.language or language or "und"
        duration = info.duration or 0
        log(name, f"language={detected} (p={info.language_probability:.2f}), duration={duration/60:.1f} min")

        segments: list[dict] = []
        words: list[dict] = []
        started = time.monotonic()
        next_report = 10
        for seg in segments_iter:
            if cancel_check and cancel_check():
                raise JobCancelled()
            text = seg.text.strip()
            if text:
                segments.append({"start": seg.start, "end": seg.end, "text": text})
                for w in seg.words or []:
                    word_text = w.word.strip()
                    if word_text:
                        words.append({"start": w.start, "end": w.end, "text": word_text})
            if duration:
                pct = min(100, seg.end / duration * 100)
                elapsed = time.monotonic() - started
                speed = seg.end / elapsed if elapsed else 0
                if on_progress:
                    on_progress(pct, speed)
                if pct >= next_report:
                    log(name, f"transcribing… {pct:.0f}% ({speed:.1f}x realtime)")
                    next_report = (int(pct // 10) + 1) * 10

        # Prefer word-level regrouping; fall back to raw segments if the model
        # returned no word timings.
        if words:
            segments = words_to_lines(words)
        return detected, segments


class Translator:
    """Lazy, shared NLLB-200 translator on CTranslate2 (same runtime as Whisper).

    Uses the `tokenizers` fast tokenizer, so no PyTorch is needed. Source
    sequences follow the NLLB convention: [src_lang] + pieces + ["</s>"],
    with the target language passed as a decoding prefix.
    """

    def __init__(self, model_repo: str, device: str):
        self.model_repo = model_repo
        self.device = device
        self._model = None
        self._tokenizer = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        import ctranslate2
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer

        try:
            path = snapshot_download(self.model_repo, local_files_only=True)
        except Exception:
            log("translator", f"first run: downloading '{self.model_repo}' from Hugging Face…")
            path = snapshot_download(self.model_repo)

        device = self.device
        if device == "auto":
            try:
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"
        compute = "float16" if device == "cuda" else "int8"
        log("translator", f"loading NLLB on {device} ({compute})…")
        try:
            self._model = ctranslate2.Translator(path, device=device, compute_type=compute)
        except Exception as e:
            if device == "cuda":
                log("translator", f"CUDA init failed ({e}); falling back to CPU int8")
                self._model = ctranslate2.Translator(path, device="cpu", compute_type="int8")
            else:
                raise
        self._tokenizer = Tokenizer.from_file(os.path.join(path, "tokenizer.json"))

    def _ensure(self) -> None:
        with self._lock:
            if self._model is None:
                self._load()

    def translate(self, texts: list[str], src: str, tgt: str) -> list[str]:
        """Translate a list of lines between two ISO 639-1 languages."""
        src_code = NLLB_CODES.get(src)
        tgt_code = NLLB_CODES.get(tgt)
        if not src_code or not tgt_code:
            raise ValueError(f"unsupported translation pair: {src} -> {tgt}")
        self._ensure()
        tok = self._tokenizer
        sources = [
            [src_code] + tok.encode(t, add_special_tokens=False).tokens + ["</s>"]
            for t in texts
        ]
        results = self._model.translate_batch(
            sources,
            target_prefix=[[tgt_code]] * len(sources),
            beam_size=4,
            max_batch_size=32,
        )
        out: list[str] = []
        for result in results:
            hyp = result.hypotheses[0]
            if hyp and hyp[0] == tgt_code:
                hyp = hyp[1:]
            ids = [tok.token_to_id(t) for t in hyp]
            out.append(tok.decode([i for i in ids if i is not None]).strip())
        return out

    def translate_segments(self, segments: list[dict], src: str, tgt: str) -> list[dict]:
        texts = self.translate([s["text"] for s in segments], src, tgt)
        return [
            {"start": s["start"], "end": s["end"], "text": text}
            for s, text in zip(segments, texts)
        ]


def extract_audio_track(path: Path, track: int, out_dir: Path) -> Path:
    """Extract one specific audio stream to a temp WAV (for --audio-track)."""
    tmp = out_dir / f".{path.stem}.a{track}.{uuid.uuid4().hex[:6]}.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(path), "-map", f"0:a:{track}",
             "-ac", "1", "-ar", "16000", str(tmp)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        detail = e.stderr.strip().splitlines()[-1] if e.stderr else str(e)
        raise RuntimeError(f"could not extract audio track {track}: {detail}") from e
    return tmp


def process_file(
    path: Path,
    args: argparse.Namespace,
    transcriber: Transcriber,
    on_event=None,
    cancel_check=None,
    translator: Translator | None = None,
) -> str:
    name = path.name

    def emit(status: str, pct: float | None = None, detail: str = "") -> None:
        if on_event:
            on_event(status, pct, detail)

    if path.stem.endswith(".subs"):
        emit("skipped", 100, "own mux output")
        return f"skipped (own mux output): {name}"
    is_audio = path.suffix.lower() in AUDIO_EXTS
    # Outputs of previous runs carry a container tag — never re-transcribe
    # them, even when they were written to a different folder.
    if not args.overwrite and not is_audio and has_subvid_marker(path):
        emit("skipped", 100, "output of a previous run")
        return f"skipped (output of a previous run): {name}"
    mode = "sidecar" if is_audio else args.mode
    task = "translate" if args.task == "translate" else "transcribe"

    out_dir = Path(args.output_dir) if args.output_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # "auto" keeps the original container when it can hold text subtitles
    # (MP4 uses mov_text, MKV uses SRT); anything else is remuxed into MKV,
    # the safest container for subtitle tracks.
    if args.container == "auto":
        ext = path.suffix.lower()
        container = "mp4" if ext in (".mp4", ".m4v", ".mov") else "mkv"
    else:
        container = args.container

    # Predict output paths (language becomes known after detection, so for
    # skip-checks we use the forced language when given, else probe for any).
    def srt_path(lang: str) -> Path:
        return out_dir / f"{path.stem}.{lang}.srt"

    def mux_path() -> Path:
        ext = f".{container}"
        candidate = out_dir / f"{path.stem}{ext}"
        if candidate.resolve() == path.resolve():
            candidate = out_dir / f"{path.stem}.subs{ext}"
        return candidate

    def has_sidecar() -> bool:
        prefix = f"{path.stem}."
        return any(
            p.name.startswith(prefix) and p.suffix.lower() == ".srt"
            for p in out_dir.iterdir()
        )

    if not args.overwrite:
        srt_exists = has_sidecar()
        mux_exists = mux_path().exists()
        if mode == "sidecar" and srt_exists:
            emit("skipped", 100, "sidecar exists")
            return f"skipped (sidecar exists): {name}"
        if mode == "mux" and mux_exists:
            emit("skipped", 100, "muxed output exists")
            return f"skipped (muxed output exists): {name}"
        if mode == "both" and mux_exists and srt_exists:
            emit("skipped", 100, "outputs exist")
            return f"skipped (outputs exist): {name}"
        # A video that already carries a subtitle stream *and* has a matching
        # sidecar is almost certainly a previous mux output picked up by a
        # re-scan of the same folder — don't process it again.
        if srt_exists and not is_audio and ffprobe_subtitle_stream_count(path) > 0:
            emit("skipped", 100, "already has a subtitle track")
            return f"skipped (already has a subtitle track): {name}"

    log(name, "transcribing…")
    emit("transcribing", 0)
    t0 = time.monotonic()

    # Multi-audio inputs: Whisper only sees ONE stream (the first). When the
    # user picks another one, extract it to a temp WAV first.
    audio_source = path
    tmp_audio: Path | None = None
    if args.audio_track and not is_audio:
        log(name, f"extracting audio track #{args.audio_track}…")
        tmp_audio = extract_audio_track(path, args.audio_track, out_dir)
        audio_source = tmp_audio

    try:
        lang, segments = transcriber.transcribe(
            audio_source, args.language, task, name,
            vad_options=args.vad_options,
            on_progress=lambda pct, speed: emit("transcribing", pct, f"{speed:.1f}x"),
            cancel_check=cancel_check,
        )
    finally:
        if tmp_audio:
            tmp_audio.unlink(missing_ok=True)
    if task == "translate":
        lang = "en"
    log(name, f"transcription done in {time.monotonic() - t0:.0f}s — {len(segments)} lines")

    if not segments:
        emit("done", 100, "no speech detected")
        return f"no speech detected, nothing written: {name}"

    # One subtitle track per output language: the transcription plus a local
    # NLLB translation for every extra language requested via --to.
    tracks: list[tuple[str, list[dict]]] = [(lang, segments)]
    for target_lang in args.to_langs or []:
        if target_lang == lang:
            continue
        if translator is None or lang not in NLLB_CODES or target_lang not in NLLB_CODES:
            log(name, f"translation {lang} -> {target_lang} not available, skipping that track")
            continue
        emit("translating", 92, target_lang)
        log(name, f"translating {lang} -> {target_lang}…")
        tx0 = time.monotonic()
        tracks.append((target_lang, translator.translate_segments(segments, lang, target_lang)))
        log(name, f"translated to {target_lang} in {time.monotonic() - tx0:.0f}s")

    srt_files: list[tuple[str, Path]] = []
    temp_srts: list[Path] = []
    for track_lang, track_segments in tracks:
        content = segments_to_srt(track_segments)
        if mode in ("sidecar", "both"):
            sidecar = srt_path(track_lang)
            sidecar.write_text(content, encoding="utf-8")
            log(name, f"sidecar written: {sidecar.name}")
            srt_files.append((track_lang, sidecar))
        else:
            tmp = out_dir / f".{path.stem}.{track_lang}.{uuid.uuid4().hex[:6]}.tmp.srt"
            tmp.write_text(content, encoding="utf-8")
            temp_srts.append(tmp)
            srt_files.append((track_lang, tmp))

    try:
        if mode in ("mux", "both") and not is_audio:
            emit("muxing", 96)
            target = mux_path()
            log(name, f"muxing {len(srt_files)} subtitle track(s) into {target.name} (stream copy)…")
            mux_subtitles(path, srt_files, target, args.default_track)
            log(name, f"done: {target.name}")
    finally:
        for tmp in temp_srts:
            tmp.unlink(missing_ok=True)

    langs_label = "+".join(track_lang for track_lang, _ in tracks)
    emit("done", 100, f"{len(segments)} lines · {langs_label}")
    return f"ok: {name} ({len(segments)} lines, langs={langs_label})"


def build_parser(config: dict) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subvid-batch",
        description="Batch-generate subtitles with faster-whisper and attach them as a selectable track (no re-encoding).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", help="video/audio files or directories")
    parser.add_argument("-l", "--language", default=None, help="force audio language (ISO code, e.g. es); default: auto-detect per file")
    parser.add_argument("-m", "--model", default="large-v3-turbo", help="faster-whisper model (tiny/base/small/medium/large-v3/large-v3-turbo)")
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"], help="inference device")
    parser.add_argument("--compute-type", default="auto", help="ctranslate2 compute type (auto/float16/int8/int8_float16)")
    parser.add_argument("--mode", default="sidecar", choices=["mux", "sidecar", "both"],
                        help="mux: new file with embedded toggleable track; sidecar: .srt next to the video (Jellyfin auto-detects it); both")
    parser.add_argument("--container", default="auto", choices=["auto", "mkv", "mp4"],
                        help="container for muxed output; auto keeps the original when possible (mp4/mov stay mp4, rest become mkv)")
    parser.add_argument("--no-vad", action="store_true",
                        help="disable voice-activity filtering (try this if quiet speech or speech over music gets skipped)")
    parser.add_argument("--vad-threshold", type=float, default=None, metavar="0.0-1.0",
                        help="VAD sensitivity threshold; LOWER = more sensitive (default 0.5, try 0.3 for quiet speech)")
    parser.add_argument("--config", default=None, metavar="FILE",
                        help=f"config file (default: {DEFAULT_CONFIG_PATH.name} next to this script)")
    parser.add_argument("-o", "--output-dir", default=None, help="output directory (default: next to each input)")
    parser.add_argument("-j", "--jobs", type=int, default=1,
                        help="files processed in parallel (1 is best on a single GPU: parallel jobs share the same CUDA queue and freeze the desktop for no throughput gain)")
    parser.add_argument("--to", dest="to_langs", default=None, metavar="LANGS",
                        help="comma-separated extra subtitle languages translated locally with NLLB "
                             "(e.g. 'es,en'); the original-language track is always included")
    parser.add_argument("--audio-track", type=int, default=0, metavar="N",
                        help="audio stream to transcribe when the video has several (0 = first)")
    parser.add_argument("--task", default="transcribe", choices=["transcribe", "translate"],
                        help="translate = Whisper's built-in English translation (prefer '--to en', which uses NLLB)")
    parser.add_argument("--default-track", action="store_true", help="mark the new subtitle track as default (enabled on playback)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="don't scan directories recursively")
    parser.add_argument("--overwrite", action="store_true", help="regenerate even if outputs already exist")

    # Apply [general] config values as parser defaults (empty strings mean "unset").
    general = dict(config.get("general", {}))
    for key in ("language", "output_dir"):
        if not general.get(key):
            general.pop(key, None)
    valid_dests = {action.dest for action in parser._actions}
    parser.set_defaults(**{k: v for k, v in general.items() if k in valid_dests})
    return parser


def finalize_args(args: argparse.Namespace, config: dict) -> None:
    """Resolve VAD options and subtitle line shaping from config + CLI flags."""
    # Effective VAD settings: [vad] section + command-line overrides.
    vad_cfg = config.get("vad", {})
    if args.no_vad or not vad_cfg.get("enabled", False):
        args.vad_options = None
    else:
        threshold = args.vad_threshold if args.vad_threshold is not None else vad_cfg.get("threshold", 0.5)
        args.vad_options = {
            "threshold": max(0.05, min(0.95, float(threshold))),
            "min_silence_duration_ms": int(vad_cfg.get("min_silence_ms", 2000)),
            "speech_pad_ms": int(vad_cfg.get("speech_pad_ms", 400)),
        }

    # Translation targets: --to flag, else [translation].output_langs.
    tx_cfg = config.get("translation", {})
    raw_langs = args.to_langs if args.to_langs is not None else tx_cfg.get("output_langs", [])
    if isinstance(raw_langs, str):
        raw_langs = [x.strip() for x in raw_langs.split(",") if x.strip()]
    seen_langs: list[str] = []
    for lang in (str(x).lower() for x in raw_langs):
        if lang in seen_langs:
            continue
        if lang not in NLLB_CODES:
            print(f"warning: unsupported output language '{lang}' ignored "
                  f"(supported: {', '.join(sorted(NLLB_CODES))})", file=sys.stderr)
            continue
        seen_langs.append(lang)
    args.to_langs = seen_langs
    args.translation_model = tx_cfg.get("model", DEFAULT_TRANSLATION_MODEL)

    # Subtitle line shaping from [lines].
    lines_cfg = config.get("lines", {})
    global MAX_LINE_CHARS, MAX_LINE_WORDS, MAX_LINE_SECONDS, WORD_GAP_BREAK
    MAX_LINE_CHARS = int(lines_cfg.get("max_chars", MAX_LINE_CHARS))
    MAX_LINE_WORDS = int(lines_cfg.get("max_words", MAX_LINE_WORDS))
    MAX_LINE_SECONDS = float(lines_cfg.get("max_seconds", MAX_LINE_SECONDS))
    WORD_GAP_BREAK = float(lines_cfg.get("word_gap_break", WORD_GAP_BREAK))


def main() -> int:
    # Pre-parse --config so the file's values become the parser defaults
    # (explicit command-line arguments always win over the config file).
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None)
    pre_args, _ = pre.parse_known_args()
    config = load_config(Path(pre_args.config) if pre_args.config else None)
    args = build_parser(config).parse_args()
    finalize_args(args, config)

    # Windows consoles often default to cp1252, which can't print ✔/… symbols.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    _enable_windows_cuda_dlls()

    files = collect_inputs(args.inputs, args.recursive, exclude_dir=args.output_dir)
    files = dedupe_output_collisions(files, args)
    if not files:
        print("No media files found.", file=sys.stderr)
        return 1

    vad_info = f"vad={args.vad_options['threshold']}" if args.vad_options else "vad=off"
    to_info = f" | to={','.join(args.to_langs)}" if args.to_langs else ""
    print(f"Found {len(files)} file(s). Model: {args.model} | mode: {args.mode} | jobs: {args.jobs} | {vad_info}{to_info}")
    print("(press Ctrl+C to stop)\n")
    transcriber = Transcriber(args.model, args.device, args.compute_type)
    translator = Translator(args.translation_model, args.device) if args.to_langs else None

    failures = 0
    t0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=max(1, args.jobs))
    futures = {
        pool.submit(process_file, f, args, transcriber, translator=translator): f
        for f in files
    }
    pending = set(futures)
    try:
        # Poll with a timeout instead of blocking indefinitely: on Windows,
        # Ctrl+C is not delivered while the main thread waits on a lock with
        # no timeout, which made the process seem unkillable.
        while pending:
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                src = futures[future]
                try:
                    print(f"  ✔ {future.result()}")
                except Exception as e:
                    failures += 1
                    print(f"  ✖ failed: {src.name} — {e}", file=sys.stderr)
        pool.shutdown(wait=True)
    except KeyboardInterrupt:
        print("\nInterrupted — aborting. Files already completed are kept; "
              "half-written outputs stay as .part files and are ignored.", file=sys.stderr)
        pool.shutdown(wait=False, cancel_futures=True)
        # Transcriptions run inside native (CTranslate2) threads that cannot be
        # interrupted from Python, so a hard exit is the only reliable stop.
        os._exit(130)

    print(f"\nFinished in {(time.monotonic() - t0)/60:.1f} min — {len(files) - failures} ok, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
