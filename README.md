# YouTube Portuguese Transcripts

Small CLI that uses `yt-dlp` to download YouTube audio and Whisper to produce Portuguese transcripts.

The default backend is `faster-whisper`, which is the recommended path for NVIDIA GPUs.

## Setup

```bash
cd /root/code/yt-transcript
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements-windows.txt
```

`ffmpeg` is required. On Windows, install it with one of:

```powershell
winget install Gyan.FFmpeg
choco install ffmpeg
```

For NVIDIA GPU acceleration on Windows, install a current NVIDIA driver. `faster-whisper` also needs CUDA/cuDNN runtime DLLs available on `PATH`; if GPU startup fails, install the current CUDA 12 runtime and cuDNN 9 runtime from NVIDIA.

## Usage

```bash
cd /root/code/yt-transcript
source .venv/bin/activate
python transcribe_youtube.py "https://www.youtube.com/watch?v=VIDEO_ID" --device cuda
```

If YouTube blocks the server with a bot/sign-in check, export YouTube cookies from your browser and pass them in:

```bash
python transcribe_youtube.py "URL" --cookies /path/to/cookies.txt
```

Outputs are written to `transcripts/`:

- `.pt.txt`: plain Portuguese transcript
- `.pt.srt`: subtitles
- `.pt.vtt`: web subtitles
- `.pt.json`: transcript plus segments and source metadata
- `.mp3`: downloaded audio

## Windows Quick Start

Download the Windows release zip, extract it, and run:

```cmd
yt-transcript-gui.exe
```

The GUI is the easiest option: paste the YouTube URL, optionally select a `cookies.txt` file, keep `large-v3-turbo`, `cuda`, and `float16`, then click **Transcribe**.

The GUI shows live `yt-dlp` download logs, Whisper progress timestamps, clear dependency errors, and has a cancel button.

CLI usage is also available:

```cmd
yt-transcript.exe "https://www.youtube.com/watch?v=VIDEO_ID" --device cuda --model large-v3-turbo
```

Or use the helper:

```cmd
run-windows-gpu.bat
```

If YouTube asks for bot/sign-in confirmation, export cookies from your browser and run:

```cmd
yt-transcript.exe "URL" --cookies C:\path\to\cookies.txt --device cuda --model large-v3-turbo
```

Some browser cookie exporters write YouTube host-only cookies in a format Python rejects. The app automatically writes a normalized copy inside the output folder when needed.

## Model Choice

For an NVIDIA RTX 5070 with 12 GB VRAM, use:

```bash
python transcribe_youtube.py "URL" --backend faster-whisper --device cuda --compute-type float16 --model large-v3-turbo
```

Why:

- `large-v3-turbo` is the best default for Portuguese transcription on 12 GB VRAM: strong quality and much faster than full `large-v3`.
- Use `large-v3` when you want maximum accuracy and can wait longer.
- Use `medium` if CUDA memory setup fails or you want a smaller download.
- Use `small` or `base` only for quick drafts.

For lower VRAM pressure, use:

```bash
python transcribe_youtube.py "URL" --device cuda --compute-type int8_float16 --model large-v3-turbo
```

## Notes

- The CLI forces Whisper language detection to Portuguese with `language="pt"`.
- `--task transcribe` keeps Portuguese output. `--task translate` translates to English, so avoid it if you want Portuguese text.
