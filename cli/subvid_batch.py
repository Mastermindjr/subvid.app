#!/usr/bin/env python3
"""subvid-batch — Batch subtitle generation and muxing for media libraries.

Transcribes video/audio files with Whisper and attaches the subtitles as a
*selectable* track (soft subs) via a lossless ffmpeg stream-copy remux — no
re-encoding, no quality loss. Designed for Jellyfin/Plex-style libraries.

GPU acceleration (--device auto picks the best available backend):
    cuda    NVIDIA GPUs via faster-whisper/CTranslate2 (works out of the box)
    mlx     Apple Silicon GPUs via mlx-whisper
    vulkan  AMD/Intel GPUs (dedicated or integrated) via whisper.cpp
            (pywhispercpp built with GGML_VULKAN=1 — see requirements.txt)

Examples:
    # Transcribe a whole folder (recursive) and produce .mkv files with a
    # toggleable subtitle track next to the originals:
    python subvid_batch.py "D:/Series/MyShow"

    # Only write Jellyfin-style sidecar .srt files (original videos untouched):
    python subvid_batch.py "D:/Movies" --mode sidecar

    # Force Spanish, 2 files in parallel, custom output folder:
    python subvid_batch.py video1.mp4 video2.mkv -l es -j 2 -o out/

    # Styled .ass subtitles (web-app presets) and hard-burned subtitles:
    python subvid_batch.py movie.mkv --style neon --sub-position top
    python subvid_batch.py movie.mkv --mode burn --style bold --sub-bg-opacity 0.6
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import platform
import re
import signal
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


# ── Child-process lifetime guardrails ────────────────────────────────────────
# Spawned ffmpeg/ffprobe processes must never outlive this process, no matter
# how it dies:
#   - Windows: children are placed in a Job Object with KILL_ON_JOB_CLOSE; the
#     kernel terminates the whole job when our last handle disappears — this
#     covers even a hard `taskkill /F` on us.
#   - Linux: PR_SET_PDEATHSIG makes the kernel SIGKILL the child when we die.
#   - macOS: no kernel equivalent, so children are tracked and killed from
#     atexit + termination-signal handlers (everything short of SIGKILL; the
#     GUI additionally kills the whole process group).

_CHILDREN: set[subprocess.Popen] = set()
_CHILDREN_LOCK = threading.Lock()
_WIN_JOB: int | None = None


def _windows_job() -> int:
    """One shared Job Object configured to kill its processes on handle close."""
    global _WIN_JOB
    if _WIN_JOB is not None:
        return _WIN_JOB
    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(field, ctypes.c_uint64) for field in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job = kernel32.CreateJobObjectW(None, None)
    if job:
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            job = 0
    _WIN_JOB = job or 0
    return _WIN_JOB


def _bind_child_to_parent_linux() -> None:
    """preexec_fn: the kernel SIGKILLs this child if the parent dies first."""
    import ctypes
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def _adopt_child(proc: subprocess.Popen) -> None:
    with _CHILDREN_LOCK:
        _CHILDREN.add(proc)
    if os.name == "nt":
        job = _windows_job()
        if job:
            import ctypes
            ctypes.WinDLL("kernel32").AssignProcessToJobObject(job, int(proc._handle))  # type: ignore[attr-defined]


def _forget_child(proc: subprocess.Popen) -> None:
    with _CHILDREN_LOCK:
        _CHILDREN.discard(proc)


def _kill_children() -> None:
    with _CHILDREN_LOCK:
        procs = list(_CHILDREN)
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass


atexit.register(_kill_children)


def _install_terminate_guards() -> None:
    """On termination signals, kill tracked children first, then die normally.

    SIGINT is left alone on purpose: KeyboardInterrupt handling elsewhere
    (main() here, uvicorn in the server) already takes care of it.
    """
    def _handler(signum: int, _frame) -> None:
        _kill_children()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            if signal.getsignal(sig) == signal.SIG_DFL:
                signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # not the main thread, or signal unsupported


def run_child(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    """Like subprocess.run(capture_output=True, text=True), but the child is
    registered with the guardrails above so it can never outlive us."""
    kwargs: dict = {}
    if sys.platform.startswith("linux"):
        kwargs["preexec_fn"] = _bind_child_to_parent_linux
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs
    )
    _adopt_child(proc)
    try:
        out, err = proc.communicate()
    except BaseException:
        proc.kill()
        raise
    finally:
        _forget_child(proc)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


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


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def mlx_available() -> bool:
    """True when the MLX backend (Apple Silicon GPU via mlx-whisper) is usable."""
    if not is_apple_silicon():
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def ct2_cuda_available() -> bool:
    """True when CTranslate2 sees at least one CUDA (NVIDIA) device."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _whispercpp_gpu_flags() -> set[str]:
    """GPU backends compiled into pywhispercpp (whisper.cpp), e.g. {"vulkan"}.

    Parsed from whisper.cpp's system-info string; empty when pywhispercpp is
    missing or is a CPU-only build.
    """
    try:
        from pywhispercpp.model import Model
        info = str(Model.system_info() or "")
    except Exception:
        return set()
    flags: set[str] = set()
    # Old builds print "VULKAN = 1"; newer ggml prints one "Vulkan : …" section
    # per compiled backend. Accept the name unless it is explicitly "= 0".
    for name in ("VULKAN", "CUDA", "METAL", "SYCL", "HIP", "OPENCL"):
        if re.search(rf"\b{name}\b(?!\s*=\s*0)", info, re.IGNORECASE):
            flags.add(name.lower())
    return flags


