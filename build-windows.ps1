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
  --collect-all yt_dlp `
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

# Strip unused ffmpeg video/image encoders + video-device libs (audio decode only).
$avlibs = "dist/yt-transcript/_internal/av.libs"
if (Test-Path $avlibs) {
  foreach ($p in @("libx265*","libSvtAv1Enc*","libx264*","libvpx*","libdav1d*","libopenh264*","libwebp*","libvorbisenc*","libaom*","libvpl*","avdevice*")) {
    Get-ChildItem $avlibs -Filter $p -ErrorAction SilentlyContinue | Remove-Item -Force
  }
}

# Pillow is only used to draw the tray icon — drop unused image codecs.
$pil = "dist/yt-transcript/_internal/PIL"
if (Test-Path $pil) {
  foreach ($p in @("_avif*","_webp*","_imagingcms*","_imagingft*")) {
    Get-ChildItem $pil -Filter $p -ErrorAction SilentlyContinue | Remove-Item -Force
  }
}

# yt-dlp now runs from the bundled yt_dlp module via the app's --run-ytdlp
# dispatch, so we no longer ship a separate yt-dlp.exe.
Copy-Item README.md "dist/yt-transcript/README.md"
Copy-Item run-windows-gpu.bat "dist/yt-transcript/run-windows-gpu.bat"

Compress-Archive -Path "dist/yt-transcript/*" -DestinationPath "yt-transcript-windows.zip" -CompressionLevel Optimal -Force
"{0:N1} MB  yt-transcript-windows.zip" -f ((Get-Item yt-transcript-windows.zip).Length/1MB)
