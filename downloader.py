"""Background yt-dlp worker with thread-safe progress reporting.

A `DownloadWorker` runs yt-dlp on a daemon thread and posts events to a
`queue.Queue` that the GUI polls from the main thread. This keeps Tk
responsive and avoids cross-thread widget access.
"""
from __future__ import annotations

import queue
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yt_dlp


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


class CancelledError(Exception):
    """Raised inside progress hooks to abort a download cleanly."""


@dataclass
class DownloadJob:
    url: str
    output_dir: Path
    mode: str  # "video" | "audio"
    quality: str
    playlist: bool


_VIDEO_FORMATS: dict[str, str] = {
    "Best": "bestvideo*+bestaudio/best",
    "4K (2160p)": "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
    "1440p": "bestvideo[height<=1440]+bestaudio/best[height<=1440]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "Worst": "worstvideo+worstaudio/worst",
}

_AUDIO_BITRATES: dict[str, str] = {
    "Best": "0",
    "320 kbps": "320",
    "256 kbps": "256",
    "192 kbps": "192",
    "128 kbps": "128",
    "96 kbps": "96",
}


def video_quality_options() -> list[str]:
    return list(_VIDEO_FORMATS.keys())


def audio_quality_options() -> list[str]:
    return list(_AUDIO_BITRATES.keys())


class DownloadWorker:
    """Runs yt-dlp on a background thread and posts events to a queue.

    Events are dicts with a `type` key:
      {"type": "log",           "message": str}
      {"type": "progress",      "percent": float, "speed": str, "eta": str,
                                "filename": str, "index": int|None,
                                "total": int|None}
      {"type": "finished_file", "filename": str}
      {"type": "done",          "ok": bool, "error": str | None}
    """

    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, job: DownloadJob) -> None:
        if self.is_running:
            raise RuntimeError("A download is already in progress.")
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    # ---- internals --------------------------------------------------------

    def _emit(self, **kw: Any) -> None:
        self.events.put(kw)

    def _progress_hook(self, d: dict[str, Any]) -> None:
        if self._cancel.is_set():
            raise CancelledError
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100.0) if total else 0.0
            info = d.get("info_dict") or {}
            self._emit(
                type="progress",
                percent=percent,
                speed=_fmt_speed(d.get("speed")),
                eta=_fmt_eta(d.get("eta")),
                filename=Path(d.get("filename") or "").name,
                index=info.get("playlist_index"),
                total=info.get("n_entries"),
            )
        elif status == "finished":
            self._emit(
                type="finished_file",
                filename=Path(d.get("filename") or "").name,
            )

    def _postprocessor_hook(self, d: dict[str, Any]) -> None:
        if self._cancel.is_set():
            raise CancelledError
        if d.get("status") == "started":
            pp = d.get("postprocessor") or "postprocessor"
            self._emit(type="log", message=f"… {pp}")

    def _build_opts(self, job: DownloadJob) -> dict[str, Any]:
        if job.playlist:
            outtmpl = str(
                job.output_dir
                / "%(playlist_title|playlist)s"
                / "%(playlist_index)03d - %(title).200B [%(id)s].%(ext)s"
            )
        else:
            outtmpl = str(job.output_dir / "%(title).200B [%(id)s].%(ext)s")

        opts: dict[str, Any] = {
            "outtmpl": outtmpl,
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._postprocessor_hook],
            "noprogress": True,
            "quiet": True,
            "no_warnings": False,
            "no_color": True,
            "noplaylist": not job.playlist,
            "ignoreerrors": False,
            "logger": _QuietLogger(self._emit),
            "windowsfilenames": True,
            "restrictfilenames": False,
            "concurrent_fragment_downloads": 4,
            "retries": 5,
            "fragment_retries": 5,
        }

        if job.mode == "audio":
            bitrate = _AUDIO_BITRATES.get(job.quality, "0")
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": bitrate,
                }
            ]
        else:
            opts["format"] = _VIDEO_FORMATS.get(job.quality, _VIDEO_FORMATS["Best"])
            opts["merge_output_format"] = "mp4"
        return opts

    def _run(self, job: DownloadJob) -> None:
        try:
            job.output_dir.mkdir(parents=True, exist_ok=True)
            opts = self._build_opts(job)
            self._emit(type="log", message=f"Starting: {job.url}")
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([job.url])
            self._emit(type="done", ok=True, error=None)
        except CancelledError:
            self._emit(type="done", ok=False, error="cancelled")
        except yt_dlp.utils.DownloadError as e:
            err = _strip_ansi(str(e))
            if "ffmpeg is not installed" in err.lower() or "ffmpeg is not installed" in err:
                err = (
                    "ffmpeg is required to merge video + audio or extract mp3, "
                    "but it was not found on PATH.\n"
                    "Install it and try again:\n"
                    "  Windows:  winget install Gyan.FFmpeg\n"
                    "  Arch:     sudo pacman -S ffmpeg\n"
                    "  Debian:   sudo apt install ffmpeg\n"
                    "  macOS:    brew install ffmpeg\n"
                    "\n"
                    "Original error: " + err
                )
            self._emit(type="done", ok=False, error=err)
        except Exception as e:  # noqa: BLE001
            self._emit(type="done", ok=False, error=f"{type(e).__name__}: {e}")


class _QuietLogger:
    """Funnels yt-dlp warnings/errors into our event queue.

    `debug` is silenced because `quiet=True` already suppresses normal output;
    real errors propagate via the DownloadError exception path.
    """

    def __init__(self, emit: Callable[..., None]) -> None:
        self._emit = emit

    def debug(self, msg: str) -> None:
        return

    def info(self, msg: str) -> None:
        if msg:
            self._emit(type="log", message=_strip_ansi(msg))

    def warning(self, msg: str) -> None:
        self._emit(type="log", message=f"warning: {_strip_ansi(msg)}")

    def error(self, msg: str) -> None:
        self._emit(type="log", message=f"error: {_strip_ansi(msg)}")


def _fmt_speed(speed: float | None) -> str:
    if not speed:
        return "—"
    value = float(speed)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB/s"


def _fmt_eta(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"
