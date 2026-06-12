#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
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
OUTPUT_FORMATS = ("txt", "srt", "vtt", "json")
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
    normalized.write_text(fixed, encoding="utf-8")
    if log:
        log(f"Normalized YouTube cookies format: {normalized}")
    return str(normalized)


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
    template = str(output_dir / "%(title).120s [%(id)s].%(ext)s")
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
        candidates = sorted(output_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            files = "\n".join(p.name for p in output_dir.glob("*"))
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

    log("Importing faster-whisper")
    progress("model", None, "Importing faster-whisper")

    from faster_whisper import WhisperModel

    log("Checking audio duration")
    duration = audio_duration_seconds(audio_path)
    progress("model", None, f"Loading {model_name}")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
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
                if fmt == "txt":
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
                if fmt == "txt":
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

    formats = args.formats or ["txt"]
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