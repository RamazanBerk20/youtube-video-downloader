"""Concurrent yt-dlp download manager.

Each `DownloadTask` runs on its own daemon thread. The `DownloadManager`
keeps a dict of tasks and is scheduled cooperatively from the GUI's poll
loop: every tick, `schedule(max_concurrent=N)` starts the next queued
task(s) if fewer than N are running. Per-task progress, errors, and
lifecycle events are published through a single thread-safe queue.
"""
from __future__ import annotations

import itertools
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
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

# Optional container conversion after the download finishes. "None" leaves
# the natural container as yt-dlp produced it. Modern containers use
# yt-dlp's FFmpegVideoConvertor remux path unless the user requests a real
# compatibility re-encode. Legacy containers always get a real transcode
# because stream-copying YouTube's H.264/AAC, VP9/Opus, or AV1/Opus into
# MPEG/AVI/FLV/WMV is either invalid or unreliable.
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


# Ordered list — UI dropdown shows them in this exact sequence.
_REENCODE_QUALITY_OPTIONS: tuple[str, ...] = ("Fast", "Balanced", "Quality")


def reencode_quality_options() -> list[str]:
    return list(_REENCODE_QUALITY_OPTIONS)


def _container_target(label: str) -> str | None:
    """Return the target extension for a label, or None if the label means
    "don't convert"."""
    return _VIDEO_CONTAINERS.get(label, None)


_LEGACY_TRANSCODE_CONTAINERS: frozenset[str] = frozenset({
    "avi", "flv", "mpeg", "wmv",
})


# Codecs that are already in the right shape for each target container.
# Used by the smart-skip path: if the downloaded file already has these
# codecs in the matching extension, the compatibility re-encode is a no-op.
# Audio set can include None semantics — see _codecs_match below.
_TARGET_CODECS: dict[str, tuple[set[str], set[str]]] = {
    "mp4":  ({"h264"},                     {"aac"}),
    "mov":  ({"h264"},                     {"aac"}),
    "mkv":  ({"h264"},                     {"aac"}),
    "webm": ({"vp9"},                      {"opus"}),
    "avi":  ({"mpeg4", "msmpeg4v3"},       {"mp3"}),
    "flv":  ({"flv1", "h264"},             {"mp3", "aac"}),
    "mpeg": ({"mpeg2video"},               {"mp2"}),
    "wmv":  ({"wmv1", "wmv2", "wmv3"},     {"wmav2", "wmav1"}),
}


# Cached result of `ffmpeg -encoders` parsed for H.264 variants. Probed
# once on first use and reused — encoders list doesn't change at runtime.
_H264_ENCODER_CACHE: list[str] | None = None

# Encoders that hit a runtime failure (or watchdog timeout) during a real
# encode get added here for the rest of the session, so subsequent encodes
# skip them instead of paying the failed-init cost again. Populated by
# `_mark_encoder_broken` from _ContainerTranscodePP.run on caught failures.
_KNOWN_BROKEN_ENCODERS: set[str] = set()
_KNOWN_BROKEN_LOCK = threading.Lock()


def _mark_encoder_broken(encoder: str) -> None:
    with _KNOWN_BROKEN_LOCK:
        _KNOWN_BROKEN_ENCODERS.add(encoder)


def _is_encoder_broken(encoder: str) -> bool:
    with _KNOWN_BROKEN_LOCK:
        return encoder in _KNOWN_BROKEN_ENCODERS


