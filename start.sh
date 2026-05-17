#!/usr/bin/env bash
# Launches the YouTube Downloader GUI on Linux/macOS.
# On first run, creates a .venv and installs dependencies.
set -e

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR=".venv"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Error: '$PYTHON_BIN' not found. Install Python 3.10+ and try again." >&2
    echo "       (Override the interpreter with PYTHON_BIN=/path/to/python ./start.sh)" >&2
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Warning: ffmpeg not found in PATH. Audio extraction and video+audio merging will fail." >&2
    echo "         Install via your package manager, e.g.:" >&2
    echo "           sudo pacman -S ffmpeg     # Arch / CachyOS" >&2
    echo "           sudo apt install ffmpeg   # Debian / Ubuntu" >&2
    echo "           brew install ffmpeg       # macOS" >&2
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
