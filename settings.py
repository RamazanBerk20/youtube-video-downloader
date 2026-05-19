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


_LANGUAGES = {"", "en", "tr", "es", "fr", "de", "ru", "ar", "zh", "ja"}
_MODES = {"video", "audio"}
_QUALITIES = {
    "Best", "4K (2160p)", "1440p", "1080p", "720p", "480p", "360p",
    "Worst", "320 kbps", "256 kbps", "192 kbps", "128 kbps", "96 kbps",
}
_VIDEO_CODECS = {"Auto", "MP4 (H.264)", "WebM (VP9)", "WebM (AV1)"}
_AUDIO_CODECS = {
    "m4a (AAC)", "mp3", "opus", "vorbis (ogg)", "flac", "wav", "alac",
    "ac3", "Original",
}
_CONTAINERS = {"None", "MP4", "MKV", "WebM", "MOV", "AVI", "FLV", "MPEG", "WMV"}
# Quality/speed tradeoff for the compatibility re-encode pass. Maps to a
# per-encoder preset+CRF combo in downloader._ENCODER_PRESETS. "Balanced"
# is the default — fast enough for HW encoders to barely notice, slow
# enough for libx264 to produce a reasonable file.
_REENCODE_QUALITIES = {"Fast", "Balanced", "Quality"}
_COOKIE_BROWSERS = {
    "Auto", "Off", "Firefox", "Chrome", "Brave", "Chromium", "Edge",
    "Opera", "Safari", "Vivaldi",
}


def _coerce_int(value: object, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _choice(value: object, valid: set[str], default: str) -> str:
    return value if isinstance(value, str) and value in valid else default


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
    # Optional output container for video downloads. "None" leaves yt-dlp's
    # natural output (.mp4 for H.264, .webm/.mkv otherwise); any other key
    # (MP4, MKV, MOV, AVI, FLV, MPEG, WebM, WMV) triggers either a remux or,
    # for legacy containers, a compatibility transcode.
    container: str = "None"
    # When True, video downloads run through a real ffmpeg re-encode pass.
    # Codec choice follows the target container: H.264/AAC for MP4/MOV/MKV,
    # MPEG-2/MP2 for MPEG, Xvid/MP3 for AVI, etc. Defaults to mp4 when
    # container is "None".
    reencode_h264: bool = False
    # Per-encoder speed/quality preset for the re-encode pass. Fast/
    # Balanced/Quality maps to e.g. NVENC p2/p5/p7 or libx264 ultrafast/
    # medium/slow. Only consulted when reencode_h264 is True.
    reencode_quality: str = "Balanced"
    playlist: bool = False
    auto_update_check: bool = True
    # Per-video parallel HTTP fragment downloads. YouTube throttles each
    # single TCP stream, so splitting one file across N connections is what
    # actually unlocks >2 MB/s on fast home connections. yt-dlp default is 1.
    concurrent_fragments: int = 4
    # Browser whose cookie jar yt-dlp should send with each request.
    # "Auto" = no cookies normally, but the app auto-detects the user's
    # browser and switches mid-session when YouTube's bot-check fires.
    # "Off" = never send cookies. An explicit browser name forces that
    # browser's cookies on every request.
    cookies_browser: str = "Auto"

    def __post_init__(self) -> None:
        # Treat the config file as user-editable input. Bad values should
        # fall back to defaults instead of crashing UI construction.
        self.language = _choice(self.language, _LANGUAGES, "")
        self.max_concurrent = _coerce_int(self.max_concurrent, 2)
        self.output_dir = self.output_dir if isinstance(self.output_dir, str) else ""
        self.mode = _choice(self.mode, _MODES, "video")
        self.quality = _choice(self.quality, _QUALITIES, "Best")
        self.video_codec = _choice(self.video_codec, _VIDEO_CODECS, "Auto")
        self.audio_codec = _choice(self.audio_codec, _AUDIO_CODECS, "m4a (AAC)")
        self.container = _choice(self.container, _CONTAINERS, "None")
        self.reencode_h264 = _coerce_bool(self.reencode_h264)
        self.reencode_quality = _choice(
            self.reencode_quality, _REENCODE_QUALITIES, "Balanced"
        )
        self.playlist = _coerce_bool(self.playlist)
        self.auto_update_check = _coerce_bool(self.auto_update_check)
        self.concurrent_fragments = _coerce_int(self.concurrent_fragments, 4)
        self.cookies_browser = _choice(self.cookies_browser, _COOKIE_BROWSERS, "Auto")

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
