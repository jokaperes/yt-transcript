#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

logger = logging.getLogger(__name__)

LOG_FILE = Path("transcribe_youtube.log")

DEFAULT_MODEL = "large-v3-turbo"
DOWNLOAD_TIMEOUT_SECONDS = 900
ProgressCallback = Callable[[str, float | None, str], None]
OUTPUT_FORMATS = ("md", "txt", "srt", "vtt", "json")
URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?.*v=|shorts/|embed/|live/)|youtu\.be/)[\w\-]{11}"
)


def parse_urls(text: str) -> list[str]:
    lines = text.strip().splitlines()
    urls: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        matches = URL_PATTERN.findall(line)
        if matches:
            for m in matches:
                if m not in urls:
                    urls.append(m)
        elif line.startswith("http"):
            if line not in urls:
                urls.append(line)
    return urls


def setup_logging() -> None:
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.basicConfig(handlers=[file_handler, stream_handler], level=logging.INFO)


def log_info(message: str) -> None:
    logger.info(message)


def log_error(message: str) -> None:
    logger.error(message)


def run(command: list[str], capture_output: bool = False) -> str:
    try:
        completed = subprocess.run(command, check=True, capture_output=capture_output, text=True)
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing command: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() if exc.stderr else exc.stdout.strip() if exc.stdout else ""
        message = f"Command failed with exit code {exc.returncode}: {' '.join(command)}"
        if details:
            message = f"{message}\n\n{details}"
        raise SystemExit(message) from exc
    return completed.stdout.strip() if capture_output else ""


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value)
    value = re.sub(r"[^\w\s.-]", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.rstrip(" .")
    return value[:100] or "youtube-video"


def find_ytdlp() -> list[str]:
    # Frozen build: the yt_dlp module is bundled, so re-invoke our own exe as a
    # yt-dlp runner (see the "--run-ytdlp" dispatch in yt_transcript_gui.py).
    # This avoids shipping a separate ~18 MB standalone yt-dlp.exe.
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-ytdlp"]
    bundled_ytdlp = Path(sys.executable).with_name("yt-dlp.exe")
    ytdlp_binary = str(bundled_ytdlp) if bundled_ytdlp.exists() else shutil.which("yt-dlp")
    if ytdlp_binary:
        return [ytdlp_binary]
    return [sys.executable, "-m", "yt_dlp"]


def check_dependencies() -> list[str]:
    issues: list[str] = []
    if not shutil.which("ffmpeg"):
        issues.append("ffmpeg was not found on PATH. Install FFmpeg and reopen the app.")
    command = find_ytdlp()
    # A single-element command is a discovered binary path; verify it exists.
    if len(command) == 1 and not Path(command[0]).exists():
        issues.append("yt-dlp was not found.")
    return issues


def normalize_cookies_file(cookies: str | None, output_dir: Path, log: Callable[[str], None] | None = None) -> str | None:
    if not cookies:
        return None

    source = Path(cookies).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"Cookies file does not exist: {source}")

    text = source.read_text(encoding="utf-8", errors="replace")
    # A dot-prefixed domain with the include-subdomains flag set to FALSE is
    # malformed Netscape output (old exporter bug). Fix the flag rather than
    # stripping the dot: a host-only "youtube.com" cookie would never be sent
    # to www.youtube.com, silently breaking authentication.
    fixed = re.sub(r"^(\.[^\t]+)\tFALSE\t", r"\1\tTRUE\t", text, flags=re.MULTILINE)
    if fixed == text:
        return str(source)

    output_dir.mkdir(parents=True, exist_ok=True)
    normalized = output_dir / "normalized-youtube-cookies.txt"
    # yt-dlp rotates YouTube account cookies on every download and saves the
    # new values back into the file it is given. Regenerating this copy from
    # the (stale) original on each run would discard those rotated values and
    # invalidate the account cookies after the first download. Reuse the copy
    # unless the original export is newer (i.e. a fresh export).
    if normalized.exists() and normalized.stat().st_mtime > source.stat().st_mtime:
        if log:
            log(f"Reusing rotated YouTube cookies: {normalized}")
        return str(normalized)
    normalized.write_text(fixed, encoding="utf-8")
    if log:
        log(f"Normalized YouTube cookies format: {normalized}")
    return str(normalized)


