@echo off
REM Launches the YouTube Downloader GUI on Windows.
REM On first run, creates a .venv and installs dependencies.
setlocal

cd /d "%~dp0"

set "VENV_DIR=.venv"

REM ---- Locate a working Python interpreter ------------------------------
REM Prefer the 'py' launcher, fall back to 'python'. Verify with --version
REM so we reject the Microsoft Store stub that 'where' otherwise finds.
set "PYTHON="
where py >nul 2>nul && py -3 --version >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (where python >nul 2>nul && python --version >nul 2>nul && set "PYTHON=python")
if defined PYTHON goto :python_ok

REM ---- No Python: offer to install via winget ---------------------------
REM Labels and set/p MUST live at the top level, NOT inside an `if (...)`
REM block — cmd.exe parses the whole block as one compound command and
REM falls over on labels or unexpected `)` characters inside it.
echo No working Python interpreter was found.
echo.
where winget >nul 2>nul
if errorlevel 1 goto :no_winget

echo Would install via:  winget install Python.Python.3.13
set "REPLY="
set /p REPLY="Install now? [Y/n] "
if /i "%REPLY%"=="n"  goto :py_abort
if /i "%REPLY%"=="no" goto :py_abort

winget install --silent --accept-source-agreements --accept-package-agreements Python.Python.3.13
if errorlevel 1 goto :py_install_failed

REM Re-resolve after install (winget updates the user PATH).
set "PYTHON="
where py >nul 2>nul && py -3 --version >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (where python >nul 2>nul && python --version >nul 2>nul && set "PYTHON=python")
if not defined PYTHON goto :py_path_stale
goto :python_ok

:no_winget
echo winget is not available on this system.
echo Install Python 3.10 or newer manually from:
echo   https://www.python.org/downloads/
echo.
echo IMPORTANT: tick "Add python.exe to PATH" in the installer.
pause
exit /b 1

:py_abort
echo Aborted. Install Python 3.10+ and re-run.
pause
exit /b 1

:py_install_failed
echo Python install failed.
pause
exit /b 1

:py_path_stale
echo Python was installed but is not yet on PATH for this session.
echo Close this window and double-click start.bat again.
pause
exit /b 1

:python_ok

REM ---- ffmpeg warning (the app also surfaces this with an Install button)
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo Warning: ffmpeg is not in PATH. Audio extraction and video+audio merging will fail.
    echo          Install via: winget install Gyan.FFmpeg
    echo          Or use the in-app Install button after the GUI opens.
    echo.
)

REM ---- Virtual environment ---------------------------------------------
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
