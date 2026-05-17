"""Concurrent yt-dlp download manager.

Each `DownloadTask` runs on its own daemon thread. The `DownloadManager`
keeps a dict of tasks and is scheduled cooperatively from the GUI's poll
loop: every tick, `schedule(max_concurrent=N)` starts the next queued
task(s) if fewer than N are running. Per-task progress, errors, and
lifecycle events are published through a single thread-safe queue.
"""
from __future__ import annotations

import itertools
import queue
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yt_dlp


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


# ---- Quality option tables --------------------------------------------------

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


# ---- Task ------------------------------------------------------------------


class _CancelledError(Exception):
    pass


@dataclass
class DownloadTask:
    id: int
    url: str
    output_dir: Path
    mode: str
    quality: str
    playlist: bool

    status: str = "queued"   # queued | running | done | failed | cancelled
    title: str = ""
    percent: float = 0.0
    speed: str = "—"
    eta: str = "—"
    error: str = ""

    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)


# ---- Manager ---------------------------------------------------------------


class DownloadManager:
    """Holds the task queue and starts threads up to a per-tick concurrency cap.

    Events published to `self.events`:
      {"type": "task_added",     "task_id": int}
      {"type": "task_started",   "task_id": int}
      {"type": "task_progress",  "task_id": int}      # task fields are updated in place
      {"type": "task_done",      "task_id": int, "ok": bool}
      {"type": "task_log",       "task_id": int, "message": str}
      {"type": "log",            "message": str}      # global, no task
    """

    def __init__(self) -> None:
        self.tasks: dict[int, DownloadTask] = {}
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._id_seq = itertools.count(1)
        self._lock = threading.Lock()

    # ---- Public API ---------------------------------------------------

    def add(
        self,
        url: str,
        output_dir: Path,
        mode: str,
        quality: str,
        playlist: bool,
    ) -> DownloadTask:
        task = DownloadTask(
            id=next(self._id_seq),
            url=url,
            output_dir=output_dir,
            mode=mode,
            quality=quality,
            playlist=playlist,
            title=url,
        )
        with self._lock:
            self.tasks[task.id] = task
        self.events.put({"type": "task_added", "task_id": task.id})
        return task

    def cancel(self, task_id: int) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        if task.status == "queued":
            task.status = "cancelled"
            self.events.put({"type": "task_done", "task_id": task.id, "ok": False})
        elif task.status == "running":
            task._cancel.set()  # cooperatively aborts via progress hook

    def cancel_all(self) -> None:
        for tid in list(self.tasks.keys()):
            self.cancel(tid)

    def clear_done(self) -> int:
        finished = {"done", "failed", "cancelled"}
        with self._lock:
            removed = [tid for tid, t in self.tasks.items() if t.status in finished]
            for tid in removed:
                del self.tasks[tid]
        return len(removed)

    def schedule(self, max_concurrent: int) -> None:
        """Start as many queued tasks as fit under `max_concurrent`."""
        with self._lock:
            running = sum(1 for t in self.tasks.values() if t.status == "running")
            slots = max(0, max_concurrent - running)
            if slots == 0:
                return
            queued = [t for t in self.tasks.values() if t.status == "queued"]
        for task in queued[:slots]:
            self._start(task)

    # ---- Internal -----------------------------------------------------

    def _start(self, task: DownloadTask) -> None:
        task.status = "running"
        self.events.put({"type": "task_started", "task_id": task.id})
        task._thread = threading.Thread(target=self._run, args=(task,), daemon=True)
        task._thread.start()

    def _run(self, task: DownloadTask) -> None:
        try:
            task.output_dir.mkdir(parents=True, exist_ok=True)
            opts = self._build_opts(task)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([task.url])
            task.status = "done"
            task.percent = 100.0
            self.events.put({"type": "task_done", "task_id": task.id, "ok": True})
        except _CancelledError:
            task.status = "cancelled"
            self.events.put({"type": "task_done", "task_id": task.id, "ok": False})
        except yt_dlp.utils.DownloadError as e:
            task.status = "failed"
            task.error = self._friendly_error(strip_ansi(str(e)))
            self.events.put({"type": "task_done", "task_id": task.id, "ok": False})
        except Exception as e:  # noqa: BLE001
            task.status = "failed"
            task.error = f"{type(e).__name__}: {e}"
            self.events.put({"type": "task_done", "task_id": task.id, "ok": False})

    def _friendly_error(self, raw: str) -> str:
        low = raw.lower()
        if "ffmpeg is not installed" in low:
            return (
                "ffmpeg is required (merging streams or extracting mp3) "
                "but is not on PATH. Install:\n"
                "  Windows:  winget install Gyan.FFmpeg\n"
                "  Arch:     sudo pacman -S ffmpeg\n"
                "  Debian:   sudo apt install ffmpeg\n"
                "  macOS:    brew install ffmpeg\n"
                "Original: " + raw
            )
        return raw

    def _build_opts(self, task: DownloadTask) -> dict[str, Any]:
        if task.playlist:
            outtmpl = str(
                task.output_dir
                / "%(playlist_title|playlist)s"
                / "%(playlist_index)03d - %(title).200B [%(id)s].%(ext)s"
            )
        else:
            outtmpl = str(task.output_dir / "%(title).200B [%(id)s].%(ext)s")

        def progress_hook(d: dict[str, Any]) -> None:
            if task._cancel.is_set():
                raise _CancelledError
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes") or 0
                task.percent = (downloaded / total * 100.0) if total else 0.0
                task.speed = _fmt_speed(d.get("speed"))
                task.eta = _fmt_eta(d.get("eta"))
                info = d.get("info_dict") or {}
                title = info.get("title") or Path(d.get("filename") or "").name
                if title:
                    task.title = title
                self.events.put({"type": "task_progress", "task_id": task.id})
            elif status == "finished":
                name = Path(d.get("filename") or "").name
                self.events.put(
                    {"type": "task_log", "task_id": task.id, "message": name}
                )

        def postprocessor_hook(d: dict[str, Any]) -> None:
            if task._cancel.is_set():
                raise _CancelledError

        opts: dict[str, Any] = {
            "outtmpl": outtmpl,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
            "noprogress": True,
            "quiet": True,
            "no_warnings": False,
            "no_color": True,
            "noplaylist": not task.playlist,
            "ignoreerrors": False,
            "logger": _TaskLogger(self.events, task.id),
            "windowsfilenames": True,
            "restrictfilenames": False,
            "concurrent_fragment_downloads": 4,
            "retries": 5,
            "fragment_retries": 5,
        }

        if task.mode == "audio":
            bitrate = _AUDIO_BITRATES.get(task.quality, "0")
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": bitrate,
                }
            ]
        else:
            opts["format"] = _VIDEO_FORMATS.get(task.quality, _VIDEO_FORMATS["Best"])
            opts["merge_output_format"] = "mp4"
        return opts


# ---- Helpers ---------------------------------------------------------------


class _TaskLogger:
    """yt-dlp logger that funnels lines into the manager's event queue."""

    def __init__(self, events: queue.Queue, task_id: int) -> None:
        self._events = events
        self._task_id = task_id

    def debug(self, msg: str) -> None:
        return

    def info(self, msg: str) -> None:
        if msg:
            self._events.put(
                {"type": "task_log", "task_id": self._task_id, "message": strip_ansi(msg)}
            )

    def warning(self, msg: str) -> None:
        self._events.put(
            {"type": "task_log", "task_id": self._task_id,
             "message": "warning: " + strip_ansi(msg)}
        )

    def error(self, msg: str) -> None:
        self._events.put(
            {"type": "task_log", "task_id": self._task_id,
             "message": "error: " + strip_ansi(msg)}
        )


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
