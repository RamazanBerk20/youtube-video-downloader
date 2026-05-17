"""Git-backed self-updater.

Checks whether the local repo is behind its upstream branch (`origin/main`
by default) and runs a fast-forward pull when the user asks. All git
operations are wrapped in subprocess calls with timeouts; failures
(no git, no remote, no network, ZIP download with no .git, local commits
ahead of upstream) degrade silently — the caller is expected to hide the
update banner if `commits_behind()` returns -1.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
UPSTREAM = "origin/main"


def has_git() -> bool:
    return shutil.which("git") is not None


def is_git_checkout() -> bool:
    return has_git() and (REPO_ROOT / ".git").exists()


def _run_git(args: list[str], timeout: float = 30.0) -> tuple[int, str]:
    """Run `git -C <repo> <args>`; return (returncode, combined_output)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (result.stdout or "") + (result.stderr or "")
        return result.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return -1, "git timed out"
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def current_commit() -> str:
    if not is_git_checkout():
        return ""
    code, out = _run_git(["rev-parse", "--short", "HEAD"])
    return out if code == 0 else ""


def commits_behind() -> tuple[int, str | None]:
    """Return (count, error).

    `count` is the number of commits the user is behind upstream.
      0  = up-to-date
      >0 = N commits behind
      -1 = check failed (not a git checkout, no network, etc.) — caller
           should treat this as "unknown" and not show a banner.
    `error` is a short human-readable reason when `count == -1`, otherwise None.
    """
    if not is_git_checkout():
        return -1, "not a git checkout"
    code, _ = _run_git(["fetch", "--quiet", "origin"], timeout=30.0)
    if code != 0:
        return -1, "git fetch failed (network?)"
    code, out = _run_git(["rev-list", "--count", f"HEAD..{UPSTREAM}"])
    if code != 0:
        return -1, "git rev-list failed"
    try:
        return int(out), None
    except ValueError:
        return -1, f"unparseable rev-list output: {out!r}"


def pull() -> tuple[bool, str]:
    """Fast-forward only pull. Returns (ok, output).

    --ff-only means we never auto-merge; if the user has local commits, the
    pull aborts with a clear message rather than creating a merge commit
    behind their back.
    """
    if not is_git_checkout():
        return False, "not a git checkout"
    code, out = _run_git(["pull", "--ff-only", "origin", "main"], timeout=120.0)
    return code == 0, out
