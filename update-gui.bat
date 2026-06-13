@echo off
setlocal

set "REPO=jokaperes/yt-transcript"
set "TAG=v1.3.3"

echo Checking for updates...
echo.

for /f "tokens=*" %%i in ('curl -sL "https://api.github.com/repos/%REPO%/releases/tags/%TAG%" ^| findstr /C:"browser_download_url" ^| findstr /C:"yt-transcript-gui.exe"') do set "URL=%%i"
set "URL=%URL:~1,-1%"

echo Downloading yt-transcript-gui.exe...
powershell -Command "Invoke-WebRequest -Uri '%URL%' -OutFile 'yt-transcript-gui-new.exe'"

echo.
echo Replacing old executable...
move /y "yt-transcript-gui.exe" "yt-transcript-gui-old.exe"
move /y "yt-transcript-gui-new.exe" "yt-transcript-gui.exe"

echo.
echo Done! Old exe saved as yt-transcript-gui-old.exe
pause