def vulkan_available() -> bool:
    """True when pywhispercpp has a GPU backend for AMD/Intel (Vulkan/SYCL/HIP)."""
    return bool(_whispercpp_gpu_flags() - {"cuda", "metal"})


# Standard Whisper model names -> pre-converted MLX repos (Apple GPU).
# Anything not in this map is passed through as a Hugging Face repo id.
MLX_WHISPER_REPOS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = round(seconds * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ── Japanese romaji transliteration ─────────────────────────────────────────
# Subtitles translated *into* Japanese are emitted in Hepburn romaji instead
# of kanji/kana (configurable via [translation].romaji_ja).

ROMAJI_JA = True

_kakasi = None
_romaji_warned = False


def to_romaji(text: str) -> str:
    """Transliterate Japanese text to Hepburn romaji. Needs pykakasi; the
    original text is returned untouched when it is not installed."""
    global _kakasi, _romaji_warned
    if _kakasi is None:
        try:
            import pykakasi
        except ImportError:
            if not _romaji_warned:
                _romaji_warned = True
                print("warning: pykakasi is not installed; Japanese subtitles keep the native "
                      "script (pip install pykakasi)", file=sys.stderr)
            return text
        _kakasi = pykakasi.kakasi()
    parts = _kakasi.convert(text)
    romaji = " ".join(p["hepburn"].strip() for p in parts if p.get("hepburn", "").strip())
    return romaji or text


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


# ── Styled ASS subtitles ─────────────────────────────────────────────────────
# Mirrors the web app's subtitle visualizer: the same presets and knobs (font,
# size, color, background box + opacity, outline, position, alignment) are
# rendered into the [V4+ Styles] header, so a sidecar/muxed .ass looks like the
# web preview. Word animation ("karaoke") adds \k tags on top of the style.

# Web font stacks -> concrete font names that exist on typical player systems.
ASS_FONTS = {
    "sans": "Arial",
    "serif": "Georgia",
    "rounded": "Trebuchet MS",
    "condensed": "Arial Narrow",
    "mono": "Consolas",
}

# Baseline = the web app's default caption style.
DEFAULT_STYLE = {
    "font": "sans",           # key of ASS_FONTS, or a literal font name
    "size": 1.0,              # multiplier over the base size (72px @ 1080p)
    "color": "#ffffff",       # text color
    "bold": False,
    "italic": False,
    "align": "center",        # left / center / right
    "position": "bottom",     # top / middle / bottom
    "bg": True,               # opaque box behind the text
    "bg_color": "#06080b",
    "bg_opacity": 0.84,       # 0.0 transparent … 1.0 solid
    "outline": False,         # black outline when the box is disabled
    "highlight_color": "#b8f060",  # spoken-word color in karaoke mode
}

# Same presets as the web visualizer (values are overrides on DEFAULT_STYLE).
STYLE_PRESETS = {
    "default": {},
    "clean": {"bg": False, "outline": True},
    "bold": {"size": 1.12, "bold": True, "bg_color": "#000000", "bg_opacity": 1.0},
    "pop": {"font": "rounded", "size": 1.06, "color": "#fde047", "bold": True,
            "bg": False, "outline": True},
    "neon": {"color": "#b8f060", "bold": True, "bg_opacity": 0.55},
    "classic": {"font": "serif", "bg": False, "outline": True},
    "terminal": {"font": "mono", "size": 0.92, "bg_color": "#0a0d12", "bg_opacity": 0.9},
}


def _hex_to_ass(hex_color: str, opacity: float = 1.0) -> str:
    """#RRGGBB -> ASS &HAABBGGRR (AA: 00 = opaque, FF = fully transparent)."""
    h = str(hex_color or "#ffffff").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        n = int(h, 16)
    except ValueError:
        n = 0xFFFFFF
    r, g, b = (n >> 16) & 255, (n >> 8) & 255, n & 255
    a = max(0, min(255, round((1.0 - float(opacity)) * 255)))
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def build_ass_header(style: dict, karaoke: bool) -> str:
    font = ASS_FONTS.get(style.get("font", "sans"), str(style.get("font") or "Arial"))
    size = max(24, round(72 * float(style.get("size", 1.0))))
    color = _hex_to_ass(style.get("color", "#ffffff"))
    if karaoke:
        # SecondaryColour = not yet spoken, PrimaryColour = spoken (\k flips them).
        primary = _hex_to_ass(style.get("highlight_color", "#b8f060"))
        secondary = color
    else:
        primary, secondary = color, "&H00FFFFFF"
    if style.get("bg", True):
        border_style, outline_w, shadow = 3, 3, 0  # BorderStyle 3 = opaque box
        back = _hex_to_ass(style.get("bg_color", "#06080b"), float(style.get("bg_opacity", 0.84)))
    else:
        border_style, shadow = 1, 1
        outline_w = 3 if style.get("outline") else 1
        back = "&H80000000"
    base = {"bottom": 1, "middle": 4, "top": 7}.get(str(style.get("position", "bottom")), 1)
    offset = {"left": 0, "center": 1, "right": 2}.get(str(style.get("align", "center")), 1)
    alignment = base + offset
    bold = -1 if style.get("bold") else 0
    italic = -1 if style.get("italic") else 0
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{primary},{secondary},&H00101010,{back},{bold},{italic},0,0,100,100,0,0,{border_style},{outline_w},{shadow},{alignment},60,60,50,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def format_ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def _karaoke_text(seg: dict) -> str:
    words = seg.get("words") or []
    if not words:
        # Translated tracks carry no word timestamps: approximate each word's
        # duration by its share of the line's characters.
        tokens = [t for t in str(seg["text"]).split() if t]
        if not tokens:
            return _ass_escape(seg["text"])
        total_cs = max(len(tokens), round((seg["end"] - seg["start"]) * 100))
        weights = [len(t) + 1 for t in tokens]
        weight_sum = sum(weights)
        parts, used = [], 0
        for i, (token, weight) in enumerate(zip(tokens, weights)):
            dur = total_cs - used if i == len(tokens) - 1 else max(1, round(total_cs * weight / weight_sum))
            used += dur
            parts.append(f"{{\\k{dur}}}{_ass_escape(token)} ")
        return "".join(parts).rstrip()

    parts = []
    line_start = seg["start"]
    for i, word in enumerate(words):
        w_start = max(line_start, word["start"])
        if i == 0 and w_start > line_start:
            parts.append(f"{{\\k{round((w_start - line_start) * 100)}}}")
        # Extend each word until the next one starts so highlights never gap.
        w_end = words[i + 1]["start"] if i + 1 < len(words) else word["end"]
        dur = max(1, round((max(w_start, w_end) - w_start) * 100))
        parts.append(f"{{\\k{dur}}}{_ass_escape(word['text'])} ")
    return "".join(parts).rstrip()


def segments_to_ass(segments: list[dict], style: dict | None = None, karaoke: bool = True) -> str:
    lines = [build_ass_header(style or DEFAULT_STYLE, karaoke)]
    for seg in segments:
        text = _karaoke_text(seg) if karaoke else _ass_escape(seg["text"])
        lines.append(
            f"Dialogue: 0,{format_ass_time(seg['start'])},{format_ass_time(seg['end'])},"
            f"Default,,0,0,0,,{text}"
        )
    return "\n".join(lines) + "\n"


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
        out = run_child(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
            check=True,
        ).stdout
        tags = (json.loads(out).get("format", {}) or {}).get("tags", {}) or {}
        comment = next((v for k, v in tags.items() if k.lower() == "comment"), "")
        return SUBVID_TAG in str(comment)
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        return False


def decode_audio_mono_16k(path: Path):
    """Decode a media file's audio to 16 kHz mono float32 samples (numpy).

    Same job ffmpeg does inside faster-whisper; needed for whisper.cpp, which
    only accepts raw samples (or 16 kHz WAV files) as input. The child follows
    the lifetime guardrails above.
    """
    import numpy as np

    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vn",
           "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", "16000", "-"]
    kwargs: dict = {}
    if sys.platform.startswith("linux"):
        kwargs["preexec_fn"] = _bind_child_to_parent_linux
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    _adopt_child(proc)
    try:
        out, err = proc.communicate()
    except BaseException:
        proc.kill()
        raise
    finally:
        _forget_child(proc)
    if proc.returncode != 0:
        detail = err.decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"ffmpeg audio decode failed: {detail[-1] if detail else proc.returncode}")
    return np.frombuffer(out, dtype=np.float32)