CUDA_RUNTIME_PACKAGES = ("nvidia-cublas-cu12", "nvidia-cudnn-cu12")
# cudnn64_9.dll is just a loader shim (the ctranslate2 wheel even bundles its
# own copy), so probing it can succeed while the real libraries are missing.
# Probe the actual workhorse DLLs instead.
CUDA_PROBE_DLLS = ("cublas64_12.dll", "cudnn_ops64_9.dll")


def cuda_runtime_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "yt-transcript" / "cuda"
    return Path.home() / ".cache" / "yt-transcript" / "cuda"


def _wire_cuda_dir(bin_dir: Path) -> None:
    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(bin_dir))


def _cuda_dlls_loadable() -> bool:
    import ctypes

    try:
        for name in CUDA_PROBE_DLLS:
            ctypes.WinDLL(name)
    except OSError:
        return False
    return True


def _latest_windows_wheel(package: str) -> tuple[str, int]:
    import urllib.request

    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json", timeout=30) as response:
        data = json.load(response)
    for entry in data["urls"]:
        if entry["filename"].endswith("win_amd64.whl"):
            return entry["url"], int(entry.get("size") or 0)
    raise RuntimeError(f"No Windows wheel found on PyPI for {package}")


def _download_file(url: str, dest: Path, size: int, log: Callable[[str], None]) -> None:
    import urllib.request

    done = 0
    next_report = 10
    with urllib.request.urlopen(url, timeout=60) as response, open(dest, "wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if size and done * 100 / size >= next_report:
                log(f"  downloaded {done / 1048576:.0f} of {size / 1048576:.0f} MB")
                next_report += 10


def _extract_wheel_dlls(wheel_path: Path, bin_dir: Path) -> int:
    import zipfile

    bin_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(wheel_path) as wheel:
        for member in wheel.namelist():
            if member.lower().endswith(".dll"):
                target = bin_dir / Path(member).name
                with wheel.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                count += 1
    return count


def ensure_cuda_runtime(log: Callable[[str], None] | None = None) -> bool:
    """Make cuBLAS/cuDNN loadable on Windows, downloading NVIDIA's runtime
    wheels on first GPU use. Returns False when CUDA cannot be used."""
    if sys.platform != "win32":
        return True
    log = log or log_info
    if _cuda_dlls_loadable():
        return True
    bin_dir = cuda_runtime_dir() / "bin"
    if bin_dir.exists() and any(bin_dir.glob("cublas64_*.dll")):
        _wire_cuda_dir(bin_dir)
        if _cuda_dlls_loadable():
            return True
    log(
        "NVIDIA CUDA runtime (cuBLAS/cuDNN) not found. Downloading it now — "
        f"about 1.2 GB, one time only, kept in {bin_dir.parent}"
    )
    try:
        for package in CUDA_RUNTIME_PACKAGES:
            url, size = _latest_windows_wheel(package)
            log(f"Downloading {package} ({size / 1048576:.0f} MB)")
            bin_dir.parent.mkdir(parents=True, exist_ok=True)
            wheel_path = bin_dir.parent / f"{package}.whl"
            _download_file(url, wheel_path, size, log)
            extracted = _extract_wheel_dlls(wheel_path, bin_dir)
            wheel_path.unlink()
            log(f"Installed {extracted} DLLs from {package}")
    except Exception as exc:
        log(f"CUDA runtime download failed: {exc}")
        return False
    _wire_cuda_dir(bin_dir)
    if _cuda_dlls_loadable():
        log("CUDA runtime ready.")
        return True
    log("CUDA runtime is still not loadable after the download.")
    return False


def _parse_ytdlp_line(line: str) -> tuple[float | None, str]:
    download_match = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
    if download_match:
        return float(download_match.group(1)), "download"
    if line.startswith("[download]"):
        return None, "download"
    if line.startswith("[ExtractAudio]"):
        return None, "extract"
    if line.startswith("[postprocess]"):
        return None, "extract"
    if line.startswith("[youtube]"):
        return None, "download"
    return None, ""


DOWNLOAD_CACHE_NAME = ".yt-transcript-cache"


def download_cache_dir(output_dir: Path) -> Path:
    return output_dir / DOWNLOAD_CACHE_NAME


def purge_download_cache(output_dir: Path, log: Callable[[str], None] | None = None) -> int:
    """Delete leftover downloaded audio/info files from previous runs, including
    ones a native abort() killed before per-video cleanup could run. Safe to call
    at startup. Returns how many files were removed."""
    cache = download_cache_dir(output_dir)
    if not cache.exists():
        return 0
    removed = 0
    for item in cache.iterdir():
        try:
            if item.is_file():
                item.unlink()
                removed += 1
        except Exception:
            pass
    if removed and log:
        log(f"Cleaned {removed} leftover download file(s) from a previous run.")
    return removed


def download_audio(
    url: str,
    output_dir: Path,
    cookies: str | None,
    cookies_from_browser: str | None,
    log: Callable[[str], None] | None = None,
    progress: ProgressCallback | None = None,
    stop_requested: Callable[[], bool] | None = None,
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
) -> tuple[Path, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = download_cache_dir(output_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    template = str(cache_dir / "%(title).120s [%(id)s].%(ext)s")
    log = log or log_info
    progress = progress or (lambda phase, percent, detail: None)
    stop_requested = stop_requested or (lambda: False)
    cookies = normalize_cookies_file(cookies, output_dir, log)

    ytdlp_cmd = find_ytdlp()
    args: list[str] = [
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "--newline",
        "--socket-timeout", "30",
        "--retries", "3",
        "--fragment-retries", "3",
        "--progress-template",
        "download:[download] %(progress._percent_str)s of %(progress._total_bytes_str)s at %(progress._speed_str)s ETA %(progress._eta_str)s",
        "--progress-template",
        "postprocess:[postprocess] %(progress.status)s %(info.id)s",
        "--no-playlist",
        "-f", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--write-info-json",
        "--print", "after_move:filepath",
        "-o", template,
    ]
    if cookies:
        args = ["--cookies", cookies] + args
    if cookies_from_browser:
        args = ["--cookies-from-browser", cookies_from_browser] + args

    command = ytdlp_cmd + args + [url]

    printed_paths: list[str] = []
    output_lines: list[str] = []
    log("Starting yt-dlp")
    progress("download", None, "Starting yt-dlp")
    log(" ".join(command))

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing command: {command[0]}") from exc

    started_at = time.monotonic()
    last_output_at = [started_at]
    last_heartbeat_at = started_at
    line_queue: Queue[str] = Queue()
    assert process.stdout is not None

    def read_output() -> None:
        for output_line in process.stdout:
            line_queue.put(output_line)

    threading.Thread(target=read_output, daemon=True).start()

    def _process_line(clean: str) -> None:
        if not clean:
            return
        last_output_at[0] = time.monotonic()
        output_lines.append(clean)
        log(clean)
        percent, phase = _parse_ytdlp_line(clean)
        if phase:
            progress(phase, percent, clean)
        if not clean.startswith("[") and not clean.startswith("WARNING:") and not clean.startswith("ERROR:"):
            printed_paths.append(clean)

    while True:
        try:
            line = line_queue.get(timeout=0.2)
            _process_line(line.rstrip())
        except Empty:
            pass

        if process.poll() is not None:
            while True:
                try:
                    _process_line(line_queue.get_nowait().rstrip())
                except Empty:
                    break
            break

        if stop_requested():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise SystemExit("Download cancelled.")

        if time.monotonic() - started_at > timeout_seconds:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise SystemExit(
                f"yt-dlp took more than {timeout_seconds // 60} minutes. "
                "This usually means YouTube is blocking the request, cookies are expired, or the network is stuck."
            )

        now = time.monotonic()
        if now - last_output_at[0] > 10 and now - last_heartbeat_at > 10:
            elapsed = int(now - started_at)
            log(f"Still waiting for yt-dlp output... {elapsed}s elapsed")
            estimated = min(12.0, elapsed / max(timeout_seconds, 1) * 100)
            progress("download", estimated, f"Waiting for yt-dlp output ({elapsed}s)")
            last_heartbeat_at = now

    if process.returncode != 0:
        details = "\n".join(output_lines[-80:])
        auth_markers = (
            "sign in to confirm",
            "cookies are no longer valid",
            "account cookies are no longer valid",
            "log in for access",
            "use --cookies",
        )
        lowered = details.lower()
        hint = ""
        if any(marker in lowered for marker in auth_markers):
            hint = (
                "\n\nYouTube is refusing the request, which usually means the cookies file "
                "expired or was rotated. Re-export it the durable way: in an incognito window, "
                "log in with a spare YouTube account, open youtube.com/robots.txt, export the "
                "cookies, close the window, and never reuse that account in a browser."
            )
        raise SystemExit(f"yt-dlp failed with exit code {process.returncode}.\n\n{details}{hint}")

    if not printed_paths:
        raise SystemExit("yt-dlp finished, but did not report an audio file path.")

    audio_path = Path(printed_paths[-1]).resolve()
    if not audio_path.exists():
        candidates = sorted(cache_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            files = "\n".join(p.name for p in cache_dir.glob("*"))
            raise SystemExit(
                f"yt-dlp reported an audio path, but it does not exist: {audio_path}\n\n"
                f"Files currently in output folder:\n{files}"
            )
        fallback_path = candidates[0].resolve()
        log(f"Reported audio path was not found. Using newest MP3 instead: {fallback_path}")
        audio_path = fallback_path

    info_path = audio_path.with_suffix(".info.json")
    info: dict[str, Any] = {}
    if info_path.exists():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        try:
            info_path.unlink()  # already parsed into `info`; don't leave it on disk
        except Exception:
            pass
    progress("download", 100.0, "Audio downloaded")

    return audio_path, info


def timestamp(seconds: float, vtt: bool = False) -> str:
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    separator = "." if vtt else ","
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"


def write_txt(path: Path, segments: list[dict[str, Any]]) -> None:
    paragraphs: list[str] = []
    current: list[str] = []
    prev_end: float | None = None
    for seg in segments:
        start_ts = timestamp(seg["start"])
        line = seg["text"].strip()
        gap = (seg["start"] - prev_end) > 2.0 if prev_end is not None else False
        if gap and current:
            paragraphs.append(" ".join(current))
            current = []
        if line:
            current.append(f"[{start_ts}] {line}")
        prev_end = seg.get("end", seg["start"])
    if current:
        paragraphs.append(" ".join(current))
    text = "\n\n".join(paragraphs)
    if not text.strip():
        text = " ".join(seg["text"].strip() for seg in segments).strip()
    path.write_text(text.strip() + "\n", encoding="utf-8")


def write_srt(path: Path, segments: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        lines.append(str(index))
        lines.append(f"{timestamp(segment['start'])} --> {timestamp(segment['end'])}")
        lines.append(segment["text"].strip())
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_vtt(path: Path, segments: list[dict[str, Any]]) -> None:
    lines = ["WEBVTT", ""]
    for segment in segments:
        lines.append(f"{timestamp(segment['start'], vtt=True)} --> {timestamp(segment['end'], vtt=True)}")
        lines.append(segment["text"].strip())
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_json(path: Path, segments: list[dict[str, Any]], info: dict[str, Any]) -> None:
    data = {
        "segments": segments,
        "info": {k: info.get(k) for k in ("title", "id", "duration", "uploader", "upload_date") if k in info},
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_md(path: Path, segments: list[dict[str, Any]], info: dict[str, Any]) -> None:
    # Markdown meant to be fed to an AI: a metadata header for context, then
    # paragraphs carrying a single start timestamp each (one per segment would
    # just burn the model's attention on noise).
    lines = [f"# {info.get('title') or path.stem}", ""]
    if info.get("uploader"):
        lines.append(f"- **Channel:** {info['uploader']}")
    if info.get("id"):
        lines.append(f"- **URL:** https://www.youtube.com/watch?v={info['id']}")
    upload_date = info.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8:
        lines.append(f"- **Uploaded:** {upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}")
    if info.get("duration"):
        lines.append(f"- **Duration:** {timestamp(float(info['duration']))[:8]}")
    if lines[-1] != "":
        lines.append("")
    lines.extend(["## Transcript", ""])

    paragraph: list[str] = []
    paragraph_start = 0.0
    prev_end: float | None = None

    def flush() -> None:
        if paragraph:
            lines.append(f"**[{timestamp(paragraph_start)[:8]}]** " + " ".join(paragraph))
            lines.append("")
            paragraph.clear()

    for seg in segments:
        text = seg["text"].strip()
        start = seg["start"]
        long_gap = prev_end is not None and (start - prev_end) > 2.0
        too_long = bool(paragraph) and (start - paragraph_start) > 60.0
        if long_gap or too_long:
            flush()
        if text:
            if not paragraph:
                paragraph_start = start
            paragraph.append(text)
        prev_end = seg.get("end", start)
    flush()

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def transcribe_openai(
    audio_path: Path,
    model_name: str,
    task: str,
    language: str,
    device: str | None,
    log: Callable[[str], None] | None = None,
    progress: ProgressCallback | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    log = log or log_info
    progress = progress or (lambda phase, percent, detail: None)
    stop_requested = stop_requested or (lambda: False)

    progress("model", None, "Loading openai-whisper model")

    import whisper

    model = whisper.load_model(model_name, device=device)
    progress("model", 50.0, "Model loaded, starting transcription")
    if stop_requested():
        raise SystemExit("Transcription cancelled.")
    result = model.transcribe(str(audio_path), language=language, task=task, fp16=device == "cuda")
    return result["text"].strip(), result.get("segments", [])


def _diag_file() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return base / "yt-transcript-gpu-diag.log"


def write_diag(text: str) -> None:
    """Append to the GPU diagnostics file, flushed to disk so it survives a
    native abort() that would otherwise discard buffered UI/log output."""
    try:
        with open(_diag_file(), "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        pass


def gpu_diagnostics(device: str, compute_type: str, model_name: str) -> str:
    """Collect what the GPU and the ctranslate2 build actually support. The
    supported-compute-types line is the decisive signal: if 'float16' is missing
    on a CUDA device, the wheel has no working kernels for this GPU (e.g. a
    Blackwell sm_120 card on a wheel built without sm_120 kernels)."""
    lines = [f"requested: device={device} compute_type={compute_type} model={model_name}"]
    try:
        import ctranslate2

        lines.append(f"ctranslate2 version: {ctranslate2.__version__}")
        try:
            lines.append(f"ctranslate2 CUDA device count: {ctranslate2.get_cuda_device_count()}")
        except Exception as exc:
            lines.append(f"get_cuda_device_count failed: {exc}")
        try:
            supported = ctranslate2.get_supported_compute_types("cuda", 0)
            lines.append(f"CUDA supported compute types: {sorted(supported)}")
        except Exception as exc:
            lines.append(f"get_supported_compute_types(cuda) failed: {exc}")
    except Exception as exc:
        lines.append(f"import ctranslate2 failed: {exc}")
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run(
                [smi, "--query-gpu=name,compute_cap,driver_version,memory.total", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            lines.append(f"nvidia-smi: {(out.stdout or out.stderr).strip()}")
        except Exception as exc:
            lines.append(f"nvidia-smi failed: {exc}")
    else:
        lines.append("nvidia-smi not found")
    return "\n".join(lines)


def gpu_compute_cap() -> float | None:
    """Return the GPU compute capability as a float (e.g. 12.0 for an RTX 50-series
    Blackwell sm_120 card), or None if it can't be read."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run(
            [smi, "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        rows = [row.strip() for row in (out.stdout or "").splitlines() if row.strip()]
        if rows:
            return float(rows[0])
    except Exception:
        pass
    return None


def safe_cuda_compute_type(requested: str) -> tuple[str, str | None]:
    """Pick a compute_type that will not trigger an uncatchable native cuBLAS
    abort on this GPU.

    On RTX 50-series (Blackwell, sm_120 / compute_cap >= 12.0) the int8 GEMM path
    aborts inside cuBLAS with CUBLAS_STATUS_NOT_SUPPORTED. That is a C-level
    abort(), so the GUI's CPU-fallback except-handler never gets a chance to run
    and the whole process dies. The documented working type there is float16, so
    we never let int8* reach a Blackwell GPU. We also honour ctranslate2's own
    supported-types list as a second guard.

    Returns (chosen, note); note is None when nothing changed."""
    chosen = requested
    note = None

    cc = gpu_compute_cap()
    if cc is not None and cc >= 12.0 and requested in ("int8", "int8_float16"):
        chosen = "float16"
        note = (
            f"compute_type {requested!r} aborts on Blackwell (compute_cap {cc}); "
            f"forcing {chosen!r}"
        )

    try:
        import ctranslate2

        supported = set(ctranslate2.get_supported_compute_types("cuda", 0))
        if supported and chosen not in supported:
            for candidate in ("float16", "int8_float16", "int8", "float32"):
                if candidate in supported:
                    note = (
                        f"compute_type {chosen!r} not in CUDA supported types "
                        f"{sorted(supported)}; using {candidate!r}"
                    )
                    chosen = candidate
                    break
    except Exception:
        pass

    return chosen, note


def transcribe_faster(
    audio_path: Path,
    model_name: str,
    task: str,
    language: str,
    device: str,
    compute_type: str,
    beam_size: int,
    log: Callable[[str], None] | None = None,
    progress: ProgressCallback | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    log = log or log_info
    progress = progress or (lambda phase, percent, detail: None)
    stop_requested = stop_requested or (lambda: False)

    try:
        import tqdm._monitor

        class DisabledTqdmMonitor(threading.Thread):
            daemon = True
            _stop_event = threading.Event()
            def run(self):
                self._stop_event.wait(999999)

        tqdm._monitor.TqdmMonitor = DisabledTqdmMonitor
        tqdm.utils.monotonic = lambda: 0
    except Exception:
        pass

    if device in ("cuda", "auto") and sys.platform == "win32":
        if not shutil.which("nvidia-smi"):
            if device == "cuda":
                log("No NVIDIA driver found (nvidia-smi missing). Falling back to CPU (int8).")
                device = "cpu"
                compute_type = "int8"
        elif not ensure_cuda_runtime(log):
            log("Could not set up the CUDA runtime. Falling back to CPU (int8).")
            device = "cpu"
            compute_type = "int8"

    log("Importing faster-whisper")
    progress("model", None, "Importing faster-whisper")

    os.environ.setdefault("CT2_VERBOSE", "1")
    from faster_whisper import WhisperModel

    diag = gpu_diagnostics(device, compute_type, model_name)
    log(diag)
    write_diag("=== GPU DIAGNOSTICS ===\n" + diag)

    if device == "cuda":
        safe_compute, note = safe_cuda_compute_type(compute_type)
        if note:
            log(f"Adjusting compute type: {note}")
            write_diag(f"compute-type guard: {note}")
            compute_type = safe_compute

    log("Checking audio duration")
    duration = audio_duration_seconds(audio_path)
    progress("model", None, f"Loading {model_name}")
    write_diag(f"about to load WhisperModel (device={device}, compute_type={compute_type})")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        write_diag("WhisperModel loaded OK")
    except Exception as exc:
        if device == "cuda":
            raise SystemExit(
                "Could not start faster-whisper on CUDA. Install/update the NVIDIA driver, CUDA 12 runtime, "
                "and cuDNN 9 runtime, then make sure their bin folders are on PATH. "
                "For a slower fallback, rerun with --device cpu --compute-type int8."
                f"\n\nOriginal error: {exc}"
            ) from exc
        raise

    log("Model loaded. Starting Whisper transcription.")
    progress("transcribe", 0.0, "Model loaded")

    write_diag("about to start decode (model.transcribe)")
    segments_iter, _ = model.transcribe(
        str(audio_path),
        language=language,
        task=task,
        beam_size=beam_size,
        vad_filter=True,
    )

    segments: list[dict[str, Any]] = []
    try:
        for index, segment in enumerate(segments_iter):
            if index == 0:
                write_diag("first segment decoded OK (GPU pipeline working)")
            if stop_requested():
                raise SystemExit("Transcription cancelled.")
            segments.append(
                {
                    "id": index,
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                }
            )
            if index == 0 or index % 10 == 0:
                detail = f"Transcribed up to {timestamp(segment.end, vtt=True)}"
                log(detail)
                percent = min((segment.end / duration) * 100, 99.0) if duration else None
                progress("transcribe", percent, detail)
    finally:
        try:
            import tqdm.utils
            tqdm.utils.monotonic = lambda: time.monotonic()
        except Exception:
            pass

    text = " ".join(segment["text"].strip() for segment in segments).strip()
    progress("transcribe", 100.0, "Transcription complete")
    return text, segments


def audio_duration_seconds(audio_path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(completed.stdout.strip())
    except Exception:
        return None


def process_single_url(
    url: str,
    output_dir: Path,
    cookies: str | None,
    cookies_from_browser: str | None,
    model: str,
    task: str,
    language: str,
    backend: str,
    device: str,
    compute_type: str,
    beam_size: int,
    formats: list[str],
) -> list[Path] | None:
    audio_path, info = download_audio(url, output_dir, cookies, cookies_from_browser)

    title = safe_name(info.get("title") or audio_path.stem)
    stem = output_dir / title
    lang = language

    print(f"Loading {backend} model: {model} on {device}", file=sys.stderr)
    print(f"Transcribing in {lang}: {audio_path}", file=sys.stderr)

    try:
        if backend == "faster-whisper":
            text, segments = transcribe_faster(
                audio_path,
                model,
                task,
                lang,
                device,
                compute_type,
                beam_size,
            )
        else:
            dev = None if device == "auto" else device
            text, segments = transcribe_openai(audio_path, model, task, lang, dev)

        output_files: list[Path] = []
        for fmt in formats:
            out_path = stem.with_suffix(f".{lang}.{fmt}")
            try:
                if fmt == "md":
                    write_md(out_path, segments, info)
                elif fmt == "txt":
                    write_txt(out_path, segments)
                elif fmt == "srt":
                    write_srt(out_path, segments)
                elif fmt == "vtt":
                    write_vtt(out_path, segments)
                elif fmt == "json":
                    write_json(out_path, segments, info)
                output_files.append(out_path)
                log_info(f"Wrote {fmt}: {out_path}")
            except Exception as exc:
                log_error(f"Failed to write {fmt}: {exc}")
                raise

        return output_files
    finally:
        try:
            audio_path.unlink(missing_ok=True)
            log_info(f"Deleted audio: {audio_path}")
        except Exception as exc:
            log_error(f"Failed to delete audio {audio_path}: {exc}")

        try:
            info_path = audio_path.with_suffix(".info.json")
            if info_path.exists():
                info_path.unlink(missing_ok=True)
                log_info(f"Deleted info: {info_path}")
        except Exception as exc:
            log_error(f"Failed to delete info {info_path}: {exc}")


def emit_event(payload: dict[str, Any]) -> None:
    """Write a single newline-delimited JSON event to stdout and flush.

    Used by --json-events mode so an Electron (or other) front-end can parse
    structured progress instead of scraping human-readable log lines.
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_json_events_batch(args: argparse.Namespace, urls: list[str], formats: list[str]) -> int:
    """Process URLs sequentially, emitting JSON events on stdout.

    Cancellation: a background thread reads stdin; the line "cancel" sets a
    threading.Event that is polled by the download/transcribe loops. The
    front-end may also simply terminate the process.
    """
    output_dir = Path(args.output_dir).expanduser().resolve()
    purge_download_cache(output_dir, log=log_info)
    cancel = threading.Event()

    def watch_stdin() -> None:
        try:
            for line in sys.stdin:
                if line.strip().lower() == "cancel":
                    cancel.set()
                    break
        except Exception:
            pass

    threading.Thread(target=watch_stdin, daemon=True).start()

    def log_cb(message: str) -> None:
        emit_event({"type": "log", "message": message})

    def progress_cb(phase: str, percent: float | None, detail: str) -> None:
        emit_event({"type": "progress", "phase": phase, "percent": percent, "detail": detail})

    effective_compute = "int8" if args.device == "cpu" else args.compute_type
    total = len(urls)
    completed = 0
    failed = 0
    all_files: list[str] = []

    emit_event({"type": "batch_start", "total": total})

    for i, url in enumerate(urls, 1):
        if cancel.is_set():
            log_cb(f"Cancelled. Processed {i - 1}/{total} URL(s).")
            break

        emit_event({"type": "queue", "current": i, "total": total, "url": url})
        emit_event({"type": "progress", "phase": "download", "percent": None, "detail": f"Downloading [{i}/{total}]"})

        try:
            audio_path, info = download_audio(
                url,
                output_dir,
                args.cookies,
                args.cookies_from_browser,
                log=log_cb,
                progress=progress_cb,
                stop_requested=cancel.is_set,
            )
            log_cb(f"Audio saved: {audio_path}")
            if cancel.is_set():
                log_cb(f"Cancelled during download of URL {i}.")
                break

            emit_event({"type": "progress", "phase": "model", "percent": None, "detail": f"Loading model [{i}/{total}]"})
            text, segments = transcribe_faster(
                audio_path,
                args.model,
                args.task,
                args.language,
                args.device,
                effective_compute,
                args.beam_size,
                log=log_cb,
                progress=progress_cb,
                stop_requested=cancel.is_set,
            )
            if cancel.is_set():
                log_cb(f"Cancelled after transcription of URL {i}.")
                break

            title = safe_name(info.get("title") or audio_path.stem)
            stem = output_dir / title
            emit_event({"type": "progress", "phase": "write", "percent": 0.0, "detail": f"Writing files [{i}/{total}]"})

            video_files: list[str] = []
            for fmt in formats:
                out_path = stem.with_suffix(f".{args.language}.{fmt}")
                if fmt == "md":
                    write_md(out_path, segments, info)
                elif fmt == "txt":
                    write_txt(out_path, segments)
                elif fmt == "srt":
                    write_srt(out_path, segments)
                elif fmt == "vtt":
                    write_vtt(out_path, segments)
                elif fmt == "json":
                    write_json(out_path, segments, info)
                video_files.append(str(out_path))
                all_files.append(str(out_path))
                log_cb(f"Wrote {fmt}: {out_path}")

            try:
                audio_path.unlink(missing_ok=True)
                info_path = audio_path.with_suffix(".info.json")
                if info_path.exists():
                    info_path.unlink(missing_ok=True)
            except Exception:
                pass

            completed += 1
            emit_event({"type": "video_done", "title": title, "files": video_files})

        except BaseException as exc:
            message = str(exc)
            if "cancel" in message.lower():
                log_cb("Cancelled by user.")
                break
            failed += 1
            emit_event({"type": "video_failed", "url": url, "error": message})
            continue

    emit_event({
        "type": "batch_done",
        "completed": completed,
        "failed": failed,
        "total": total,
        "files": all_files,
        "cancelled": cancel.is_set(),
    })
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Download YouTube videos and transcribe them with Whisper.")
    parser.add_argument("url", nargs="?", help="YouTube video URL (or use --urls-file for batch)")
    parser.add_argument("--urls-file", help="Path to a text file with one YouTube URL per line")
    parser.add_argument("-o", "--output-dir", default="transcripts", help="Directory for audio and transcript files")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL, help="Whisper model, for example small, medium, large-v3, large-v3-turbo")
    parser.add_argument("--task", choices=["transcribe", "translate"], default="transcribe", help="Use transcribe for same-language output, translate for English")
    parser.add_argument("-l", "--language", default="pt", help="Language code for transcription (default: pt)")
    parser.add_argument("--backend", choices=["faster-whisper", "openai-whisper"], default="faster-whisper", help="Transcription backend")
    parser.add_argument("--device", default="cuda", help="Whisper device, for example cuda or cpu")
    parser.add_argument("--compute-type", default="float16", help="faster-whisper compute type: float16, int8_float16, int8")
    parser.add_argument("--beam-size", type=int, default=5, help="faster-whisper beam size")
    parser.add_argument("--format", dest="formats", action="append", choices=OUTPUT_FORMATS, help="Output format(s): txt, srt, vtt, json. Can be repeated. Default: txt")
    parser.add_argument("--cookies", help="Path to exported browser cookies.txt for YouTube")
    parser.add_argument("--cookies-from-browser", help="Read cookies from a local browser, for example chrome or firefox")
    parser.add_argument("--json-events", action="store_true", help="Emit newline-delimited JSON events on stdout (for the Electron front-end)")
    args = parser.parse_args()

    formats = args.formats or ["md"]
    setup_logging()

    urls: list[str] = []
    if args.url:
        urls.append(args.url)
    if args.urls_file:
        path = Path(args.urls_file).expanduser().resolve()
        if not path.exists():
            print(f"Error: URLs file not found: {path}", file=sys.stderr)
            return 1
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)

    if not urls:
        print("Error: Provide a URL argument or use --urls-file", file=sys.stderr)
        parser.print_help(sys.stderr)
        return 1

    if args.json_events:
        return run_json_events_batch(args, urls, formats)

    output_dir = Path(args.output_dir).expanduser().resolve()
    purge_download_cache(output_dir, log=log_info)

    succeeded = 0
    failed = 0
    all_outputs: list[Path] = []

    for i, url in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] Processing: {url}", file=sys.stderr)
        try:
            outputs = process_single_url(
                url, output_dir, args.cookies, args.cookies_from_browser,
                args.model, args.task, args.language, args.backend,
                args.device, args.compute_type, args.beam_size, formats,
            )
            if outputs:
                succeeded += 1
                all_outputs.extend(outputs)
                print(str(outputs[0]), file=sys.stderr)
            else:
                failed += 1
                print(f"Error: no output for {url}", file=sys.stderr)
        except SystemExit as exc:
            failed += 1
            print(f"Error processing [{i}/{len(urls)}] {url}: {exc}", file=sys.stderr)
            continue
        except KeyboardInterrupt:
            print("\nInterrupted by user", file=sys.stderr)
            break

    print(f"\nBatch complete: {succeeded} succeeded, {failed} failed out of {len(urls)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log_info("Interrupted by user")
        raise SystemExit(1)
    except SystemExit as exc:
        if exc.code != 0:
            log_error(f"SystemExit: {exc.code}")
        raise
    except Exception as exc:
        log_error(f"Unexpected error: {exc}")
        traceback.print_exc(file=sys.stderr)
        log_error(traceback.format_exc())
        raise SystemExit(1)