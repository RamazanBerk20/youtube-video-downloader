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
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


_JS_RUNTIME_NAMES = ("deno", "node", "bun")


def find_js_runtimes() -> dict[str, str]:
    """Return {runtime_name: full_path} for every yt-dlp-compatible JS
    runtime found on PATH. We pass these explicitly via `js_runtimes` so
    yt-dlp doesn't have to re-probe PATH (which on Windows can lag a
    fresh winget install)."""
    found: dict[str, str] = {}
    for name in _JS_RUNTIME_NAMES:
        path = shutil.which(name)
        if path:
            found[name] = path
    return found


def has_js_runtime() -> bool:
    """True if any JS runtime yt-dlp can use for YouTube's anti-bot challenge
    solver is on PATH. Without one, yt-dlp can only see image (thumbnail)
    formats on most YouTube videos. yt-dlp wiki: EJS.

    Deno is the recommended option (sandboxed); Node and Bun also work."""
    return bool(find_js_runtimes())


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

# Browsers that yt-dlp can pull a cookie jar from. "Off" sends no cookies.
# "Auto" also sends no cookies by default, but the GUI swaps it for a real
# browser the moment YouTube's bot-detection fires (see _resolve_cookies
# + DownloadManager.set_auto_cookies_browser). The yt-dlp option takes a
# lowercase browser name, so we lowercase at use time rather than
# maintaining two parallel lists.
_COOKIES_BROWSERS: tuple[str, ...] = (
    "Auto",
    "Off",
    "Firefox",
    "Chrome",
    "Brave",
    "Chromium",
    "Edge",
    "Opera",
    "Safari",
    "Vivaldi",
)


def cookies_browser_options() -> list[str]:
    return list(_COOKIES_BROWSERS)


# Substring (case-insensitive) yt-dlp surfaces when YouTube's bot-check
# rate-limit kicks in. The GUI checks task.error against this to decide
# whether to show the "enable browser cookies" banner. We deliberately
# drop the apostrophe from the match string because the surrounding error
# text can use either ASCII (') or Unicode curly (’) form depending on
# the terminal / locale, and a substring without it matches both.
BOT_DETECTION_SIGNATURE = "sign in to confirm you"

# yt-dlp's failure mode when it can't read a Chromium-based browser's
# cookies on Windows (App-Bound Encryption since Chrome 127). When this
# fires under Auto mode, the GUI moves on to the next candidate browser.
COOKIES_DECRYPT_FAILED_SIGNATURE = "failed to decrypt"

# yt-dlp error string when the JS challenge couldn't be solved and the
# only formats left are images (thumbnails). The GUI uses this together
# with `has_js_runtime()` to offer to install Deno.
NO_VIDEO_FORMAT_SIGNATURE = "requested format is not available"


# Audio codec → (ffmpeg preferredcodec, supports_bitrate).
# None codec = "Original" mode: no FFmpegExtractAudio postprocessor at all,
# so the source audio file is kept as-is (.webm/.m4a/etc.).
_AUDIO_CODECS: dict[str, tuple[str | None, bool]] = {
    "m4a (AAC)": ("m4a", True),
    "mp3":       ("mp3", True),
    "opus":      ("opus", True),
    "vorbis (ogg)": ("vorbis", True),
    "flac":      ("flac", False),
    "wav":       ("wav", False),
    "alac":      ("alac", False),
    "ac3":       ("ac3", True),
    "Original":  (None, False),
}

# Optional container conversion applied via FFmpegVideoConvertor after the
# download finishes. "None" leaves the natural container as yt-dlp produced
# it; everything else triggers a re-mux (or re-encode if codecs are
# incompatible) into the target format. Internal keys are short and stable
# (i18n table maps them to localised display strings); the value is the
# `preferedformat` string FFmpegVideoConvertor expects.
_VIDEO_CONTAINERS: dict[str, str | None] = {
    "None": None,
    "MP4":  "mp4",
    "MKV":  "mkv",
    "WebM": "webm",
    "MOV":  "mov",
    "AVI":  "avi",
    "FLV":  "flv",
    "MPEG": "mpeg",
    "WMV":  "wmv",
}


