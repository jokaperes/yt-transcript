#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import whisper


DEFAULT_MODEL = "small"


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

    command = [
        sys.executable,
        "-m",
        "yt_dlp",
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Download a YouTube video and transcribe it in Portuguese with Whisper.")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("-o", "--output-dir", default="transcripts", help="Directory for audio and transcript files")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL, help="Whisper model: tiny, base, small, medium, large")
    parser.add_argument("--task", choices=["transcribe", "translate"], default="transcribe", help="Use transcribe for Portuguese text")
    parser.add_argument("--device", default=None, help="Whisper device, for example cpu or cuda")
    parser.add_argument("--cookies", help="Path to exported browser cookies.txt for YouTube")
    parser.add_argument("--cookies-from-browser", help="Read cookies from a local browser, for example chrome or firefox")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    audio_path, info = download_audio(args.url, output_dir, args.cookies, args.cookies_from_browser)

    title = safe_name(info.get("title") or audio_path.stem)
    stem = output_dir / title

    print(f"Loading Whisper model: {args.model}", file=sys.stderr)
    model = whisper.load_model(args.model, device=args.device)

    print(f"Transcribing in Portuguese: {audio_path}", file=sys.stderr)
    result = model.transcribe(str(audio_path), language="pt", task=args.task, fp16=False)
    segments = result.get("segments", [])

    write_txt(stem.with_suffix(".pt.txt"), result["text"])
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
                "text": result["text"].strip(),
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
