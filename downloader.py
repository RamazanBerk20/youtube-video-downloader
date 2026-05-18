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


# ---- Quality / codec option tables ----------------------------------------
#
# Quality is the user-visible resolution / bitrate label. Codec is the
# container + codec preference. The two combine into a yt-dlp `--format`
# selector at download time via `_build_video_format` /
# `_build_audio_postprocessor`.
#
# We intentionally do NOT pass `merge_output_format=mp4` — forcing mp4
# around opus audio yields files that some players (Windows Media Player,
# Movies & TV) play silently. Instead we *prefer* compatible codecs and
# let yt-dlp pick the natural container (.mp4 for H.264/AAC, .webm/.mkv
# for VP9/AV1/opus).

# Maps the quality label to a height ceiling. None = no cap (Best),
# "worst" = the special worst-quality selector.
_QUALITY_HEIGHT: dict[str, int | None | str] = {
    "Best": None,
    "4K (2160p)": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "Worst": "worst",
}

# Bitrate (kbps) lookup for lossy audio codecs. "0" tells yt-dlp to use the
# codec's default best bitrate.
_AUDIO_BITRATES: dict[str, str] = {
    "Best": "0",
    "320 kbps": "320",
    "256 kbps": "256",
    "192 kbps": "192",
    "128 kbps": "128",
    "96 kbps": "96",
}

# Video codec → chain of `/`-joined format selectors, each containing `{h}`
# which is replaced with the height filter (or "" for Best). The chain is
# tried left-to-right until one resolves, so each entry can be increasingly
# loose (codec-specific → container-specific → generic best).
_VIDEO_CODEC_CHAINS: dict[str, list[str]] = {
    "Auto": [
        "bestvideo{h}[ext=mp4]+bestaudio[ext=m4a]",
        "bestvideo{h}+bestaudio",
        "best{h}",
        "best",
    ],
    "MP4 (H.264)": [
        "bestvideo{h}[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]",
        "bestvideo{h}[ext=mp4]+bestaudio[ext=m4a]",
        "best{h}[ext=mp4]",
        "best{h}",
        "best",
    ],
    "WebM (VP9)": [
        "bestvideo{h}[vcodec^=vp09]+bestaudio[acodec=opus]",
        "bestvideo{h}[ext=webm]+bestaudio[ext=webm]",
        "bestvideo{h}+bestaudio",
        "best{h}",
        "best",
    ],
    "WebM (AV1)": [
        "bestvideo{h}[vcodec^=av01]+bestaudio[acodec=opus]",
        "bestvideo{h}+bestaudio",
        "best{h}",
        "best",
    ],
}

# Audio codec → (ffmpeg preferredcodec, supports_bitrate).
# None codec = "Original" mode: no FFmpegExtractAudio postprocessor at all,
# so the source audio file is kept as-is (.webm/.m4a/etc.).
_AUDIO_CODECS: dict[str, tuple[str | None, bool]] = {
    "m4a (AAC)": ("m4a", True),
    "mp3":       ("mp3", True),
    "opus":      ("opus", True),
    "flac":      ("flac", False),
    "wav":       ("wav", False),
    "Original":  (None, False),
}


def video_quality_options() -> list[str]:
    return list(_QUALITY_HEIGHT.keys())


def audio_quality_options() -> list[str]:
    return list(_AUDIO_BITRATES.keys())


def video_codec_options() -> list[str]:
    return list(_VIDEO_CODEC_CHAINS.keys())


def audio_codec_options() -> list[str]:
    return list(_AUDIO_CODECS.keys())


def audio_codec_uses_bitrate(codec_key: str) -> bool:
    """True iff the audio codec has a meaningful bitrate setting."""
    return _AUDIO_CODECS.get(codec_key, (None, False))[1]


def _build_video_format(quality: str, codec: str) -> str:
    spec = _QUALITY_HEIGHT.get(quality)
    if spec == "worst":
        return "worstvideo+worstaudio/worst"
    h = f"[height<={spec}]" if isinstance(spec, int) else ""
    chain = _VIDEO_CODEC_CHAINS.get(codec) or _VIDEO_CODEC_CHAINS["Auto"]
    return "/".join(s.replace("{h}", h) for s in chain)


def _build_audio_postprocessor(codec_key: str, bitrate_key: str) -> dict | None:
    spec = _AUDIO_CODECS.get(codec_key) or _AUDIO_CODECS["m4a (AAC)"]
    codec, supports_br = spec
    if codec is None:
        return None  # Original: keep source as-is, no postprocessing
    return {
        "key": "FFmpegExtractAudio",
        "preferredcodec": codec,
        "preferredquality": _AUDIO_BITRATES.get(bitrate_key, "0") if supports_br else "0",
    }


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
    codec: str
    playlist: bool
    force_mp4: bool = False
    concurrent_fragments: int = 4

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
        codec: str,
        playlist: bool,
        force_mp4: bool = False,
        concurrent_fragments: int = 4,
    ) -> DownloadTask:
        task = DownloadTask(
            id=next(self._id_seq),
            url=url,
            output_dir=output_dir,
            mode=mode,
            quality=quality,
            codec=codec,
            playlist=playlist,
            force_mp4=force_mp4,
            concurrent_fragments=max(1, int(concurrent_fragments)),
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

    def clear_done(self) -> list[int]:
        """Remove finished tasks from the manager and return their ids.

        Returning the exact list keeps the GUI from having to infer which
        rows to destroy by diffing the dict after the call — that approach
        races with download threads transitioning state mid-click."""
        finished = {"done", "failed", "cancelled"}
        with self._lock:
            removed = [tid for tid, t in self.tasks.items() if t.status in finished]
            for tid in removed:
                del self.tasks[tid]
        return removed

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
            "concurrent_fragment_downloads": max(1, int(task.concurrent_fragments)),
            "retries": 5,
            "fragment_retries": 5,
        }

        if task.mode == "audio":
            opts["format"] = "bestaudio/best"
            pp = _build_audio_postprocessor(task.codec, task.quality)
            if pp is not None:
                opts["postprocessors"] = [pp]
            # else: "Original" → keep the raw audio stream as-is.
        else:
            opts["format"] = _build_video_format(task.quality, task.codec)
            # We deliberately do NOT pass merge_output_format=mp4: it would
            # mux opus audio into mp4 and play silently in some players.
            # If the user wants a guaranteed-mp4 output, force_mp4 triggers
            # an FFmpegVideoConvertor pass that re-encodes to H.264 + AAC.
            # Convertor is a no-op when the merged file is already .mp4.
            if task.force_mp4:
                opts["postprocessors"] = [
                    {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
                ]
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