def video_container_options() -> list[str]:
    return list(_VIDEO_CONTAINERS.keys())


def _container_target(label: str) -> str | None:
    """Return the FFmpegVideoConvertor target string for a label, or None
    if the label means "don't convert"."""
    return _VIDEO_CONTAINERS.get(label, None)


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


class _ReencodeH264MP4PP(FFmpegPostProcessor):
    """Re-encode the downloaded video to H.264 video + AAC audio in an
    MP4 container. Used when the user enables "Re-encode for max
    compatibility" — slow (real encode, not stream-copy) but produces a
    file every player on Earth understands. yt-dlp's built-in
    FFmpegVideoConvertor only remuxes, so we can't reuse it here.

    Replaces the source file in-place: the .mp4 lands alongside the
    original and the original is removed."""

    # Codec defaults chosen for "broadly compatible, sensible quality":
    #   - libx264 with CRF 20 + medium preset = a good mid-range quality/
    #     speed/size trade-off. CRF lower than the typical 23 because the
    #     source is already compressed once (YouTube transcode); going
    #     too aggressive on CRF compounds the visible artifacts.
    #   - AAC at 192 kbps is high enough that nobody can tell it from the
    #     original opus/AAC source by ear.
    #   - +faststart moves the moov atom to the file head so the result
    #     starts playing immediately when streamed (handy for the user's
    #     downloaded copies too).
    _ENCODE_ARGS = (
        "-map", "0", "-dn", "-ignore_unknown",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
    )

    def run(self, info):
        in_path = Path(info["filepath"])
        out_path = in_path.with_suffix(".mp4")

        # If the source is already .mp4 we still need to re-encode (the
        # whole point), so write to a temp file and atomic-rename over
        # the original. Otherwise out_path differs from in_path and we
        # can write directly.
        if out_path == in_path:
            tmp_path = in_path.with_suffix(".reencoding.mp4")
            self.run_ffmpeg(str(in_path), str(tmp_path), list(self._ENCODE_ARGS))
            in_path.unlink(missing_ok=True)
            tmp_path.replace(out_path)
        else:
            self.run_ffmpeg(str(in_path), str(out_path), list(self._ENCODE_ARGS))
            in_path.unlink(missing_ok=True)

        info["filepath"] = str(out_path)
        info["ext"] = "mp4"
        return [], info


