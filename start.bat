@echo off
REM Launches the YouTube Downloader GUI on Windows.
REM
REM Verifies every dependency the app needs before handing off to the
REM Python GUI. Offers to install missing pieces via winget:
REM   - Python 3.13            (Python.Python.3.13)
REM   - git (optional)         (Git.Git)
REM   - ffmpeg                 (Gyan.FFmpeg)
REM   - Deno (JS runtime)      (DenoLand.Deno)
REM
REM After each winget install, PATH is re-read from the registry so the
REM current shell can find the newly installed binary without forcing
REM the user to close + reopen the terminal.
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set "VENV_DIR=.venv"

REM =========================================================================
REM PATH refresh helper. winget writes installed binaries to user/system
REM PATH at the registry level, but the current cmd.exe session inherited
REM its PATH at startup and won't see the new entries. Re-read from the
REM registry and rebuild %PATH%.
REM =========================================================================
goto :after_helpers

:refresh_path
    set "SYS_PATH="
    set "USER_PATH="
    for /f "tokens=2,*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul ^| findstr /i Path') do set "SYS_PATH=%%b"
    for /f "tokens=2,*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul ^| findstr /i Path') do set "USER_PATH=%%b"
    if defined SYS_PATH if defined USER_PATH (
        set "PATH=!SYS_PATH!;!USER_PATH!"
    ) else if defined SYS_PATH (
        set "PATH=!SYS_PATH!"
    )
    goto :eof

:after_helpers

REM =========================================================================
REM Python
REM =========================================================================
set "PYTHON="
where py >nul 2>nul && py -3 --version >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (where python >nul 2>nul && python --version >nul 2>nul && set "PYTHON=python")
if defined PYTHON goto :python_ok

echo No working Python interpreter was found.
echo.
where winget >nul 2>nul
if errorlevel 1 goto :no_winget_py

echo Would install via:  winget install Python.Python.3.13
set "REPLY="
set /p REPLY="Install now? [Y/n] "
if /i "%REPLY%"=="n"  goto :py_abort
if /i "%REPLY%"=="no" goto :py_abort

winget install --silent --accept-source-agreements --accept-package-agreements Python.Python.3.13
if errorlevel 1 goto :py_install_failed

call :refresh_path

set "PYTHON="
where py >nul 2>nul && py -3 --version >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (where python >nul 2>nul && python --version >nul 2>nul && set "PYTHON=python")
if not defined PYTHON goto :py_path_stale
goto :python_ok

:no_winget_py
echo winget is not available. Install Python 3.10 or newer manually:
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

REM =========================================================================
REM git (optional but recommended — needed by the in-app auto-updater)
REM =========================================================================
where git >nul 2>nul
if not errorlevel 1 goto :git_ok

echo Note: 'git' is not installed. Auto-update checks will be disabled.
where winget >nul 2>nul
if errorlevel 1 goto :git_no_winget
echo Would install via:  winget install Git.Git
set "GIT_REPLY="
set /p GIT_REPLY="Install now? [Y/n] "
if /i "%GIT_REPLY%"=="n"  goto :git_skip
if /i "%GIT_REPLY%"=="no" goto :git_skip
winget install --silent --accept-source-agreements --accept-package-agreements Git.Git
if errorlevel 1 (
    echo git install failed - continuing without auto-update.
    goto :git_ok
)
call :refresh_path
where git >nul 2>nul
if errorlevel 1 (
    echo git installed but not on PATH for this session.
    echo Close and re-run start.bat to pick it up.
)
goto :git_ok

:git_no_winget
echo   No winget available. Install Git for Windows manually if you want auto-updates:
echo     https://git-scm.com/download/win
goto :git_ok

:git_skip
echo Skipping. Auto-update remains disabled.

:git_ok

REM =========================================================================
REM ffmpeg (required for audio extraction and video+audio merging)
REM =========================================================================
where ffmpeg >nul 2>nul
if not errorlevel 1 goto :ffmpeg_ok

echo ffmpeg is not installed.
where winget >nul 2>nul
if errorlevel 1 goto :ffmpeg_no_winget
echo Would install via:  winget install Gyan.FFmpeg
set "FF_REPLY="
set /p FF_REPLY="Install now? [Y/n] "
if /i "%FF_REPLY%"=="n"  goto :ffmpeg_skip
if /i "%FF_REPLY%"=="no" goto :ffmpeg_skip
winget install --silent --accept-source-agreements --accept-package-agreements Gyan.FFmpeg
if errorlevel 1 (
    echo ffmpeg install failed - audio extraction and merging will fail.
    goto :ffmpeg_ok
)
call :refresh_path
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo ffmpeg installed but not on PATH for this session.
    echo Close and re-run start.bat to pick it up.
)
goto :ffmpeg_ok

:ffmpeg_no_winget
echo   No winget available. Install ffmpeg manually: https://ffmpeg.org/download.html
goto :ffmpeg_ok

:ffmpeg_skip
echo Skipping ffmpeg install. Audio extraction and high-quality merging will fail.

:ffmpeg_ok

REM =========================================================================
REM Deno (JS runtime — REQUIRED for YouTube downloads as of 2025+)
REM yt-dlp can't solve YouTube's anti-bot JS challenge without a runtime
REM and falls back to "Requested format is not available" on every video.
REM =========================================================================
where deno >nul 2>nul
if not errorlevel 1 goto :deno_ok
where node >nul 2>nul
if not errorlevel 1 goto :deno_ok
where bun >nul 2>nul
if not errorlevel 1 goto :deno_ok

echo Deno (a JavaScript runtime) is not installed.
echo This is REQUIRED — without it, YouTube downloads fail with
echo "Requested format is not available" because yt-dlp can't solve
echo YouTube's anti-bot challenge.
where winget >nul 2>nul
if errorlevel 1 goto :deno_no_winget
echo Would install via:  winget install DenoLand.Deno
set "DENO_REPLY="
set /p DENO_REPLY="Install now? [Y/n] "
if /i "%DENO_REPLY%"=="n"  goto :deno_skip
if /i "%DENO_REPLY%"=="no" goto :deno_skip
winget install --silent --accept-source-agreements --accept-package-agreements DenoLand.Deno
if errorlevel 1 (
    echo Deno install failed - downloads will likely fail.
    goto :deno_ok
)
call :refresh_path
where deno >nul 2>nul
if errorlevel 1 (
    echo Deno installed but not on PATH for this session.
    echo Close and re-run start.bat to pick it up.
    pause
    exit /b 1
)
goto :deno_ok

:deno_no_winget
echo   No winget available. Install Deno manually: https://deno.land/
goto :deno_ok

:deno_skip
echo Skipping. YouTube downloads will likely fail without a JS runtime.

:deno_ok

REM =========================================================================
REM Virtual environment + Python dependencies
REM =========================================================================
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

"%VENV_PY%" -c "import yt_dlp, customtkinter, arabic_reshaper; import bidi.algorithm" >nul 2>nul
if errorlevel 1 (
    echo Installing Python dependencies...
    "%VENV_PY%" -m pip install --upgrade pip
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install Python dependencies.
        pause
        exit /b 1
    )
)

"%VENV_PY%" app.py %*
endlocal
