#!/usr/bin/env bash
# Launches the YouTube Downloader GUI on Linux/macOS.
# On first run, creates a .venv and installs dependencies.
set -e

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR=".venv"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python ('$PYTHON_BIN') is not installed."
    # Detect a usable package manager + elevation helper
    PM=""
    PM_ARGS=""
    if command -v pacman >/dev/null 2>&1; then
        PM="pacman"; PM_ARGS="-S --noconfirm python"
    elif command -v apt-get >/dev/null 2>&1; then
        PM="apt-get"; PM_ARGS="install -y python3 python3-venv python3-pip"
    elif command -v dnf >/dev/null 2>&1; then
        PM="dnf"; PM_ARGS="install -y python3 python3-pip"
    elif command -v zypper >/dev/null 2>&1; then
        PM="zypper"; PM_ARGS="--non-interactive install python3 python3-pip"
    elif command -v brew >/dev/null 2>&1; then
        PM="brew"; PM_ARGS="install python"
    fi
    ELEV=""
    if [ "$PM" != "brew" ] && [ -n "$PM" ]; then
        if command -v pkexec >/dev/null 2>&1; then
            ELEV="pkexec"
        elif command -v sudo >/dev/null 2>&1; then
            ELEV="sudo"
        fi
    fi
    if [ -n "$PM" ]; then
        echo "Would install via:  ${ELEV:+$ELEV }$PM $PM_ARGS"
        printf "Install now? [Y/n] "
        read -r reply
        case "$reply" in
            ""|y|Y|yes|YES)
                # shellcheck disable=SC2086
                ${ELEV:+$ELEV} $PM $PM_ARGS || {
                    echo "Install failed. Re-run after installing Python manually." >&2
                    exit 1
                }
                ;;
            *)
                echo "Aborted. Install Python 3.10+ and re-run." >&2
                exit 1
                ;;
        esac
    else
        echo "No supported package manager found. Install Python 3.10+ manually:" >&2
        echo "  https://www.python.org/downloads/" >&2
        exit 1
    fi
    # Verify the install worked before continuing
    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        echo "Python install reported success but '$PYTHON_BIN' is still not on PATH." >&2
        exit 1
    fi
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Warning: ffmpeg not found in PATH. Audio extraction and video+audio merging will fail." >&2
    echo "         Install via your package manager, e.g.:" >&2
    echo "           sudo pacman -S ffmpeg     # Arch / CachyOS" >&2
    echo "           sudo apt install ffmpeg   # Debian / Ubuntu" >&2
    echo "           brew install ffmpeg       # macOS" >&2
fi

# Git is optional but recommended — without it the in-app auto-updater
# silently bails out (because it can't fetch from origin). Offer to install
# the same way we offer Python install above.
if ! command -v git >/dev/null 2>&1; then
    echo "Note: 'git' is not installed. Auto-update checks will be disabled."
    GIT_PM=""
    GIT_PM_ARGS=""
    if command -v pacman >/dev/null 2>&1; then
        GIT_PM="pacman"; GIT_PM_ARGS="-S --noconfirm git"
    elif command -v apt-get >/dev/null 2>&1; then
        GIT_PM="apt-get"; GIT_PM_ARGS="install -y git"
    elif command -v dnf >/dev/null 2>&1; then
        GIT_PM="dnf"; GIT_PM_ARGS="install -y git"
    elif command -v zypper >/dev/null 2>&1; then
        GIT_PM="zypper"; GIT_PM_ARGS="--non-interactive install git"
    elif command -v brew >/dev/null 2>&1; then
        GIT_PM="brew"; GIT_PM_ARGS="install git"
    fi
    GIT_ELEV=""
    if [ "$GIT_PM" != "brew" ] && [ -n "$GIT_PM" ]; then
        if command -v pkexec >/dev/null 2>&1; then
            GIT_ELEV="pkexec"
        elif command -v sudo >/dev/null 2>&1; then
            GIT_ELEV="sudo"
        fi
    fi
    if [ -n "$GIT_PM" ]; then
        echo "Would install via:  ${GIT_ELEV:+$GIT_ELEV }$GIT_PM $GIT_PM_ARGS"
        printf "Install now? [Y/n] "
        read -r git_reply
        case "$git_reply" in
            ""|y|Y|yes|YES)
                # shellcheck disable=SC2086
                ${GIT_ELEV:+$GIT_ELEV} $GIT_PM $GIT_PM_ARGS || {
                    echo "git install failed — continuing without auto-update." >&2
                }
                ;;
            *)
                echo "Skipping. Auto-update remains disabled." >&2
                ;;
        esac
    else
        echo "  No supported package manager found. Install git manually if you want auto-updates." >&2
    fi
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "First-time setup: creating virtual environment in $VENV_DIR…"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"

if ! "$VENV_PY" -c "import yt_dlp, customtkinter" >/dev/null 2>&1; then
    echo "Installing dependencies…"
    "$VENV_PY" -m pip install --upgrade pip >/dev/null
    "$VENV_PY" -m pip install -r requirements.txt
fi

exec "$VENV_PY" app.py "$@"
