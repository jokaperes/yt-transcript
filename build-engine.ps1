# Builds the Python transcription engine as a standalone exe and stages it
# (plus yt-dlp.exe) into electron/engine/ so electron-builder can bundle it.
$ErrorActionPreference = "Stop"

python -m pip install -U pip
python -m pip install -r requirements-windows.txt

pyinstaller `
  --onefile `
  --name yt-transcript `
  --collect-all faster_whisper `
  --collect-all ctranslate2 `
  --hidden-import yt_dlp `
  transcribe_youtube.py

$engineDir = "electron/engine"
New-Item -ItemType Directory -Force $engineDir | Out-Null
Copy-Item dist\yt-transcript.exe (Join-Path $engineDir "yt-transcript.exe") -Force

# yt-dlp.exe must sit next to the engine exe (find_ytdlp looks beside sys.executable).
Invoke-WebRequest `
  -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" `
  -OutFile (Join-Path $engineDir "yt-dlp.exe")

Get-ChildItem $engineDir