@dataclass
class DownloadTask:
    id: int
    url: str
    output_dir: Path
    mode: str
    quality: str
    codec: str
    playlist: bool
    # Optional output container key (e.g. "MP4", "MKV"). "None" or any
    # key not in _VIDEO_CONTAINERS means no conversion runs. Replaces the
    # older `force_mp4` boolean — the field is generic so we don't have
    # to grow another bool every time a new container gets added.
    container: str = "None"
    # When True, force a real re-encode to H.264/AAC/MP4 after the
    # download. Wins over `container` (output is always .mp4) because
    # this is the explicit "max compatibility" path — slow, lossy, but
    # plays everywhere including ancient Windows / Apple players that
    # can't decode VP9-in-mp4 or opus-in-mp4.
    reencode_h264: bool = False
    concurrent_fragments: int = 4
    cookies_browser: str = "Off"
    # Bumped by the GUI when it retries a bot-detection failure with auto-
    # detected cookies. Used to cap retries at 1 so a bad cookie jar (e.g.
    # browser not logged in) can't infinite-loop.
    retry_attempts: int = 0

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
        # Set by the GUI once it detects the user's browser (lazy, on first
        # bot-detection failure when cookies_browser="Auto"). Once set, any
        # task with cookies_browser="Auto" will start passing this browser
        # to yt-dlp. None = Auto behaves like Off.
        self._auto_cookies_browser: str | None = None

    # ---- Public API ---------------------------------------------------

    def add(
        self,
        url: str,
        output_dir: Path,
        mode: str,
        quality: str,
        codec: str,
        playlist: bool,
        container: str = "None",
        reencode_h264: bool = False,
        concurrent_fragments: int = 4,
        cookies_browser: str = "Off",
    ) -> DownloadTask:
        task = DownloadTask(
            id=next(self._id_seq),
            url=url,
            output_dir=output_dir,
            mode=mode,
            quality=quality,
            codec=codec,
            playlist=playlist,
            container=container or "None",
            reencode_h264=bool(reencode_h264) and mode == "video",
            concurrent_fragments=max(1, int(concurrent_fragments)),
            cookies_browser=cookies_browser or "Off",
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

    def set_auto_cookies_browser(self, name: str | None) -> None:
        """Tell the manager which browser to use when a task's cookies_browser
        is "Auto". Pass None to disable."""
        self._auto_cookies_browser = (name or None)

    def retry_task(self, task_id: int) -> bool:
        """Reset a failed/cancelled task back to queued so the scheduler
        picks it up again. Returns False if the task isn't retryable.

        Used by the GUI to silently retry a bot-detection failure once
        Auto has resolved to a real browser."""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None or task.status not in ("failed", "cancelled"):
                return False
            task.status = "queued"
            task.error = ""
            task.percent = 0.0
            task.speed = "—"
            task.eta = "—"
            task.retry_attempts += 1
            task._cancel.clear()
        # Push a progress event so the row re-renders to the queued state.
        self.events.put({"type": "task_progress", "task_id": task.id})
        return True

    def _resolve_cookies(self, task: DownloadTask) -> str | None:
        """Translate task.cookies_browser to the actual yt-dlp browser name
        (lowercase) or None for "don't send cookies". "Auto" resolves to
        the manager's session-wide auto browser, which is None until the
        GUI activates it via set_auto_cookies_browser()."""
        val = (task.cookies_browser or "Off").strip()
        if val == "Auto":
            return self._auto_cookies_browser  # None until activated
        if val.lower() == "off":
            return None
        return val.lower()

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
                # The "Re-encode for max compatibility" custom PP can't be
                # specified in opts['postprocessors'] (yt-dlp resolves keys
                # to its built-in classes only). Register it explicitly so
                # it runs after the FFmpegFD merger, last in the chain.
                if task.reencode_h264 and task.mode == "video":
                    ydl.add_post_processor(
                        _ReencodeH264MP4PP(downloader=ydl), when="post_process"
                    )
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
            # YouTube now ships an anti-bot JavaScript challenge that yt-dlp
            # can only solve when (a) a JS runtime is available AND (b) the
            # remote EJS challenge-solver script is allowed. Without (b)
            # downloads silently degrade to image-only formats. We always
            # opt into ejs:github — it's the recommended option and the
            # download is cached by yt-dlp after first use.
            "remote_components": ["ejs:github"],
        }

        # Pass JS runtime paths explicitly. yt-dlp's auto-probe walks PATH
        # at YDL construction time; passing what we already found avoids a
        # second probe and makes Windows winget-fresh installs work as soon
        # as the GUI process inherits the updated PATH (start.bat refresh).
        # The value MUST be a dict per yt-dlp's contract: it later does
        # `config.get('path')` on it, so {} works but None crashes.
        runtimes = find_js_runtimes()
        if runtimes:
            opts["js_runtimes"] = {
                name: {"path": path} for name, path in runtimes.items()
            }

        # Browser cookies bypass YouTube's "Sign in to confirm you're not a
        # bot" rate limit. Resolver translates Off/Auto/<name> to either
        # None (don't send) or a lowercase yt-dlp browser key.
        browser = self._resolve_cookies(task)
        if browser:
            opts["cookiesfrombrowser"] = (browser,)

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
            # When the user picks a target container, an FFmpegVideoConvertor
            # pass remuxes (or re-encodes when codecs aren't compatible).
            # Convertor is a no-op when the merged file is already that
            # container, so it's safe to enable even for matching formats.
            #
            # Re-encode mode is mutually exclusive with the container PP:
            # the custom _ReencodeH264MP4PP forces .mp4 output via a real
            # encode pass, so a preceding remux to e.g. .mkv would just
            # be wasted work. _run() attaches the re-encode PP separately.
            if not task.reencode_h264:
                target = _container_target(task.container)
                if target:
                    opts["postprocessors"] = [
                        {"key": "FFmpegVideoConvertor", "preferedformat": target}
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
