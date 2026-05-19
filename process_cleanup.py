"""Make sure child processes don't outlive this Python process.

yt-dlp spawns ffmpeg via subprocess.Popen during merging, audio extraction,
and our compatibility transcode. Those subprocesses are not tied to our
process lifecycle in any way — when the user closes the window, the
Popen objects in download threads get garbage-collected but the ffmpeg
children keep running, often pegging a CPU core and holding gigabytes
of memory.

Two mechanisms:
  - Windows: assign the current process to a Job Object with
    KILL_ON_JOB_CLOSE. When this process exits — cleanly or by crash —
    Windows kills everything in the job. No bookkeeping required.
  - Unix: become process-group leader at startup so children inherit
    our PGID. On exit, walk pgrep to find descendants and SIGTERM (then
    SIGKILL after a short grace period) each one. atexit covers the
    normal close path; the explicit terminate_children() call from the
    GUI's WM_DELETE_WINDOW handler covers the X-button path which
    sometimes bypasses atexit on Tk teardown.
"""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import time

# Windows Job Object handle — kept alive at module scope so the OS doesn't
# release the job (which would un-bind us from KILL_ON_JOB_CLOSE).
_job_handle: int | None = None
_armed: bool = False
# Set once terminate_children() has done its work, so the atexit-registered
# call after a manual _on_close shutdown is a fast no-op instead of
# re-running pgrep + os.kill against descendants that no longer exist.
_terminated: bool = False


def arm() -> None:
    """Set up 'kill children on exit' for this process. Idempotent.

    Unix path is intentionally minimal: we register the atexit hook and
    that's it. An earlier version called `os.setpgrp()` to put us in our
    own process group so we'd be easy to identify, but that detaches the
    process from the terminal's foreground group — meaning Ctrl+C from
    konsole no longer reaches Python, and the only escape is closing the
    terminal. terminate_children walks the descendant tree via
    `pgrep -P <pid>` (parent-PID, not PGID), so setpgrp wasn't actually
    buying us anything."""
    global _armed
    if _armed:
        return
    _armed = True
    if sys.platform == "win32":
        _setup_windows_job_object()
    atexit.register(terminate_children)


def terminate_children(timeout: float = 3.0) -> None:
    """SIGTERM every descendant, escalate to SIGKILL after `timeout`.
    On Windows, the Job Object handles this automatically; we still
    call TerminateJobObject up front so the children die before we
    return rather than racing the OS-level cleanup. Idempotent — the
    atexit hook fires this again after a manual _on_close call, and
    we don't want to spend another second polling already-dead pids."""
    global _terminated
    if _terminated:
        return
    _terminated = True
    if sys.platform == "win32":
        if _job_handle is not None:
            try:
                import ctypes
                ctypes.WinDLL("kernel32", use_last_error=True).TerminateJobObject(
                    _job_handle, 0
                )
            except (OSError, AttributeError):
                pass
        return

    pids = _find_descendant_pids()
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        alive = [pid for pid in pids if _pid_alive(pid)]
        if not alive:
            return
        time.sleep(0.1)
        pids = alive
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _find_descendant_pids() -> list[int]:
    """Return all transitive children of this process via `pgrep -P`.
    Falls back to an empty list if pgrep isn't available — in which case
    the Job Object / atexit path is our only line of defence."""
    discovered: list[int] = []
    seen: set[int] = set()
    queue: list[int] = [os.getpid()]
    while queue:
        parent = queue.pop()
        try:
            r = subprocess.run(
                ["pgrep", "-P", str(parent)],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return discovered
        for token in r.stdout.split():
            token = token.strip()
            if not token.isdigit():
                continue
            pid = int(token)
            if pid in seen:
                continue
            seen.add(pid)
            discovered.append(pid)
            queue.append(pid)
    return discovered


# ---- Windows Job Object ---------------------------------------------------

def _setup_windows_job_object() -> None:
    """Wrap the current process in a Job Object that auto-kills its
    contents when the job handle is released. Silently no-ops on failure
    (already in a non-breakaway Job, sandbox restrictions, etc.) — the
    GUI is still usable, we just don't get the auto-cleanup."""
    try:
        import ctypes
    except ImportError:
        return

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit",     ctypes.c_int64),
            ("LimitFlags",              ctypes.c_uint32),
            ("MinimumWorkingSetSize",   ctypes.c_size_t),
            ("MaximumWorkingSetSize",   ctypes.c_size_t),
            ("ActiveProcessLimit",      ctypes.c_uint32),
            ("Affinity",                ctypes.c_size_t),
            ("PriorityClass",           ctypes.c_uint32),
            ("SchedulingClass",         ctypes.c_uint32),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount",  ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount",   ctypes.c_uint64),
            ("WriteTransferCount",  ctypes.c_uint64),
            ("OtherTransferCount",  ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo",                IO_COUNTERS),
            ("ProcessMemoryLimit",    ctypes.c_size_t),
            ("JobMemoryLimit",        ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed",     ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    h_job = None
    try:
        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            return

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        if not kernel32.SetInformationJobObject(
            h_job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(h_job)
            return

        if not kernel32.AssignProcessToJobObject(
            h_job, kernel32.GetCurrentProcess(),
        ):
            kernel32.CloseHandle(h_job)
            return
    except (OSError, AttributeError):
        if h_job:
            kernel32.CloseHandle(h_job)
        return

    global _job_handle
    _job_handle = h_job