def _ffmpeg_env() -> dict[str, str] | None:
    """Build the env dict to hand to ffmpeg when we want the NVIDIA dGPU
    to be active. We DON'T export these vars in the parent Python process
    — setting them globally causes Tk under GLVnd to load NVIDIA's GLX
    driver, and that driver's state can be corrupted when subprocess.Popen
    forks from a daemon thread.

    Returns None when no override is needed (non-Linux, no NVIDIA driver,
    or the parent env already has the vars from a `prime-run` launch),
    telling callers to use the default subprocess env."""
    if sys.platform != "linux":
        return None
    if not os.path.exists("/proc/driver/nvidia/version"):
        return None
    if os.environ.get("__NV_PRIME_RENDER_OFFLOAD"):
        return None
    env = os.environ.copy()
    env["__NV_PRIME_RENDER_OFFLOAD"] = "1"
    env["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
    env["__VK_LAYER_NV_optimus"] = "NVIDIA_only"
    return env


def _detect_h264_encoders() -> list[str]:
    """Return available H.264 encoders for this machine, ordered best-first.
    Hardware encoders dramatically beat libx264 on 4K (often 100×+), so we
    try them in OS-appropriate priority and fall back to libx264.

    VAAPI is excluded on Linux because it needs upfront -vaapi_device +
    -filter_hw_device setup that yt-dlp's run_ffmpeg pipeline doesn't
    accommodate. NVENC/QSV/AMF are pure output-side options and work
    out of the box on machines that have the GPU + driver."""
    global _H264_ENCODER_CACHE
    if _H264_ENCODER_CACHE is not None:
        return list(_H264_ENCODER_CACHE)

    available: set[str] = set()
    try:
        r = subprocess.run(
            ["ffmpeg", "-encoders", "-hide_banner", "-loglevel", "error"],
            capture_output=True, text=True, timeout=8,
        )
        text = (r.stdout or "") + (r.stderr or "")
        for name in (
            "h264_nvenc", "h264_qsv", "h264_amf",
            "h264_videotoolbox", "libx264",
        ):
            # Match against word-aligned occurrences: `ffmpeg -encoders`
            # prints each name flush-left after the V..... flag column,
            # so substring matching is reliable here.
            if name in text:
                available.add(name)
    except (OSError, subprocess.SubprocessError):
        pass

    if not available:
        # Probe failed (ffmpeg missing?) — assume libx264 and let yt-dlp's
        # own error path surface a clearer message later.
        available = {"libx264"}

    if sys.platform == "win32":
        priority = ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")
    elif sys.platform == "darwin":
        priority = ("h264_videotoolbox", "libx264")
    else:
        # On Linux we'd love to add VAAPI for Intel/AMD users but it needs
        # extra hw-device wiring. NVENC users get the speedup; everyone
        # else falls through to libx264 with a faster preset.
        priority = ("h264_nvenc", "libx264")

    # Filter against the session-scoped broken-encoder blacklist. An
    # encoder lands in the blacklist after a real encode attempt fails
    # or watchdog-times-out, so subsequent encodes skip it directly
    # instead of repeating the failed-init handshake. libx264 stays
    # available always — it's the guaranteed fallback.
    final = [
        e for e in priority
        if e in available and not _is_encoder_broken(e)
    ]
    if not final:
        final = ["libx264"]
    # NOT cached in _H264_ENCODER_CACHE because the blacklist is dynamic;
    # we want each PP run to see the up-to-date set. The static
    # `ffmpeg -encoders` parse is cheap (~50 ms) and ffmpeg's output
    # doesn't change at runtime, so re-running it isn't wasteful.
    return final


# Per-encoder speed/quality preset table. Each row is keyed by encoder
# name + quality preset ("fast" / "balanced" / "quality") and stores the
# ffmpeg argv tokens that select the matching tradeoff for that codec.
#
# Hardware encoders (NVENC/QSV/AMF/VideoToolbox) barely care about speed
# vs quality — even the slowest preset runs at hundreds of fps on a
# modern GPU. libx264 cares a lot: ultrafast is ~10× faster than slow
# at the cost of ~30 % file size growth at the same visual quality.
_H264_ENCODER_PRESETS: dict[str, dict[str, tuple[str, ...]]] = {
    "h264_nvenc": {
        # Legacy preset names (fast/medium/slow) are accepted by every
        # NVENC generation from Kepler (gen 3) onward; the newer p1-p7
        # naming only works on SDK 10+ drivers and silently rejects on
        # older Pascal builds (GTX 10-series + old driver = exit 254 at
        # session init, which we saw in the wild). cq is constant-quality
        # — lower = better, 18 near-lossless, 28 web-quality.
        "fast":     ("-c:v", "h264_nvenc", "-preset", "fast",   "-rc", "vbr", "-cq", "24"),
        "balanced": ("-c:v", "h264_nvenc", "-preset", "medium", "-rc", "vbr", "-cq", "22"),
        "quality":  ("-c:v", "h264_nvenc", "-preset", "slow",   "-rc", "vbr", "-cq", "20"),
    },
    "h264_qsv": {
        "fast":     ("-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "24"),
        "balanced": ("-c:v", "h264_qsv", "-preset", "medium",   "-global_quality", "22"),
        "quality":  ("-c:v", "h264_qsv", "-preset", "slow",     "-global_quality", "20"),
    },
    "h264_amf": {
        "fast":     ("-c:v", "h264_amf", "-quality", "speed",
                     "-rc", "cqp", "-qp_i", "24", "-qp_p", "24"),
        "balanced": ("-c:v", "h264_amf", "-quality", "balanced",
                     "-rc", "cqp", "-qp_i", "22", "-qp_p", "22"),
        "quality":  ("-c:v", "h264_amf", "-quality", "quality",
                     "-rc", "cqp", "-qp_i", "20", "-qp_p", "20"),
    },
    "h264_videotoolbox": {
        # VideoToolbox -q:v runs 0-100, higher = better.
        "fast":     ("-c:v", "h264_videotoolbox", "-q:v", "75"),
        "balanced": ("-c:v", "h264_videotoolbox", "-q:v", "60"),
        "quality":  ("-c:v", "h264_videotoolbox", "-q:v", "45"),
    },
    "libx264": {
        # libx264 CRF: lower = better. 18 ≈ visually lossless, 28 ≈ web.
        # presets are the speed/quality dial; medium is libx264's default.
        "fast":     ("-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"),
        "balanced": ("-c:v", "libx264", "-preset", "medium",    "-crf", "21"),
        "quality":  ("-c:v", "libx264", "-preset", "slow",      "-crf", "19"),
    },
}


def _h264_encoder_args(encoder: str, quality_preset: str = "balanced") -> list[str]:
    """Output-side ffmpeg args for the chosen H.264 encoder at the chosen
    speed/quality preset. Falls back to libx264 + balanced when the lookup
    misses (defensive — settings normalisation should prevent it)."""
    by_encoder = _H264_ENCODER_PRESETS.get(encoder, _H264_ENCODER_PRESETS["libx264"])
    args = by_encoder.get(quality_preset, by_encoder["balanced"])
    return list(args)


def _build_remux_args(target_ext: str) -> list[str]:
    """ffmpeg args for a pure container swap with no re-encoding. Used
    when the source codecs already match the target profile — only the
    extension changes."""
    args = ["-map", "0", "-c", "copy"]
    if target_ext in ("mp4", "mov", "m4a"):
        args.extend(["-movflags", "+faststart"])
    if target_ext == "mpeg":
        args.extend(["-f", "mpeg"])
    return args


# Legacy codec quality scales (-q:v 1=best, 31=worst). Fast / balanced /
# quality picks roughly the same visual buckets as the H.264 preset table.
_LEGACY_QV: dict[str, dict[str, str]] = {
    "libxvid":     {"fast": "6", "balanced": "4", "quality": "2"},
    "flv":         {"fast": "7", "balanced": "5", "quality": "3"},
    "mpeg2video":  {"fast": "5", "balanced": "3", "quality": "2"},
    "wmv2":        {"fast": "6", "balanced": "4", "quality": "2"},
}

# libvpx-vp9 tuning: -crf lower = better; -cpu-used higher = faster encode.
_VP9_PRESETS: dict[str, tuple[str, str]] = {
    "fast":     ("35", "6"),
    "balanced": ("32", "4"),
    "quality":  ("28", "1"),
}


def _build_transcode_args(
    target_ext: str,
    h264_encoder: str = "libx264",
    quality_preset: str = "balanced",
) -> list[str]:
    """ffmpeg args for a full re-encode into `target_ext`. `h264_encoder`
    is only consulted for H.264-based containers (mp4/mov/mkv); legacy
    containers always use their dedicated software encoder. `quality_preset`
    is normalised lowercase: fast / balanced / quality."""
    qp = (quality_preset or "balanced").lower()
    base = ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-ignore_unknown"]
    if target_ext in ("mp4", "mov", "mkv"):
        args = list(base) + _h264_encoder_args(h264_encoder, qp)
        args.extend(["-pix_fmt", "yuv420p"])
        args.extend(["-c:a", "aac", "-b:a", "192k"])
        if target_ext in ("mp4", "mov"):
            args.extend(["-movflags", "+faststart"])
        return args
    if target_ext == "webm":
        crf, cpu = _VP9_PRESETS.get(qp, _VP9_PRESETS["balanced"])
        return list(base) + [
            "-c:v", "libvpx-vp9", "-crf", crf, "-b:v", "0", "-cpu-used", cpu,
            "-c:a", "libopus", "-b:a", "160k",
        ]
    if target_ext == "avi":
        qv = _LEGACY_QV["libxvid"].get(qp, "4")
        return list(base) + [
            "-c:v", "libxvid", "-q:v", qv,
            "-c:a", "libmp3lame", "-b:a", "192k",
        ]
    if target_ext == "flv":
        qv = _LEGACY_QV["flv"].get(qp, "5")
        return list(base) + [
            "-c:v", "flv", "-q:v", qv,
            "-c:a", "libmp3lame", "-b:a", "192k",
        ]
    if target_ext == "mpeg":
        qv = _LEGACY_QV["mpeg2video"].get(qp, "3")
        return list(base) + [
            "-c:v", "mpeg2video", "-q:v", qv,
            "-c:a", "mp2", "-b:a", "192k",
            "-f", "mpeg",
        ]
    if target_ext == "wmv":
        qv = _LEGACY_QV["wmv2"].get(qp, "4")
        return list(base) + [
            "-c:v", "wmv2", "-q:v", qv,
            "-c:a", "wmav2", "-b:a", "192k",
        ]
    # Unknown target — fall back to mp4 profile.
    return _build_transcode_args("mp4", h264_encoder, qp)


def _probe_codecs(path: Path) -> tuple[str | None, str | None]:
    """Return (video_codec_name, audio_codec_name) via ffprobe. Either may
    be None if the stream is absent or the probe failed."""
    if not shutil.which("ffprobe"):
        return None, None
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_streams",
                "-print_format", "json", str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None, None
        data = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None, None
    vcodec: str | None = None
    acodec: str | None = None
    for stream in data.get("streams") or []:
        ctype = stream.get("codec_type")
        name = stream.get("codec_name")
        if ctype == "video" and vcodec is None:
            vcodec = name
        elif ctype == "audio" and acodec is None:
            acodec = name
    return vcodec, acodec


def _codecs_match(target_ext: str, vcodec: str | None, acodec: str | None) -> bool:
    """True iff the source codecs already fit `target_ext`'s profile.
    Audio is treated as optional — a video-only source still qualifies
    if its video codec matches."""
    profile = _TARGET_CODECS.get(target_ext)
    if profile is None:
        return False
    ok_v, ok_a = profile
    if vcodec not in ok_v:
        return False
    if acodec is None:
        return True
    return acodec in ok_a


def _probe_duration(path: Path) -> float | None:
    """Return the source's duration in seconds via ffprobe, or None if the
    probe failed or the value isn't parseable. Used to translate ffmpeg's
    `out_time_us` progress field into a 0-100 % bar."""
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        value = (r.stdout or "").strip()
        if not value or value == "N/A":
            return None
        return float(value)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _run_ffmpeg_with_progress(
    in_path: Path,
    out_path: Path,
    args: list[str],
    duration_seconds: float | None,
    task: "DownloadTask",
    events: "queue.Queue[dict[str, Any]]",
) -> None:
    """Drive ffmpeg directly (bypassing yt-dlp's silent run_ffmpeg wrapper)
    so we can attach `-progress pipe:1` and stream encode progress back to
    the GUI as it happens. Updates `task.percent`, `task.speed`, `task.eta`
    in place and emits `task_progress` events on every measurable change.

    Raises `_CancelledError` if `task._cancel` fires mid-encode,
    `subprocess.CalledProcessError` on non-zero ffmpeg exit."""
    cmd: list[str] = [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-y",
        "-i", str(in_path),
        *args,
        "-progress", "pipe:1",
        str(out_path),
    ]

    # stderr=STDOUT merges error output into the same pipe as progress
    # data. That avoids needing a second drain thread (whose fork+read
    # was a likely source of X11 protocol corruption under prime-run
    # on hybrid-GPU laptops). Progress lines are key=value; anything
    # else is treated as an error message and stashed for the failure
    # path. start_new_session=True puts ffmpeg in its own session so
    # terminal signals to us don't propagate to it (and vice versa).
    popen_kwargs: dict[str, Any] = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        env=_ffmpeg_env(),
    )
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)

    # Push stdout lines through a queue so we can apply a watchdog timeout
    # AND a cancellation poll without ever blocking forever on readline().
    # A daemon reader thread does the actual pipe iteration; the main loop
    # here just consumes from the queue. select.select() would be lighter
    # but it doesn't work on subprocess pipes under Windows — queue-based
    # is the cross-platform path.
    line_q: queue.Queue = queue.Queue()

    def _read_stdout() -> None:
        if proc.stdout is None:
            line_q.put(None)
            return
        try:
            for line in proc.stdout:
                line_q.put(line)
        finally:
            line_q.put(None)

    reader_thread = threading.Thread(target=_read_stdout, daemon=True)
    reader_thread.start()

    error_lines: list[str] = []
    cancelled = False
    hung = False
    # Watchdog: ffmpeg with `-progress pipe:1` writes a key=value block
    # roughly once a second. If we go a full minute without ANY output,
    # the encoder is stuck — typically a HW encoder whose driver wedged
    # at init. Kill it so the PP can blacklist it and fall back to libx264.
    HUNG_TIMEOUT = 60.0
    last_data = time.monotonic()
    last_pct_emitted = -1.0
    try:
        while True:
            try:
                raw = line_q.get(timeout=1.0)
            except queue.Empty:
                if task._cancel.is_set():
                    cancelled = True
                    break
                if time.monotonic() - last_data > HUNG_TIMEOUT:
                    hung = True
                    break
                continue
            if raw is None:
                break  # EOF
            last_data = time.monotonic()
            if task._cancel.is_set():
                cancelled = True
                break
            line = raw.rstrip("\r\n")
            if "=" not in line:
                # Non-progress output — keep the last 20 lines for
                # error reporting if the encode fails. Cap so a
                # chatty encoder can't eat memory.
                if line:
                    error_lines.append(line)
                    if len(error_lines) > 20:
                        error_lines.pop(0)
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key == "out_time_us" and duration_seconds:
                try:
                    out_seconds = int(val) / 1_000_000.0
                except ValueError:
                    continue
                # Cap at 99.5 — the explicit "progress=end" line below
                # is the real 100% signal. Avoids the bar briefly
                # touching 100 before the encoder fully flushes.
                pct = max(0.0, min(99.5, out_seconds / duration_seconds * 100.0))
                task.percent = pct
                # Only emit when % moves ≥0.5 to avoid flooding the
                # GUI queue at ffmpeg's tick rate (~10/s).
                if pct - last_pct_emitted >= 0.5:
                    events.put({"type": "task_progress", "task_id": task.id})
                    last_pct_emitted = pct
            elif key == "speed":
                if val == "N/A" or not val.endswith("x"):
                    continue
                task.speed = val
                try:
                    multiplier = float(val[:-1])
                except ValueError:
                    multiplier = 0.0
                if duration_seconds and multiplier > 0:
                    remaining_real = max(
                        0.0,
                        duration_seconds * (100.0 - task.percent) / 100.0
                        / multiplier,
                    )
                    task.eta = _fmt_eta(int(remaining_real))
            elif key == "progress" and val == "end":
                task.percent = 100.0
                events.put({"type": "task_progress", "task_id": task.id})
                last_pct_emitted = 100.0
        if cancelled:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            raise _CancelledError
        if hung:
            # Watchdog tripped — kill the child and report it as a
            # timeout so the PP blacklists this encoder and retries
            # with libx264. SIGTERM first to let ffmpeg flush partial
            # output, SIGKILL after a brief grace period.
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            raise TimeoutError(
                f"ffmpeg produced no progress for {HUNG_TIMEOUT:.0f}s "
                f"— encoder appears stuck"
            )
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    # If the cancel flag fired AFTER our pipe-read loop exited (e.g. an
    # external SIGTERM from process_cleanup.terminate_children killed the
    # child during shutdown), the for-loop just sees EOF and the non-zero
    # returncode looks like an encoder failure. Detect that case and
    # raise _CancelledError so the task is marked cancelled, not failed
    # — and we don't waste time falling back to the next encoder.
    if task._cancel.is_set():
        raise _CancelledError

    if proc.returncode not in (0, None):
        err = "\n".join(error_lines).strip() or f"ffmpeg exited with {proc.returncode}"
        raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=err)


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


