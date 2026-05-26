# YouTube Transcripts

CLI and GUI that use `yt-dlp` to download YouTube audio and Whisper to produce transcripts in any language.

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

To specify a language (default is Portuguese):

```bash
python transcribe_youtube.py "URL" --language en --device cuda
```

To choose output formats:

```bash
python transcribe_youtube.py "URL" --format txt --format srt --format vtt --format json
```

### Batch processing

Process multiple URLs from a file:

```bash
python transcribe_youtube.py --urls-file urls.txt --device cuda
```

The file should have one URL per line. Lines starting with `#` are ignored. Each URL is processed sequentially; failures don't stop the batch.

```text
# My batch list
https://www.youtube.com/watch?v=VIDEO1
https://www.youtube.com/watch?v=VIDEO2
https://youtu.be/VIDEO3
```

If YouTube blocks the server with a bot/sign-in check, export YouTube cookies from your browser and pass them in:

```bash
python transcribe_youtube.py "URL" --cookies /path/to/cookies.txt
```

### Output formats

All outputs use the pattern `.{lang}.{fmt}`:

- `.pt.txt`: plain transcript with timestamps (default language: Portuguese)
- `.pt.srt`: SRT subtitles
- `.pt.vtt`: WebVTT subtitles
- `.pt.json`: transcript segments plus source metadata
- `.en.srt`: SRT subtitles in English (with `--language en`)

Audio files (`.mp3`) are deleted after transcription by default.

## Windows App (recommended)

Download the latest **`YT Transcript-Setup-x.y.z.exe`** from the
[Releases page](https://github.com/jokaperes/yt-transcript/releases) and run it.
This is a desktop app (Electron front-end + bundled `faster-whisper` engine) — no
Python install required. `ffmpeg` is still needed on `PATH` (see Setup above).

Once installed, the app **updates itself**: it checks GitHub Releases on launch and
offers to download/install new versions, so you only install manually once.

> Building from source? See [Building the Windows installer](#building-the-windows-installer).

The app supports:

- **Batch URLs** — paste multiple URLs in the text box, one per line; they're processed sequentially with per-video error recovery
- **Language selection** (Portuguese, English, Spanish, French, etc.)
- **Output format checkboxes** (TXT, SRT, VTT, JSON)
- **Paste button** — one-click clipboard paste into the URL box
- **Estimated time remaining** (ETA) shown in the status bar during transcription
- **Right-click log menu** — Copy selected, Select all, Clear log
- **System tray icon** — minimize to tray while processing; tooltip shows progress
- **In-app auto-update** — checks GitHub for the latest release and downloads/installs automatically
- **Settings persistence** between sessions
- **Live progress** — yt-dlp download logs, Whisper transcription progress, queue counter (3/5)

If YouTube asks for bot/sign-in confirmation, export cookies from your browser and
point the **Cookies file** field at the exported `cookies.txt`. Some browser cookie
exporters write YouTube host-only cookies in a format Python rejects; the engine
automatically writes a normalized copy inside the output folder when needed.

For scripted/CLI use, run the Python engine directly from source (see Usage above).

## Building the Windows installer

The installer is built by GitHub Actions on every `v*` tag (see
`.github/workflows/windows-release.yml`):

1. `build-engine.ps1` builds `transcribe_youtube.py` into a standalone
   `yt-transcript.exe` with PyInstaller and downloads `yt-dlp.exe`, staging both into
   `electron/engine/`.
2. `npm install` then `npm run publish` (electron-builder) bundles that engine inside
   the Electron app, produces `YT Transcript-Setup-x.y.z.exe` plus the `latest.yml`
   auto-update feed, and publishes them to GitHub Releases.

To cut a release: bump `version` in `electron/package.json`, then push a matching tag,
e.g. `git tag v2.0.0 && git push origin v2.0.0`.

## Model Choice

For an NVIDIA RTX 5070 with 12 GB VRAM, use:

```bash
python transcribe_youtube.py "URL" --backend faster-whisper --device cuda --compute-type float16 --model large-v3-turbo
```

Why:

- `large-v3-turbo` is the best default for transcription on 12 GB VRAM: strong quality and much faster than full `large-v3`.
- Use `large-v3` when you want maximum accuracy and can wait longer.
- Use `medium` if CUDA memory setup fails or you want a smaller download.
- Use `small` or `base` only for quick drafts.

For lower VRAM pressure, use:

```bash
python transcribe_youtube.py "URL" --device cuda --compute-type int8_float16 --model large-v3-turbo
```

## Notes

- Use `--language` to set the transcription language (default: `pt`). Supported: `pt`, `en`, `es`, `fr`, `de`, `it`, `ja`, `ko`, `zh`, `ru`, or any Whisper-supported code.
- `--task transcribe` keeps same-language output. `--task translate` translates to English.
- Use `--format` to select output formats. Can be specified multiple times: `--format txt --format srt`. Default is `txt` only