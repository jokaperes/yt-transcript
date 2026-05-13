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

pyinstaller `
  --onefile `
  --windowed `
  --name yt-transcript-gui `
  --collect-all faster_whisper `
  --collect-all ctranslate2 `
  --hidden-import yt_dlp `
  --hidden-import pystray `
  --hidden-import pystray._win32 `
  --hidden-import PIL `
  --hidden-import PIL.Image `
  --hidden-import PIL.ImageDraw `
  yt_transcript_gui.py

New-Item -ItemType Directory -Force release | Out-Null
Copy-Item dist\yt-transcript.exe release\yt-transcript.exe
Copy-Item dist\yt-transcript-gui.exe release\yt-transcript-gui.exe
Invoke-WebRequest `
  -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" `
  -OutFile "release\yt-dlp.exe"
Copy-Item README.md release\README.md
Copy-Item run-windows-gpu.bat release\run-windows-gpu.bat
Compress-Archive -Path release\* -DestinationPath yt-transcript-windows.zip -Force
