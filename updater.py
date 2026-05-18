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

# Source of truth for converting a ZIP-extracted copy into a real git
# checkout so the auto-updater can pull. Hardcoded — forks that want
# auto-update from their own remote can edit this line.
REPO_URL = "https://github.com/RamazanBerk20/youtube-video-downloader.git"
DEFAULT_BRANCH = "main"


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


def can_enable_auto_update() -> bool:
    """True iff git is installed but this folder is not yet a git checkout.

    Used to decide whether to offer to convert a ZIP-extracted install into
    a tracked git checkout so future updates can be pulled."""
    return has_git() and not (REPO_ROOT / ".git").exists()


def enable_auto_update() -> tuple[bool, str]:
    """Wire this folder up to origin/main as a tracked git checkout.

    Steps: `git init` → point HEAD at refs/heads/main → add origin remote
    → fetch origin/main → `git reset --hard FETCH_HEAD`. The reset is
    destructive: any locally-modified files are overwritten with the
    upstream version. That's acceptable here because (a) the user
    explicitly clicked the in-app "Enable auto-update" button, (b) a stock
    ZIP install has no local edits, and (c) functionally this is the same
    thing a successful `pull` would do.
    """
    if not has_git():
        return False, "git is not installed"
    if (REPO_ROOT / ".git").exists():
        return False, "already a git checkout"

    # `git init` defaults the branch name to whatever init.defaultBranch is
    # set to (master on older systems, main on newer). Force `main` so we
    # can fast-forward against origin/main without rename gymnastics.
    code, out = _run_git(["init"])
    if code != 0:
        return False, "git init failed: " + out

    code, out = _run_git(["symbolic-ref", "HEAD", f"refs/heads/{DEFAULT_BRANCH}"])
    if code != 0:
        return False, "git symbolic-ref failed: " + out

    code, out = _run_git(["remote", "add", "origin", REPO_URL])
    if code != 0:
        return False, "git remote add failed: " + out

    code, out = _run_git(["fetch", "origin", DEFAULT_BRANCH], timeout=120.0)
    if code != 0:
        return False, "git fetch failed: " + out

    code, out = _run_git(["reset", "--hard", "FETCH_HEAD"])
    if code != 0:
        return False, "git reset --hard failed: " + out

    return True, out or "Connected to origin/main."


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
