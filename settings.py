"""JSON-backed user settings.

Stores language choice, max-concurrent downloads, and the last-used output
directory under the platform-conventional config location:
  Windows:  %APPDATA%\\youtube-downloader\\config.json
  macOS:    ~/Library/Application Support/youtube-downloader/config.json
  Linux:    $XDG_CONFIG_HOME/youtube-downloader/config.json  (default ~/.config)
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "youtube-downloader" / "config.json"


@dataclass
class Settings:
    language: str = ""          # "" → autodetect at first run
    max_concurrent: int = 2
    output_dir: str = ""        # "" → default to ~/Downloads
    mode: str = "video"
    quality: str = "Best"
    video_codec: str = "Auto"
    audio_codec: str = "m4a (AAC)"
    force_mp4: bool = False
    playlist: bool = False
    auto_update_check: bool = True

    @classmethod
    def load(cls) -> "Settings":
        path = config_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        valid = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in valid})

    def save(self) -> None:
        path = config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass
