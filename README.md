# YouTube Portuguese Transcripts

Small CLI that uses `yt-dlp` to download YouTube audio and OpenAI Whisper to produce Portuguese transcripts.

## Setup

```bash
cd /root/code/yt-transcript
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

`ffmpeg` is required and is already installed on this machine at `/usr/bin/ffmpeg`.

## Usage

```bash
cd /root/code/yt-transcript
source .venv/bin/activate
python transcribe_youtube.py "https://www.youtube.com/watch?v=VIDEO_ID"
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

## Model Choice

The default model is `small`, which is a reasonable speed/quality balance on CPU.

Use a larger model for better accuracy:

```bash
python transcribe_youtube.py "URL" --model medium
```

Use a faster model for quick drafts:

```bash
python transcribe_youtube.py "URL" --model base
```

## Notes

- The CLI forces Whisper language detection to Portuguese with `language="pt"`.
- `--task transcribe` keeps Portuguese output. `--task translate` translates to English, so avoid it if you want Portuguese text.
