@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-windows.txt

set /p VIDEO_URL=YouTube URL: 
python transcribe_youtube.py "%VIDEO_URL%" --backend faster-whisper --device cuda --compute-type float16 --model large-v3-turbo

pause