def ffprobe_subtitle_stream_count(path: Path) -> int:
    try:
        out = run_child(
            ["ffprobe", "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=index", "-of", "json", str(path)],
            check=True,
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
        run_child(cmd, check=True)
        tmp.replace(output)
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg mux failed: {e.stderr.strip().splitlines()[-1] if e.stderr else e}") from e


def _ass_filter_path(path: Path) -> str:
    """Escape a filename for use inside an ffmpeg filter argument."""
    return (
        str(path)
        .replace("\\", "/")
        .replace(":", "\\:")
        .replace(",", "\\,")
        .replace("'", "\\'")
    )


def burn_subtitles(source: Path, ass_file: Path, output: Path) -> None:
    """Re-encode the video with the subtitles burned into the image (hard subs).

    This is the CLI equivalent of the web app's video export: the styled .ass
    is rendered onto every frame, so it plays anywhere — at the cost of a full
    re-encode (much slower than mux/sidecar and slightly lossy).
    """
    tmp = output.with_name(f"{output.name}.{uuid.uuid4().hex[:8]}.part")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-vf", f"ass={_ass_filter_path(ass_file)}",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-c:a", "copy",
        "-metadata", f"comment={SUBVID_TAG}",
        "-f", "mp4" if output.suffix.lower() == ".mp4" else "matroska",
        str(tmp),
    ]
    try:
        run_child(cmd, check=True)
        tmp.replace(output)
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg burn failed: {e.stderr.strip().splitlines()[-1] if e.stderr else e}") from e


