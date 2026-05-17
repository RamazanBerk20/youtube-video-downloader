@echo off
REM Launches the YouTube Downloader GUI on Windows.
REM On first run, creates a .venv and installs dependencies.
setlocal

cd /d "%~dp0"

set "VENV_DIR=.venv"

REM Prefer the Windows 'py' launcher; fall back to 'python' on PATH.
set "PYTHON="
where py >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo Error: Python is not installed or not on PATH.
    echo        Install Python 3.10+ from https://www.python.org/downloads/ and try again.
    pause
    exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo Warning: ffmpeg is not in PATH. Audio extraction and video+audio merging will fail.
    echo          Install via: winget install Gyan.FFmpeg
    echo          Or download from https://www.gyan.dev/ffmpeg/builds/
    echo.
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo First-time setup: creating virtual environment in %VENV_DIR%...
    %PYTHON% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

"%VENV_PY%" -c "import yt_dlp, customtkinter" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    "%VENV_PY%" -m pip install --upgrade pip
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b 1
    )
)

"%VENV_PY%" app.py %*
endlocal
