#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "large-v3-turbo"


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
    value = re.sub(r"[^\w\s.-]", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120] or "youtube-video"


def download_audio(url: str, output_dir: Path, cookies: str | None, cookies_from_browser: str | None) -> tuple[Path, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    template = str(output_dir / "%(title).120s [%(id)s].%(ext)s")

    bundled_ytdlp = Path(sys.executable).with_name("yt-dlp.exe")
    ytdlp_binary = str(bundled_ytdlp) if bundled_ytdlp.exists() else shutil.which("yt-dlp")
    if ytdlp_binary:
        command = [ytdlp_binary]
    else:
        command = [sys.executable, "-m", "yt_dlp"]

    command += [
        "--js-runtimes",
        "node",
        "--remote-components",
        "ejs:github",
        "--no-playlist",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "--write-info-json",
        "--print",
        "after_move:filepath",
        "-o",
        template,
        url,
    ]
    if cookies:
        command[3:3] = ["--cookies", cookies]
    if cookies_from_browser:
        command[3:3] = ["--cookies-from-browser", cookies_from_browser]
    printed_paths = [line for line in run(command, capture_output=True).splitlines() if line.strip()]
    if not printed_paths:
        raise SystemExit("yt-dlp finished, but did not report an audio file path.")

    audio_path = Path(printed_paths[-1]).resolve()
    if not audio_path.exists():
        raise SystemExit(f"yt-dlp reported an audio path, but it does not exist: {audio_path}")

    info_path = audio_path.with_suffix(".info.json")
    info: dict[str, Any] = {}
    if info_path.exists():
        info = json.loads(info_path.read_text(encoding="utf-8"))

    return audio_path, info


def timestamp(seconds: float, vtt: bool = False) -> str:
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    separator = "." if vtt else ","
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"


def write_txt(path: Path, text: str) -> None:
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


def transcribe_openai(audio_path: Path, model_name: str, task: str, device: str | None) -> tuple[str, list[dict[str, Any]]]:
    import whisper

    model = whisper.load_model(model_name, device=device)
    result = model.transcribe(str(audio_path), language="pt", task=task, fp16=device == "cuda")
    return result["text"].strip(), result.get("segments", [])


def transcribe_faster(
    audio_path: Path,
    model_name: str,
    task: str,
    device: str,
    compute_type: str,
    beam_size: int,
) -> tuple[str, list[dict[str, Any]]]:
    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
    except Exception as exc:
        if device == "cuda":
            raise SystemExit(
                "Could not start faster-whisper on CUDA. Install/update the NVIDIA driver, CUDA 12 runtime, "
                "and cuDNN 9 runtime, then make sure their bin folders are on PATH. "
                "For a slower fallback, rerun with --device cpu --compute-type int8."
            ) from exc
        raise
    segments_iter, _ = model.transcribe(
        str(audio_path),
        language="pt",
        task=task,
        beam_size=beam_size,
        vad_filter=True,
    )
    segments = [
        {
            "id": index,
            "start": segment.start,
            "end": segment.end,
            "text": segment.text,
        }
        for index, segment in enumerate(segments_iter)
    ]
    text = " ".join(segment["text"].strip() for segment in segments).strip()
    return text, segments


def main() -> int:
    parser = argparse.ArgumentParser(description="Download a YouTube video and transcribe it in Portuguese with Whisper.")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("-o", "--output-dir", default="transcripts", help="Directory for audio and transcript files")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL, help="Whisper model, for example small, medium, large-v3, large-v3-turbo")
    parser.add_argument("--task", choices=["transcribe", "translate"], default="transcribe", help="Use transcribe for Portuguese text")
    parser.add_argument("--backend", choices=["faster-whisper", "openai-whisper"], default="faster-whisper", help="Transcription backend")
    parser.add_argument("--device", default="cuda", help="Whisper device, for example cuda or cpu")
    parser.add_argument("--compute-type", default="float16", help="faster-whisper compute type: float16, int8_float16, int8")
    parser.add_argument("--beam-size", type=int, default=5, help="faster-whisper beam size")
    parser.add_argument("--cookies", help="Path to exported browser cookies.txt for YouTube")
    parser.add_argument("--cookies-from-browser", help="Read cookies from a local browser, for example chrome or firefox")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    audio_path, info = download_audio(args.url, output_dir, args.cookies, args.cookies_from_browser)

    title = safe_name(info.get("title") or audio_path.stem)
    stem = output_dir / title

    print(f"Loading {args.backend} model: {args.model} on {args.device}", file=sys.stderr)
    print(f"Transcribing in Portuguese: {audio_path}", file=sys.stderr)
    if args.backend == "faster-whisper":
        text, segments = transcribe_faster(
            audio_path,
            args.model,
            args.task,
            args.device,
            args.compute_type,
            args.beam_size,
        )
    else:
        device = None if args.device == "auto" else args.device
        text, segments = transcribe_openai(audio_path, args.model, args.task, device)

    write_txt(stem.with_suffix(".pt.txt"), text)
    write_srt(stem.with_suffix(".pt.srt"), segments)
    write_vtt(stem.with_suffix(".pt.vtt"), segments)
    stem.with_suffix(".pt.json").write_text(
        json.dumps(
            {
                "source_url": args.url,
                "title": info.get("title"),
                "audio": str(audio_path),
                "language": "pt",
                "model": args.model,
                "backend": args.backend,
                "device": args.device,
                "compute_type": args.compute_type if args.backend == "faster-whisper" else None,
                "text": text,
                "segments": segments,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(stem.with_suffix(".pt.txt"))
    print(stem.with_suffix(".pt.srt"))
    print(stem.with_suffix(".pt.vtt"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
