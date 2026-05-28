# Builds the Tkinter GUI + faster-whisper engine into one size-optimized
# PyInstaller bundle and packages it as yt-transcript-windows.zip (the asset
# the in-app self-updater downloads). Mirrors .github/workflows/windows-release.yml.
$ErrorActionPreference = "Stop"

python -m pip install -U pip
python -m pip install -r requirements-windows.txt

pyinstaller `
  --onedir --windowed `
  --name yt-transcript `
  --icon electron/build/icon.ico `
  --collect-all faster_whisper `
  --collect-all ctranslate2 `
  --hidden-import yt_dlp `
  --hidden-import pystray `
  --hidden-import pystray._win32 `
  --hidden-import PIL `
  --hidden-import PIL.Image `
  --hidden-import PIL.ImageDraw `
  --exclude-module hf_xet `
  --exclude-module torch `
  --exclude-module whisper `
  --exclude-module matplotlib `
  --exclude-module scipy `
  --exclude-module pandas `
  yt_transcript_gui.py

# Strip unused ffmpeg video/image encoders (we only decode audio).
$avlibs = "dist/yt-transcript/_internal/av.libs"
if (Test-Path $avlibs) {
  foreach ($p in @("libx265*","libSvtAv1Enc*","libx264*","libvpx*","libdav1d*","libopenh264*","libwebp*","libvorbisenc*","libaom*")) {
    Get-ChildItem $avlibs -Filter $p -ErrorAction SilentlyContinue | Remove-Item -Force
  }
}

# yt-dlp.exe must sit next to the app exe (find_ytdlp looks beside sys.executable).
Invoke-WebRequest `
  -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" `
  -OutFile "dist/yt-transcript/yt-dlp.exe"
Copy-Item README.md "dist/yt-transcript/README.md"
Copy-Item run-windows-gpu.bat "dist/yt-transcript/run-windows-gpu.bat"

Compress-Archive -Path "dist/yt-transcript/*" -DestinationPath "yt-transcript-windows.zip" -CompressionLevel Optimal -Force
"{0:N1} MB  yt-transcript-windows.zip" -f ((Get-Item yt-transcript-windows.zip).Length/1MB)
