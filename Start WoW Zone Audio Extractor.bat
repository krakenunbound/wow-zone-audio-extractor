@echo off
setlocal
cd /d "%~dp0"

set "PYW=%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"
set "APP=%~dp0wow_zone_audio_extractor_gui.py"

if not exist "%PYW%" (
    echo Could not find Python 3.14 at
    echo %PYW%
    pause
    exit /b 1
)
if not exist "%APP%" (
    echo Could not find
    echo %APP%
    pause
    exit /b 1
)

REM A second double-click focuses the window that is already open.
powershell -NoProfile -Command "exit ([int](-not (New-Object -ComObject WScript.Shell).AppActivate('WoW Zone Audio Extractor')))"
if %ERRORLEVEL%==0 exit /b 0

start "" "%PYW%" "%APP%"
exit /b 0
