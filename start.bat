@echo off
REM Launches the YouTube Downloader GUI on Windows.
REM On first run, creates a .venv and installs dependencies.
setlocal

cd /d "%~dp0"

set "VENV_DIR=.venv"

REM Prefer the Windows 'py' launcher; fall back to 'python' on PATH.
REM Verify with --version so we reject the Microsoft Store stub
REM (which 'where' finds but which doesn't actually run Python).
set "PYTHON="
where py >nul 2>nul && py -3 --version >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>nul && python --version >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo No working Python interpreter was found.
    echo.
    where winget >nul 2>nul
    if errorlevel 1 (
        echo winget is not available on this system.
        echo Install Python 3.10 or newer manually from:
        echo   https://www.python.org/downloads/
        echo.
        echo IMPORTANT: tick "Add python.exe to PATH" in the installer.
        pause
        exit /b 1
    )
    echo Would install via:  winget install Python.Python.3.13
    set /p REPLY="Install now? [Y/n] "
    if /i "%REPLY%"=="n" goto :py_abort
    if /i "%REPLY%"=="no" goto :py_abort
    winget install --silent --accept-source-agreements --accept-package-agreements Python.Python.3.13
    if errorlevel 1 (
        echo Python install failed.
        pause
        exit /b 1
    )
    REM Re-resolve PATH after the install (winget updates the user PATH)
    where py >nul 2>nul && py -3 --version >nul 2>nul && set "PYTHON=py -3"
    if not defined PYTHON (
        where python >nul 2>nul && python --version >nul 2>nul && set "PYTHON=python"
    )
    if not defined PYTHON (
        echo Python was installed but is not yet on PATH for this session.
        echo Close this window and double-click start.bat again.
        pause
        exit /b 1
    )
    goto :py_ok
    :py_abort
    echo Aborted. Install Python 3.10+ and re-run.
    pause
    exit /b 1
    :py_ok
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