class Transcriber:
    """Lazy, shared faster-whisper model (thread-safe: ctranslate2 releases the GIL)."""

    def __init__(self, model_name: str, device: str, compute_type: str):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.backend = "ct2"
        self._model = None
        self._lock = threading.Lock()
        # GPU inference (MLX / whisper.cpp) is serialized: the GPU is a single
        # shared queue and neither library makes thread-safety promises.
        self._gpu_lock = threading.Lock()

    def _load(self):
        device = self.device
        if device == "auto":
            if mlx_available():
                device = "mlx"
            elif not ct2_cuda_available() and vulkan_available():
                # No NVIDIA GPU, but pywhispercpp has a Vulkan/SYCL/HIP build:
                # use the AMD/Intel GPU instead of falling back to CPU.
                device = "vulkan"
        if device == "vulkan":
            return self._load_whispercpp()
        if device == "mlx":
            if not is_apple_silicon():
                raise RuntimeError("--device mlx requires an Apple Silicon Mac")
            try:
                import mlx_whisper  # noqa: F401
            except ImportError as e:
                raise RuntimeError(
                    "mlx-whisper is not installed — run: pip install mlx-whisper"
                ) from e
            self.backend = "mlx"
            repo = MLX_WHISPER_REPOS.get(self.model_name, self.model_name)
            log("model", f"using MLX backend on the Apple Silicon GPU: '{repo}'")
            return repo

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
            device = "cuda" if ct2_cuda_available() else "cpu"
            if device == "cpu" and not sys.platform == "darwin":
                log("model", "no GPU detected — running on CPU. If this machine has an "
                             "AMD/Intel GPU, install pywhispercpp with Vulkan and use "
                             "--device vulkan (see cli/requirements.txt)")
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
        model = self.model  # lazy load; also resolves self.backend
        if self.backend == "mlx":
            return self._transcribe_mlx(
                model, path, language, task, name, vad_options, on_progress, cancel_check,
            )
        if self.backend == "whispercpp":
            return self._transcribe_whispercpp(
                model, path, language, task, name, vad_options, on_progress, cancel_check,
            )
        segments_iter, info = model.transcribe(
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

    def _transcribe_mlx(
        self,
        repo: str,
        path: Path,
        language: str | None,
        task: str,
        name: str,
        vad_options: dict | None,
        on_progress,
        cancel_check,
    ) -> tuple[str, list[dict]]:
        """Apple Silicon GPU path via mlx-whisper.

        mlx-whisper returns the whole result at once (no segment iterator), so
        progress is coarse and a cancel only takes effect between files.
        """
        import mlx_whisper

        if vad_options is not None:
            log(name, "note: VAD filtering is not supported on the MLX backend; continuing without it")
        if cancel_check and cancel_check():
            raise JobCancelled()
        started = time.monotonic()
        with self._gpu_lock:
            result = mlx_whisper.transcribe(
                str(path),
                path_or_hf_repo=repo,
                language=language,
                task=task,
                word_timestamps=True,
                condition_on_previous_text=False,
                verbose=None,
            )
        if cancel_check and cancel_check():
            raise JobCancelled()

        detected = result.get("language") or language or "und"
        segments: list[dict] = []
        words: list[dict] = []
        for seg in result.get("segments") or []:
            text = str(seg.get("text", "")).strip()
            if not text:
                continue
            segments.append({"start": seg["start"], "end": seg["end"], "text": text})
            for w in seg.get("words") or []:
                word_text = str(w.get("word", "")).strip()
                if word_text:
                    words.append({"start": w["start"], "end": w["end"], "text": word_text})

        duration = segments[-1]["end"] if segments else 0.0
        elapsed = time.monotonic() - started
        speed = duration / elapsed if elapsed else 0.0
        log(name, f"language={detected}, duration={duration/60:.1f} min ({speed:.1f}x realtime on MLX)")
        if on_progress:
            on_progress(100, speed)
        if words:
            segments = words_to_lines(words)
        return detected, segments

    def _load_whispercpp(self):
        """AMD/Intel GPU backend: whisper.cpp via pywhispercpp (Vulkan/SYCL/HIP)."""
        try:
            from pywhispercpp.model import Model as WhisperCppModel
        except ImportError as e:
            raise RuntimeError(
                "--device vulkan requires pywhispercpp — install it with Vulkan support "
                "(see the AMD/Intel section in cli/requirements.txt)"
            ) from e

        gpu = _whispercpp_gpu_flags()
        if gpu:
            log("model", f"using whisper.cpp backend on the GPU ({', '.join(sorted(gpu))})")
        else:
            log("model", "warning: this pywhispercpp build reports no GPU backend and will "
                         "run on CPU — reinstall with GGML_VULKAN=1 (see cli/requirements.txt)")

        self.backend = "whispercpp"
        log("model", f"loading '{self.model_name}' (ggml)…")
        try:
            return WhisperCppModel(
                self.model_name,
                print_realtime=False,
                print_progress=False,
            )
        except Exception as e:
            raise RuntimeError(
                f"whisper.cpp could not load '{self.model_name}': {e}. Use a standard "
                "model name (tiny/base/small/medium/large-v3/large-v3-turbo) or a path "
                "to a ggml .bin file."
            ) from e

    def _transcribe_whispercpp(
        self,
        model,
        path: Path,
        language: str | None,
        task: str,
        name: str,
        vad_options: dict | None,
        on_progress,
        cancel_check,
    ) -> tuple[str, list[dict]]:
        """AMD/Intel GPU path via whisper.cpp.

        Word-level timing comes from token timestamps with one word per
        segment (max_len=1 + split_on_word), then the usual line regrouping.
        A cancel only takes effect between files.
        """
        if vad_options is not None:
            log(name, "note: VAD filtering is not supported on the whisper.cpp backend; continuing without it")
        if cancel_check and cancel_check():
            raise JobCancelled()

        # whisper.cpp only takes raw 16 kHz samples, so decode with ffmpeg
        # ourselves (this also gives us the duration for progress reporting).
        audio = decode_audio_mono_16k(path)
        duration = len(audio) / 16000.0
        started = time.monotonic()
        progress = {"next": 10}

        def on_new_segment(seg) -> None:
            if not duration:
                return
            end = seg.t1 / 100.0
            pct = min(100.0, end / duration * 100)
            elapsed = time.monotonic() - started
            speed = end / elapsed if elapsed else 0.0
            if on_progress:
                on_progress(pct, speed)
            if pct >= progress["next"]:
                log(name, f"transcribing… {pct:.0f}% ({speed:.1f}x realtime)")
                progress["next"] = (int(pct // 10) + 1) * 10

        with self._gpu_lock:
            raw_segments = model.transcribe(
                audio,
                language=language or "auto",
                translate=(task == "translate"),
                token_timestamps=True,
                split_on_word=True,
                max_len=1,
                new_segment_callback=on_new_segment,
                abort_callback=(lambda: bool(cancel_check())) if cancel_check else None,
            )
        if cancel_check and cancel_check():
            raise JobCancelled()

        detected = language or "und"
        if not language:
            # whisper.cpp keeps the detected language on its context; reading it
            # is best-effort across pywhispercpp versions.
            try:
                import _pywhispercpp as _pw
                detected = _pw.whisper_lang_str(_pw.whisper_full_lang_id(model._ctx)) or detected
            except Exception:
                pass

        words: list[dict] = []
        for seg in raw_segments:
            text = str(seg.text).strip()
            if text:
                words.append({"start": seg.t0 / 100.0, "end": seg.t1 / 100.0, "text": text})

        segments = words_to_lines(words) if words else []
        total = duration or (segments[-1]["end"] if segments else 0.0)
        elapsed = time.monotonic() - started
        speed = total / elapsed if elapsed else 0.0
        log(name, f"language={detected}, duration={total/60:.1f} min ({speed:.1f}x realtime on whisper.cpp)")
        if on_progress:
            on_progress(100, speed)
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
        # CTranslate2 only accelerates on CUDA (no Metal/Vulkan backend): when
        # transcription runs on mlx or vulkan, NLLB falls back to CPU int8
        # (NEON/Accelerate or AVX2/oneDNN).
        if device in ("auto", "mlx", "vulkan"):
            device = "cuda" if ct2_cuda_available() else "cpu"
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
        if tgt == "ja" and ROMAJI_JA:
            out = [to_romaji(text) for text in out]
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
        run_child(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(path), "-map", f"0:a:{track}",
             "-ac", "1", "-ar", "16000", str(tmp)],
            check=True,
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

    # Word animation and custom styling write .ass files, which MP4 cannot
    # embed (only MKV can).
    use_karaoke = bool(getattr(args, "word_animation", False))
    use_ass = use_karaoke or bool(getattr(args, "styled_ass", False))

    # "auto" keeps the original container when it can hold the subtitles
    # (MP4 uses mov_text, MKV uses SRT); anything else is remuxed into MKV,
    # the safest container for subtitle tracks. Styled/karaoke tracks need
    # ASS, so auto switches MP4 sources to MKV instead of silently muxing a
    # plain-text track; forcing --container mp4 keeps the old behavior.
    if args.container == "auto":
        ext = path.suffix.lower()
        container = "mp4" if ext in (".mp4", ".m4v", ".mov") and not use_ass else "mkv"
    else:
        container = args.container

    # With an explicit MP4 container the muxed track falls back to plain
    # text (the sidecar stays .ass).
    mux_ass = use_ass and container != "mp4"
    sub_ext = ".ass" if use_ass else ".srt"
    mux_ext = ".ass" if mux_ass else ".srt"

    # Predict output paths (language becomes known after detection, so for
    # skip-checks we use the forced language when given, else probe for any).
    def srt_path(lang: str) -> Path:
        return out_dir / f"{path.stem}.{lang}{sub_ext}"

    def mux_path() -> Path:
        ext = f".{container}"
        candidate = out_dir / f"{path.stem}{ext}"
        if candidate.resolve() == path.resolve():
            candidate = out_dir / f"{path.stem}.subs{ext}"
        return candidate

    def has_sidecar() -> bool:
        prefix = f"{path.stem}."
        return any(
            p.name.startswith(prefix) and p.suffix.lower() in (".srt", ".ass")
            for p in out_dir.iterdir()
        )

    if not args.overwrite:
        srt_exists = has_sidecar()
        mux_exists = mux_path().exists()
        if mode == "sidecar" and srt_exists:
            emit("skipped", 100, "sidecar exists")
            return f"skipped (sidecar exists): {name}"
        if mode in ("mux", "burn") and mux_exists:
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

    if use_ass and mode in ("mux", "both") and not is_audio and not mux_ass:
        log(name, "note: MP4 cannot embed .ass; the embedded track will be plain text")

    def track_content(track_segments: list[dict], ext: str) -> str:
        if ext == ".ass":
            return segments_to_ass(track_segments, style=getattr(args, "sub_style", None), karaoke=use_karaoke)
        return segments_to_srt(track_segments)

    srt_files: list[tuple[str, Path]] = []
    temp_srts: list[Path] = []
    for track_lang, track_segments in tracks:
        sidecar: Path | None = None
        if mode in ("sidecar", "both"):
            sidecar = srt_path(track_lang)
            sidecar.write_text(track_content(track_segments, sub_ext), encoding="utf-8")
            log(name, f"sidecar written: {sidecar.name}")
        if mode not in ("mux", "both") or is_audio:
            continue
        if sidecar is not None and sub_ext == mux_ext:
            srt_files.append((track_lang, sidecar))
        else:
            tmp = out_dir / f".{path.stem}.{track_lang}.{uuid.uuid4().hex[:6]}.tmp{mux_ext}"
            tmp.write_text(track_content(track_segments, mux_ext), encoding="utf-8")
            temp_srts.append(tmp)
            srt_files.append((track_lang, tmp))

    try:
        if mode in ("mux", "both") and not is_audio:
            emit("muxing", 96)
            target = mux_path()
            log(name, f"muxing {len(srt_files)} subtitle track(s) into {target.name} (stream copy)…")
            mux_subtitles(path, srt_files, target, args.default_track)
            log(name, f"done: {target.name}")
        elif mode == "burn" and not is_audio:
            emit("burning", 96)
            target = mux_path()
            # Burn a single track: the first requested translation when --to
            # was given (that's the language the user wants to read), else the
            # original-language transcription.
            burn_lang, burn_segments = tracks[1] if len(tracks) > 1 else tracks[0]
            if len(tracks) > 2:
                log(name, f"burn mode renders one track only: burning '{burn_lang}'")
            tmp_ass = out_dir / f".{path.stem}.{burn_lang}.{uuid.uuid4().hex[:6]}.burn.ass"
            tmp_ass.write_text(
                segments_to_ass(burn_segments, style=getattr(args, "sub_style", None), karaoke=use_karaoke),
                encoding="utf-8",
            )
            log(name, f"burning '{burn_lang}' subtitles into {target.name} (re-encode, this is slow)…")
            try:
                burn_subtitles(path, tmp_ass, target)
            finally:
                tmp_ass.unlink(missing_ok=True)
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
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu", "mlx", "vulkan"],
                        help="inference device (cuda = NVIDIA GPU, mlx = Apple Silicon GPU, "
                             "vulkan = AMD/Intel GPU via whisper.cpp; auto picks the best one available)")
    parser.add_argument("--compute-type", default="auto", help="ctranslate2 compute type (auto/float16/int8/int8_float16)")
    parser.add_argument("--mode", default="sidecar", choices=["mux", "sidecar", "both", "burn"],
                        help="mux: new file with embedded toggleable track; sidecar: .srt next to the video "
                             "(Jellyfin auto-detects it); both; burn: re-encode with the styled subtitles "
                             "burned into the image (plays anywhere, but VERY SLOW and slightly lossy)")
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
    parser.add_argument("--word-animation", action="store_true",
                        help="karaoke-style .ass subtitles: each word lights up as it is spoken "
                             "(needs an ASS-capable player; MP4 muxes fall back to plain text)")

    style = parser.add_argument_group(
        "subtitle style",
        "visual style of the subtitles, mirroring the web visualizer. Any of these "
        "options switches the output to styled .ass files (SRT cannot carry styling); "
        "they also drive --mode burn and --word-animation. Command-line values "
        "override the [style] section of the config file.",
    )
    style.add_argument("--style", default=None, choices=sorted(STYLE_PRESETS),
                       help="style preset from the web app (default/clean/bold/pop/neon/classic/terminal)")
    style.add_argument("--sub-font", default=None, metavar="FONT",
                       help="font: sans/serif/rounded/condensed/mono, or a literal font name (e.g. 'Verdana')")
    style.add_argument("--sub-size", type=float, default=None, metavar="X",
                       help="size multiplier (1.0 = normal, like the web slider)")
    style.add_argument("--sub-color", default=None, metavar="#RRGGBB", help="text color")
    style.add_argument("--sub-bold", action=argparse.BooleanOptionalAction, default=None, help="bold text")
    style.add_argument("--sub-italic", action=argparse.BooleanOptionalAction, default=None, help="italic text")
    style.add_argument("--sub-align", default=None, choices=["left", "center", "right"], help="text alignment")
    style.add_argument("--sub-position", default=None, choices=["top", "middle", "bottom"],
                       help="vertical position on screen")
    style.add_argument("--sub-bg", action=argparse.BooleanOptionalAction, default=None,
                       help="box behind the text (--no-sub-bg removes it)")
    style.add_argument("--sub-bg-color", default=None, metavar="#RRGGBB", help="box color")
    style.add_argument("--sub-bg-opacity", type=float, default=None, metavar="0.0-1.0",
                       help="box opacity: 0.0 fully transparent … 1.0 solid")
    style.add_argument("--sub-outline", action=argparse.BooleanOptionalAction, default=None,
                       help="black outline around the text (used when the box is off)")
    style.add_argument("--sub-highlight-color", default=None, metavar="#RRGGBB",
                       help="spoken-word color for --word-animation karaoke")
    style.add_argument("--ass", dest="force_ass", action="store_true",
                       help="write .ass instead of .srt even without style changes or karaoke")
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
    global ROMAJI_JA
    ROMAJI_JA = bool(tx_cfg.get("romaji_ja", True))

    # Subtitle line shaping from [lines].
    lines_cfg = config.get("lines", {})
    global MAX_LINE_CHARS, MAX_LINE_WORDS, MAX_LINE_SECONDS, WORD_GAP_BREAK
    MAX_LINE_CHARS = int(lines_cfg.get("max_chars", MAX_LINE_CHARS))
    MAX_LINE_WORDS = int(lines_cfg.get("max_words", MAX_LINE_WORDS))
    MAX_LINE_SECONDS = float(lines_cfg.get("max_seconds", MAX_LINE_SECONDS))
    WORD_GAP_BREAK = float(lines_cfg.get("word_gap_break", WORD_GAP_BREAK))

    # Visual style: preset -> [style] config section -> individual CLI flags,
    # each layer overriding the previous one (like the web visualizer, where
    # touching a knob after picking a preset customizes it).
    style_cfg = dict(config.get("style", {}))
    resolved = dict(DEFAULT_STYLE)
    preset = args.style if args.style is not None else str(style_cfg.get("preset", "") or "")
    if preset:
        if preset in STYLE_PRESETS:
            resolved.update(STYLE_PRESETS[preset])
        else:
            print(f"warning: unknown style preset '{preset}' ignored "
                  f"(available: {', '.join(sorted(STYLE_PRESETS))})", file=sys.stderr)
    for key in DEFAULT_STYLE:
        if key in style_cfg:
            resolved[key] = style_cfg[key]
    cli_style = {
        "font": args.sub_font,
        "size": args.sub_size,
        "color": args.sub_color,
        "bold": args.sub_bold,
        "italic": args.sub_italic,
        "align": args.sub_align,
        "position": args.sub_position,
        "bg": args.sub_bg,
        "bg_color": args.sub_bg_color,
        "bg_opacity": args.sub_bg_opacity,
        "outline": args.sub_outline,
        "highlight_color": args.sub_highlight_color,
    }
    for key, value in cli_style.items():
        if value is not None:
            resolved[key] = value
    resolved["size"] = max(0.5, min(2.0, float(resolved.get("size", 1.0))))
    resolved["bg_opacity"] = max(0.0, min(1.0, float(resolved.get("bg_opacity", 0.84))))
    args.sub_style = resolved
    # A customized style can only travel in .ass files, so it switches the
    # sidecar/mux format automatically (also via the explicit --ass flag, or
    # when a preset was picked on purpose — even the "default" one).
    args.styled_ass = bool(args.force_ass) or bool(preset) or resolved != DEFAULT_STYLE


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
    _install_terminate_guards()

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
        # os._exit skips atexit, so reap the ffmpeg children explicitly first.
        _kill_children()
        os._exit(130)

    print(f"\nFinished in {(time.monotonic() - t0)/60:.1f} min — {len(files) - failures} ok, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
