#!/usr/bin/env bash
# Launches the YouTube Downloader GUI on Linux/macOS.
#
# Verifies every dependency the app needs is installed BEFORE handing off
# to the Python GUI, and offers to install anything missing via the host
# package manager. Dependencies checked:
#   - python3   (the interpreter itself)
#   - pip       (provided by python3 normally; pulled in alongside Python)
#   - git       (optional, but the in-app auto-updater needs it)
#   - ffmpeg    (required for video+audio merging and audio extraction)
#   - deno      (yt-dlp's recommended JS runtime — without one, YouTube's
#                anti-bot challenge cannot be solved and downloads fail
#                with "Requested format is not available")
set -e

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR=".venv"

# --- Package-manager + elevation detection -------------------------------

_PM=""
_PM_NEEDS_ELEV=1   # 0 = no, 1 = yes
if command -v pacman >/dev/null 2>&1; then
    _PM="pacman"
elif command -v apt-get >/dev/null 2>&1; then
    _PM="apt"
elif command -v dnf >/dev/null 2>&1; then
    _PM="dnf"
elif command -v zypper >/dev/null 2>&1; then
    _PM="zypper"
elif command -v brew >/dev/null 2>&1; then
    _PM="brew"
    _PM_NEEDS_ELEV=0
fi

_ELEV=""
if [ "$_PM_NEEDS_ELEV" = "1" ] && [ -n "$_PM" ]; then
    if command -v pkexec >/dev/null 2>&1; then
        _ELEV="pkexec"
    elif command -v sudo >/dev/null 2>&1; then
        _ELEV="sudo"
    fi
fi

# Resolve (package_label) → install argv for the detected PM. Echoes the
# argv as space-separated tokens; caller must `eval` or `read -a` to use.
# Labels:
#   python     → distro Python 3 + venv + pip
#   git        → git
#   ffmpeg     → ffmpeg
#   deno       → deno (Arch + brew have packages; Debian/Fedora/openSUSE
#                fall through and the caller prints the curl install hint)
_install_args() {
    local label="$1"
    case "$_PM:$label" in
        pacman:python)  echo "pacman -S --noconfirm python" ;;
        apt:python)     echo "apt-get install -y python3 python3-venv python3-pip" ;;
        dnf:python)     echo "dnf install -y python3 python3-pip" ;;
        zypper:python)  echo "zypper --non-interactive install python3 python3-pip" ;;
        brew:python)    echo "brew install python" ;;

        pacman:git)     echo "pacman -S --noconfirm git" ;;
        apt:git)        echo "apt-get install -y git" ;;
        dnf:git)        echo "dnf install -y git" ;;
        zypper:git)     echo "zypper --non-interactive install git" ;;
        brew:git)       echo "brew install git" ;;

        pacman:ffmpeg)  echo "pacman -S --noconfirm ffmpeg" ;;
        apt:ffmpeg)     echo "apt-get install -y ffmpeg" ;;
        dnf:ffmpeg)     echo "dnf install -y ffmpeg" ;;
        zypper:ffmpeg)  echo "zypper --non-interactive install ffmpeg" ;;
        brew:ffmpeg)    echo "brew install ffmpeg" ;;

        pacman:deno)    echo "pacman -S --noconfirm deno" ;;
        brew:deno)      echo "brew install deno" ;;
        # apt/dnf/zypper have no official deno package — handled by caller.
        *) echo "" ;;
    esac
}

# Prompt and run an install. Args: <label> <human readable description>
_offer_install() {
    local label="$1"
    local description="$2"
    local args
    args="$(_install_args "$label")"

    # Deno on Debian/Fedora/openSUSE has no native package. Tell the user
    # exactly what to run and abort the auto-install path.
    if [ -z "$args" ] && [ "$label" = "deno" ]; then
        echo "  No '$label' package on $_PM. Install Deno manually:"
        echo "    curl -fsSL https://deno.land/install.sh | sh"
        echo "  Then re-run start.sh."
        return 1
    fi
    if [ -z "$args" ]; then
        echo "  No automated install recipe for '$label' on $_PM. Install it manually." >&2
        return 1
    fi
    echo "$description is not installed."
    echo "Would install via:  ${_ELEV:+$_ELEV }$args"
    printf "Install now? [Y/n] "
    read -r reply
    case "$reply" in
        ""|y|Y|yes|YES)
            # shellcheck disable=SC2086
            ${_ELEV:+$_ELEV} $args || {
                echo "Install failed." >&2
                return 1
            }
            ;;
        *)
            echo "Skipped." >&2
            return 1
            ;;
    esac
    return 0
}

# --- Python --------------------------------------------------------------

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    if [ -z "$_PM" ]; then
        echo "Python ('$PYTHON_BIN') is not installed and no supported package manager was found." >&2
        echo "Install Python 3.10+ manually: https://www.python.org/downloads/" >&2
        exit 1
    fi
    _offer_install python "Python 3" || {
        echo "Aborted. Install Python 3.10+ and re-run." >&2
        exit 1
    }
    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        echo "Python install reported success but '$PYTHON_BIN' is still not on PATH." >&2
        exit 1
    fi
fi

# --- git (optional but recommended) --------------------------------------

if ! command -v git >/dev/null 2>&1; then
    echo "Note: 'git' is not installed. Auto-update checks will be disabled."
    _offer_install git "git" || true
fi

# --- ffmpeg --------------------------------------------------------------

if ! command -v ffmpeg >/dev/null 2>&1; then
    _offer_install ffmpeg "ffmpeg" || {
        echo "Warning: continuing without ffmpeg. Audio extraction and video+audio merging will fail." >&2
    }
fi

# --- JS runtime (deno preferred) -----------------------------------------
#
# yt-dlp's YouTube extractor now requires a JS runtime to solve an anti-
# bot challenge. Without one, downloads fall back to image-only formats
# and fail with "Requested format is not available". Deno is the
# recommended option (sandboxed by default).

if ! command -v deno >/dev/null 2>&1 \
        && ! command -v node >/dev/null 2>&1 \
        && ! command -v bun >/dev/null 2>&1; then
    _offer_install deno "Deno (JavaScript runtime for yt-dlp's anti-bot solver)" || {
        echo "Warning: continuing without a JS runtime. YouTube downloads will likely fail" >&2
        echo "         with 'Requested format is not available'." >&2
    }
fi

# --- Python venv + Python deps -------------------------------------------

if [ ! -d "$VENV_DIR" ]; then
    echo "First-time setup: creating virtual environment in $VENV_DIR…"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"

if ! "$VENV_PY" -c "import yt_dlp, customtkinter, arabic_reshaper; import bidi.algorithm" >/dev/null 2>&1; then
    echo "Installing Python dependencies…"
    "$VENV_PY" -m pip install --upgrade pip >/dev/null
    "$VENV_PY" -m pip install -r requirements.txt
fi

# Hand off to the GUI. `exec` so we don't keep a useless shell parent around.
exec "$VENV_PY" app.py "$@"
