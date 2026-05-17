"""Cross-platform package installer for ffmpeg and Python.

Detects the system package manager (winget on Windows, brew on macOS,
pacman/apt/dnf on Linux) and runs the appropriate install command. On
Linux, prepends `pkexec` to trigger a graphical password prompt — if
pkexec isn't available the command is returned with `requires_manual=True`
so the caller can show it for the user to run themselves.

Output is streamed line-by-line through an `on_output` callback so a GUI
can append to a log panel in real time.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

# Package manager identifiers
WINGET = "winget"
BREW = "brew"
PACMAN = "pacman"
APT = "apt"
DNF = "dnf"
ZYPPER = "zypper"


# Per-(package, pm) -> argv. Elevation for Linux PMs is added on top later.
_COMMANDS: dict[str, dict[str, list[str]]] = {
    "ffmpeg": {
        WINGET: [
            "winget", "install", "--silent",
            "--accept-source-agreements", "--accept-package-agreements",
            "Gyan.FFmpeg",
        ],
        BREW: ["brew", "install", "ffmpeg"],
        PACMAN: ["pacman", "-S", "--noconfirm", "ffmpeg"],
        APT: ["apt-get", "install", "-y", "ffmpeg"],
        DNF: ["dnf", "install", "-y", "ffmpeg"],
        ZYPPER: ["zypper", "--non-interactive", "install", "ffmpeg"],
    },
    "python": {
        WINGET: [
            "winget", "install", "--silent",
            "--accept-source-agreements", "--accept-package-agreements",
            "Python.Python.3.13",
        ],
        BREW: ["brew", "install", "python"],
        PACMAN: ["pacman", "-S", "--noconfirm", "python"],
        APT: ["apt-get", "install", "-y", "python3", "python3-venv", "python3-pip"],
        DNF: ["dnf", "install", "-y", "python3", "python3-pip"],
        ZYPPER: ["zypper", "--non-interactive", "install", "python3", "python3-pip"],
    },
}


@dataclass
class InstallPlan:
    package: str
    pm: Optional[str]
    command: list[str]                    # command we'd run, already elevated if needed
    requires_manual: bool = False         # True → no auto-runner available
    manual_hint: str = ""                 # human-readable hint when requires_manual


def detect_pm() -> Optional[str]:
    """Detect the system package manager. Returns None if nothing usable."""
    if sys.platform == "win32":
        return WINGET if shutil.which("winget") else None
    if sys.platform == "darwin":
        return BREW if shutil.which("brew") else None
    # Linux + BSDs
    for pm in (PACMAN, APT, DNF, ZYPPER):
        # apt-get is what we actually invoke; check the canonical name too
        check = "apt-get" if pm == APT else pm
        if shutil.which(check):
            return pm
    return None


def _needs_elevation(pm: str) -> bool:
    return pm in (PACMAN, APT, DNF, ZYPPER)


def _wrap_elevation(cmd: list[str]) -> tuple[list[str], bool, str]:
    """Wrap a Linux command with a graphical elevation helper.

    Returns (wrapped_cmd, requires_manual, hint).
      - If `pkexec` exists: prefix it (GUI password prompt).
      - Else if `sudo` exists: prefix it, but warn — a no-TTY sudo will
        fail silently from a GUI app on most setups.
      - Else: return the bare command flagged as manual-only.
    """
    if shutil.which("pkexec"):
        return ["pkexec", *cmd], False, ""
    if shutil.which("sudo"):
        return ["sudo", *cmd], False, "sudo may prompt in a terminal you can't see; pkexec is preferred"
    return cmd, True, "neither pkexec nor sudo is available — run this command yourself"


def plan_install(package: str) -> InstallPlan:
    """Build (but don't run) the install plan for a package."""
    pm = detect_pm()
    if pm is None:
        return InstallPlan(
            package=package, pm=None, command=[],
            requires_manual=True,
            manual_hint="No supported package manager found on this system.",
        )
    cmd_map = _COMMANDS.get(package)
    if cmd_map is None or pm not in cmd_map:
        return InstallPlan(
            package=package, pm=pm, command=[],
            requires_manual=True,
            manual_hint=f"No automated install recipe for {package} via {pm}.",
        )
    cmd = list(cmd_map[pm])
    if _needs_elevation(pm):
        cmd, manual, hint = _wrap_elevation(cmd)
        return InstallPlan(
            package=package, pm=pm, command=cmd,
            requires_manual=manual, manual_hint=hint,
        )
    return InstallPlan(package=package, pm=pm, command=cmd)


def run_install(
    plan: InstallPlan,
    on_output: Callable[[str], None],
    timeout: float = 600.0,
) -> tuple[bool, str]:
    """Execute the plan, streaming combined stdout/stderr line-by-line.

    Returns (ok, summary). `ok` is True iff exit code 0. `summary` is a
    short status line suitable for logging.
    """
    if plan.requires_manual or not plan.command:
        return False, plan.manual_hint or "install plan has no runnable command"
    try:
        proc = subprocess.Popen(
            plan.command,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"failed to launch installer: {e}"
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line:
                on_output(line)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "installer timed out"
    return proc.returncode == 0, f"exit code {proc.returncode}"