def _positive_int(value: object, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


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


class _ContainerTranscodePP(FFmpegPostProcessor):
    """Bring the downloaded video into the target container/codecs.

    Three modes, picked in order of cheapness:
      1. The source already matches the target extension AND codecs → no-op.
         Common case for "Container = None + re-encode + 1080p H.264 MP4"
         where the natural download is already H.264/AAC in .mp4.
      2. Codecs match but the container is wrong → stream-copy remux. Fast
         (seconds), no quality loss.
      3. Codecs don't match → real re-encode. Hardware H.264 encoder (NVENC
         / QSV / AMF / VideoToolbox) is preferred over libx264 by an order
         of magnitude on 4K. libx264 with preset=fast is the fallback.

    Replaces the source file in-place: the re-encoded copy lands with the
    chosen extension and the original is removed."""

    def __init__(
        self,
        downloader=None,
        target_ext: str = "mp4",
        quality_preset: str = "Balanced",
        task: "DownloadTask | None" = None,
        events: "queue.Queue[dict[str, Any]] | None" = None,
    ):
        super().__init__(downloader)
        # Lowercase, with a "" / None fallback to "mp4": "Container = None"
        # plus compatibility re-encode still needs a concrete file type.
        self.target_ext = (target_ext or "mp4").lower()
        # Lowercased preset key for the per-encoder table lookup.
        self.quality_preset = (quality_preset or "Balanced").lower()
        # Optional task + events plumbing so the encode pass can stream
        # progress back to the GUI. None when used in tests / contexts
        # without a DownloadManager.
        self._task = task
        self._events = events

    def run(self, info):
        in_path = Path(info["filepath"])
        target_ext = self.target_ext
        src_ext = in_path.suffix.lstrip(".").lower()

        # ffprobe takes a fraction of a second normally, but on hybrid-GPU
        # laptops where the dGPU just spun up via prime-run there can be
        # a multi-second pause before its first response. Tell the user
        # something's happening before the actual encoder runs.
        self.to_screen(f"Inspecting {in_path.name}…")
        vcodec, acodec = _probe_codecs(in_path)
        codecs_match = _codecs_match(target_ext, vcodec, acodec)

        # Case 1: already perfect — skip entirely.
        if src_ext == target_ext and codecs_match:
            self.to_screen(
                f"Re-encode skipped: {in_path.name} is already "
                f"{target_ext}/{vcodec}{('/' + acodec) if acodec else ''}"
            )
            return [], info

        out_path = in_path.with_suffix(f".{target_ext}")

        # Case 2: codecs match, container doesn't — fast remux.
        if codecs_match:
            self.to_screen(
                f"Remuxing to {target_ext} (codecs already compatible)"
            )
            self._set_phase("remux")
            self._encode(in_path, out_path, target_ext,
                         _build_remux_args(target_ext),
                         duration_seconds=None)
            info["filepath"] = str(out_path)
            info["ext"] = target_ext
            return [], info

        # Case 3: full transcode. For H.264 targets try HW first, fall
        # back to libx264 on runtime failure (driver mismatch, etc.).
        if target_ext in ("mp4", "mov", "mkv"):
            encoders = _detect_h264_encoders() or ["libx264"]
        else:
            encoders = ["libx264"]  # value unused for non-H.264 profiles

        # Flip the phase BEFORE the encoder loop, not inside it: the bar
        # should reset to 0 % the moment the encode path is entered, even
        # though ffmpeg's first progress line can be 5-15 s away on slow
        # ffprobe / NVENC handshake. Otherwise the user sees download's
        # 100 % frozen and assumes the app hung.
        self._set_phase("encode")

        # Probe duration once so every encoder retry can map ffmpeg's
        # out_time_us back to a 0-100 % progress without re-running ffprobe.
        duration = _probe_duration(in_path)

        last_err: Exception | None = None
        for encoder in encoders:
            args = _build_transcode_args(target_ext, encoder, self.quality_preset)
            self.to_screen(
                f"Re-encoding to {target_ext} with {encoder} "
                f"({self.quality_preset})"
            )
            try:
                self._encode(in_path, out_path, target_ext, args,
                             duration_seconds=duration)
                info["filepath"] = str(out_path)
                info["ext"] = target_ext
                return [], info
            except _CancelledError:
                # User cancelled mid-encode — don't fall through to the
                # next encoder, just propagate so the manager marks the
                # task as cancelled.
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                # The CalledProcessError's __str__ only shows the command
                # + exit code — useless for diagnosing why NVENC refused.
                # Pull the captured stderr (set on the exception by
                # _run_ffmpeg_with_progress) so the actual ffmpeg
                # complaint reaches the user log.
                stderr = (getattr(e, "stderr", "") or "").strip()
                if stderr:
                    detail = stderr.splitlines()[-1][:200]
                else:
                    detail = str(e)[:200]
                self.report_warning(
                    f"{encoder} failed: {detail}; trying next encoder"
                )
                # Add to the session blacklist so future encodes skip
                # this encoder directly — no point retrying a NVENC
                # init that just failed because the dGPU is parked.
                # libx264 is the universal fallback and never gets
                # blacklisted (a libx264 failure is a real problem
                # we'd want surfaced, not silenced).
                if encoder != "libx264":
                    _mark_encoder_broken(encoder)
                # Clean up whatever the failed pass left behind so the
                # retry starts from a clean slate.
                if out_path != in_path and out_path.exists():
                    try:
                        out_path.unlink()
                    except OSError:
                        pass
                tmp = in_path.with_suffix(f".reencoding.{target_ext}")
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        if last_err is not None:
            raise last_err
        return [], info

    def _set_phase(self, phase: str) -> None:
        """Flip task.phase + reset percent/speed/eta so the GUI redraws
        the bar from zero with phase-appropriate labels. Safe to call
        without a wired task (no-op in tests)."""
        if self._task is None:
            return
        self._task.phase = phase
        self._task.percent = 0.0
        self._task.speed = "—"
        self._task.eta = "—"
        if self._events is not None:
            self._events.put({"type": "task_progress", "task_id": self._task.id})

    def _encode(
        self,
        in_path: Path,
        out_path: Path,
        target_ext: str,
        args: list[str],
        *,
        duration_seconds: float | None,
    ) -> None:
        """Run ffmpeg in either same-ext (write-to-temp + replace) or
        different-ext (write-direct + remove source) mode, streaming
        progress to the GUI when wired. Raises on ffmpeg failure (the
        caller handles retry / cleanup) or `_CancelledError` if the
        user pressed ×."""
        if self._task is None or self._events is None:
            # Test / no-GUI path: use yt-dlp's silent wrapper as before.
            if out_path == in_path:
                tmp_path = in_path.with_suffix(f".reencoding.{target_ext}")
                self.run_ffmpeg(str(in_path), str(tmp_path), args)
                in_path.unlink(missing_ok=True)
                tmp_path.replace(out_path)
            else:
                self.run_ffmpeg(str(in_path), str(out_path), args)
                in_path.unlink(missing_ok=True)
            return

        # GUI path: drive ffmpeg ourselves so we can stream -progress.
        if out_path == in_path:
            tmp_path = in_path.with_suffix(f".reencoding.{target_ext}")
            _run_ffmpeg_with_progress(
                in_path, tmp_path, args, duration_seconds,
                self._task, self._events,
            )
            in_path.unlink(missing_ok=True)
            tmp_path.replace(out_path)
        else:
            _run_ffmpeg_with_progress(
                in_path, out_path, args, duration_seconds,
                self._task, self._events,
            )
            in_path.unlink(missing_ok=True)


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
    # When True, force a real re-encode of the downloaded streams after the
    # download finishes. Codec choice follows the target container (H.264/AAC
    # for MP4/MOV/MKV, MPEG-2/MP2 for MPEG, Xvid/MP3 for AVI, etc.). Output
    # goes into whatever `container` says, with mp4 as the fallback when
    # container is "None".
    reencode_h264: bool = False
    # User-picked tradeoff for the re-encode pass: "Fast" / "Balanced" /
    # "Quality". Only consulted when reencode_h264 is True. Maps to per-
    # encoder presets in _H264_ENCODER_PRESETS (and _LEGACY_QV / _VP9_PRESETS
    # for non-H.264 containers).
    reencode_quality: str = "Balanced"
    concurrent_fragments: int = 4
    cookies_browser: str = "Off"
    # Bumped by the GUI when it retries a bot-detection failure with auto-
    # detected cookies. Used to cap retries at 1 so a bad cookie jar (e.g.
    # browser not logged in) can't infinite-loop.
    retry_attempts: int = 0

    status: str = "queued"   # queued | running | done | failed | cancelled
    # Sub-phase within `running`: "download" while yt-dlp is fetching the
    # streams, "remux" during stream-copy container swap, "encode" during
    # the real ffmpeg re-encode pass. The GUI swaps the status label and
    # resets the progress bar between phases so a 100% download → 0%
    # encode transition is visible.
    phase: str = "download"
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
        reencode_quality: str = "Balanced",
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
            reencode_quality=reencode_quality or "Balanced",
            concurrent_fragments=_positive_int(concurrent_fragments, 4),
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
            task.phase = "download"
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
        if val.lower() == "auto":
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
        task.phase = "download"
        self.events.put({"type": "task_started", "task_id": task.id})
        task._thread = threading.Thread(target=self._run, args=(task,), daemon=True)
        task._thread.start()

    def _run(self, task: DownloadTask) -> None:
        try:
            task.output_dir.mkdir(parents=True, exist_ok=True)
            opts = self._build_opts(task)
            with yt_dlp.YoutubeDL(opts) as ydl:
                # The "Re-encode for compatibility" custom PP can't be
                # specified in opts['postprocessors'] (yt-dlp resolves keys
                # to its built-in classes only). Register it explicitly so
                # it runs after the FFmpegFD merger, last in the chain.
                # The PP takes references to the task + events queue so it
                # can stream ffmpeg `-progress` back to the GUI as the
                # encode runs (otherwise the bar would freeze at 100 %
                # during a long re-encode).
                target_ext = self._transcode_target(task)
                if target_ext:
                    ydl.add_post_processor(
                        _ContainerTranscodePP(
                            downloader=ydl,
                            target_ext=target_ext,
                            quality_preset=task.reencode_quality,
                            task=task,
                            events=self.events,
                        ),
                        when="post_process",
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

    def _transcode_target(self, task: DownloadTask) -> str | None:
        """Return a target extension when this task needs a real ffmpeg
        encode pass, or None when remux/no-conversion is enough."""
        if task.mode != "video":
            return None
        target = _container_target(task.container)
        if task.reencode_h264:
            return target or "mp4"
        if target in _LEGACY_TRANSCODE_CONTAINERS:
            return target
        return None

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
            "concurrent_fragment_downloads": _positive_int(task.concurrent_fragments, 4),
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
            # When the user picks a modern target container, an
            # FFmpegVideoConvertor pass remuxes the downloaded streams.
            # Convertor is a no-op when the merged file is already that
            # container, so it's safe to enable even for matching formats.
            #
            # Real transcode mode is mutually exclusive with this remux PP:
            # _run() attaches _ContainerTranscodePP separately for the
            # compatibility checkbox and for legacy containers.
            if self._transcode_target(task) is None:
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
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(value) or value <= 0:
        return "—"
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB/s"


def _fmt_eta(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    try:
        total_seconds = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if total_seconds < 0:
        return "—"
    m, s = divmod(total_seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"
