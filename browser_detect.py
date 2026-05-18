"""Detect the user's default web browser, returning a yt-dlp-compatible
name (lowercase) when one can be found.

Strategy:
  1. Ask the OS for its registered default browser (xdg-settings on Linux,
     UrlAssociations registry key on Windows; macOS skipped — no clean
     shell command).
  2. If that fails or the named browser has no cookie jar on disk, fall
     back to scanning a priority list of known cookie-jar locations.
  3. Give up and return None — the caller falls back to "Off" behaviour.

We deliberately do NOT crack open the cookie databases to check whether
they hold a YouTube session. yt-dlp does that when it actually loads the
jar, and surfacing the failure to the user is more useful than guessing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# yt-dlp browser keys → candidate cookie-jar directories on this OS.
# The first existing one wins; an empty entry means "browser unsupported
# on this platform" (Safari outside macOS, for example).
_COOKIE_JAR_PATHS: dict[str, list[Path]] = {}


def _expand(p: str) -> Path:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    localappdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    return Path(
        p.replace("$HOME", str(home))
         .replace("$APPDATA", str(appdata))
         .replace("$LOCALAPPDATA", str(localappdata))
    )


def _add_paths(browser: str, *paths: str) -> None:
    _COOKIE_JAR_PATHS[browser] = [_expand(p) for p in paths]


if sys.platform == "win32":
    _add_paths("firefox",  r"$APPDATA\Mozilla\Firefox\Profiles")
    _add_paths("chrome",   r"$LOCALAPPDATA\Google\Chrome\User Data")
    _add_paths("brave",    r"$LOCALAPPDATA\BraveSoftware\Brave-Browser\User Data")
    _add_paths("chromium", r"$LOCALAPPDATA\Chromium\User Data")
    _add_paths("edge",     r"$LOCALAPPDATA\Microsoft\Edge\User Data")
    _add_paths("opera",    r"$APPDATA\Opera Software\Opera Stable")
    _add_paths("vivaldi",  r"$LOCALAPPDATA\Vivaldi\User Data")
elif sys.platform == "darwin":
    _add_paths("firefox",  "$HOME/Library/Application Support/Firefox/Profiles")
    _add_paths("chrome",   "$HOME/Library/Application Support/Google/Chrome")
    _add_paths("brave",    "$HOME/Library/Application Support/BraveSoftware/Brave-Browser")
    _add_paths("chromium", "$HOME/Library/Application Support/Chromium")
    _add_paths("edge",     "$HOME/Library/Application Support/Microsoft Edge")
    _add_paths("opera",    "$HOME/Library/Application Support/com.operasoftware.Opera")
    _add_paths("safari",   "$HOME/Library/Cookies")
    _add_paths("vivaldi",  "$HOME/Library/Application Support/Vivaldi")
else:  # Linux / BSD / other Unix
    _add_paths("firefox",  "$HOME/.mozilla/firefox")
    _add_paths("chrome",   "$HOME/.config/google-chrome")
    _add_paths("brave",    "$HOME/.config/BraveSoftware/Brave-Browser")
    _add_paths("chromium", "$HOME/.config/chromium")
    _add_paths("edge",     "$HOME/.config/microsoft-edge")
    _add_paths("opera",    "$HOME/.config/opera")
    _add_paths("vivaldi",  "$HOME/.config/vivaldi")


def _has_cookie_jar(browser: str) -> bool:
    return any(p.exists() for p in _COOKIE_JAR_PATHS.get(browser, []))


# ---- OS-level default browser ---------------------------------------------

# `xdg-settings get default-web-browser` returns a .desktop basename like
# "firefox.desktop" or "google-chrome.desktop". Strip the suffix, match
# against this table (lowercased), and return the yt-dlp key.
_LINUX_DESKTOP_MAP: dict[str, str] = {
    "firefox": "firefox",
    "firefox-esr": "firefox",
    "firefox-developer-edition": "firefox",
    "firefox-nightly": "firefox",
    "google-chrome": "chrome",
    "chromium": "chromium",
    "chromium-browser": "chromium",
    "brave-browser": "brave",
    "brave": "brave",
    "microsoft-edge": "edge",
    "microsoft-edge-stable": "edge",
    "opera": "opera",
    "vivaldi-stable": "vivaldi",
    "vivaldi": "vivaldi",
}


def _linux_default_browser() -> str | None:
    if not shutil.which("xdg-settings"):
        return None
    try:
        r = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    name = (r.stdout or "").strip().lower()
    if name.endswith(".desktop"):
        name = name[: -len(".desktop")]
    return _LINUX_DESKTOP_MAP.get(name)


# Windows registers the default http handler under a ProgId. Map common
# values (case-insensitive prefix) to yt-dlp keys. IE (`IE.HTTP`) maps to
# None because yt-dlp can't read its cookies anyway.
_WINDOWS_PROGID_MAP: list[tuple[str, str | None]] = [
    ("chromehtml",  "chrome"),
    ("firefoxurl",  "firefox"),
    ("msedgehtm",   "edge"),
    ("bravehtml",   "brave"),
    ("bravebhtm",   "brave"),
    ("operastable", "opera"),
    ("vivaldihtm",  "vivaldi"),
    ("ie.http",     None),
]


def _windows_default_browser() -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    key_path = (
        r"Software\Microsoft\Windows\Shell\Associations"
        r"\UrlAssociations\http\UserChoice"
    )
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            progid = winreg.QueryValueEx(k, "ProgId")[0]
    except OSError:
        return None
    p = (progid or "").lower()
    for prefix, browser in _WINDOWS_PROGID_MAP:
        if p.startswith(prefix):
            return browser
    return None


# ---- Public API -----------------------------------------------------------


def detect_default_browser() -> str | None:
    """Return a lowercase yt-dlp browser key for the user's default browser.

    Convenience wrapper that returns just the first candidate from
    `candidate_browsers()` — useful when caller only needs one guess."""
    cands = candidate_browsers()
    return cands[0] if cands else None


# Chromium-based browser keys whose cookies use App-Bound Encryption on
# Windows since Chrome 127 (yt-dlp issue #10927). yt-dlp's DPAPI decrypt
# fails for these, so Auto-mode should deprioritise them on Windows.
_WINDOWS_DPAPI_BLOCKED: frozenset[str] = frozenset({
    "chrome", "edge", "brave", "chromium", "vivaldi", "opera",
})


def candidate_browsers() -> list[str]:
    """Return browsers with cookie jars on disk, in the order Auto should
    try them. Each browser appears at most once.

    Windows-specific twist: Chromium-based browsers (Chrome, Edge, Brave,
    etc.) almost always fail with "Failed to decrypt with DPAPI" since
    Chrome 127 added App-Bound Encryption. So on Windows, Firefox is
    promoted to first place when it's available, regardless of which
    browser the OS reports as default."""
    candidates: list[str] = []

    # OS-registered default first — usually what the user is logged into.
    if sys.platform == "linux":
        default = _linux_default_browser()
    elif sys.platform == "win32":
        default = _windows_default_browser()
    else:
        default = None  # macOS skipped; cookie-jar scan below handles it

    # Windows DPAPI workaround: if the default is Chromium-based AND
    # Firefox is installed, jump Firefox to the front. Otherwise leave
    # the default where it is.
    if (sys.platform == "win32"
            and default in _WINDOWS_DPAPI_BLOCKED
            and _has_cookie_jar("firefox")):
        candidates.append("firefox")

    if default and default not in candidates and _has_cookie_jar(default):
        candidates.append(default)

    # Generic priority for the remainder. Firefox first because it's the
    # one browser whose cookie jar yt-dlp can actually read on every OS;
    # Safari last because it only matters on macOS.
    for b in ("firefox", "chrome", "brave", "chromium", "edge",
              "vivaldi", "opera", "safari"):
        if b not in candidates and _has_cookie_jar(b):
            candidates.append(b)

    return candidates


def display_name_for(yt_dlp_key: str) -> str:
    """Capitalised form of a yt-dlp browser key for log/banner display.
    e.g. 'firefox' → 'Firefox', 'edge' → 'Edge'."""
    return yt_dlp_key[:1].upper() + yt_dlp_key[1:].lower() if yt_dlp_key else ""
