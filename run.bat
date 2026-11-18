@echo off
setlocal
cd /d "%~dp0"
title Auto Editor

:: Set up common Windows fallback paths for Python, Scripts, and FFmpeg
where python >nul 2>nul
if errorlevel 1 (
    set "PATH=%LOCALAPPDATA%\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0;%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\LocalCache\local-packages\Python313\Scripts;%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin;%PATH%"
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin;C:\ffmpeg\bin;C:\Program Files\ffmpeg\bin;%PATH%"
)

:: Verify Python is available
where python >nul 2>nul
if errorlevel 1 (
    echo ========================================================
    echo  [ERROR] Python was not found on your system!
    echo ========================================================
    echo  Please install Python 3.9+ from:
    echo  https://www.python.org/downloads/
    echo.
    echo  Important: During installation, make sure to check:
    echo  [x] "Add Python to PATH"
    echo ========================================================
    echo.
    pause
    exit /b 1
)

:: Install dependencies
echo Checking dependencies...
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [INFO] Running fallback dependency install...
    python -m pip install --quiet customtkinter numpy opencv-python pynput
)

:: Launch the application
echo Starting Auto Editor...
python app.py
if errorlevel 1 (
    echo.
    echo [ERROR] Auto Editor stopped unexpectedly.
    pause
)
