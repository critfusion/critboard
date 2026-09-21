"""Self-update: git-identity for /api/version, a GitHub "is a newer commit
available" check, and a gated `git pull --ff-only` + dependency reinstall +
service restart.

Hard rules (mirrors quota.py's manual-only contract):
  - GET /api/update/check is meant to be called manually or on a long
    frontend interval, never a tight poll. This module also caches the
    result for `update_check_min_interval_s` so a caller that ignores that
    guidance still only costs one real GitHub API call per window.
  - POST /api/update/apply is OFF by default (`allow_self_update: false` in
    config/sources.json) and, even when enabled, only ever runs when a human
    calls the endpoint -- nothing in this codebase calls it automatically.
  - It executes code pulled from the internet, so every refusal path raises
    UpdateError with a stable machine-readable `reason` and a plain-language
    `message`; main.py maps each reason to a specific HTTP status, never a
    bare 500. It refuses a dirty working tree, anything that is not a clean
    fast-forward, and a pull from any remote other than the configured repo.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

logger = logging.getLogger("critdash.update")

DEFAULT_UPDATE_REPO = "critfusion/critboard"
DEFAULT_UPDATE_BRANCH = "main"
DEFAULT_CHECK_TIMEOUT_S = 10.0
DEFAULT_CHECK_MIN_INTERVAL_S = 300.0
GIT_TIMEOUT_S = 15.0
REINSTALL_TIMEOUT_S = 300.0


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class UpdateError(Exception):
    """A refused or failed update operation. `reason` is a stable machine
    code a caller (or a test) can switch on; `message` is the human string.
    main.py maps `reason` to an HTTP status -- see _UPDATE_APPLY_STATUS."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def _run_git(root: Path, *args: str, timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout, check=False,
    )


# -- git identity (GET /api/version) -----------------------------------------


def git_identity(root: Path) -> dict:
    """{"commit": <full sha or None>, "branch": <name or None>, "dirty":
    <bool or None>}. All three come back None -- never raised -- outside a
    git checkout, if `git` isn't installed, or on any subprocess hiccup: a
    tarball/zip install has no .git at all, and /api/version must degrade
    gracefully rather than 500 for that completely normal case."""
    if not (root / ".git").exists():
        return {"commit": None, "branch": None, "dirty": None}
    try:
        commit = _run_git(root, "rev-parse", "HEAD")
        branch = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        dirty = _run_git(root, "status", "--porcelain")
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("git identity unavailable: %s", exc)
        return {"commit": None, "branch": None, "dirty": None}
    return {
        "commit": commit.stdout.strip() or None if commit.returncode == 0 else None,
        "branch": branch.stdout.strip() or None if branch.returncode == 0 else None,
        "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
    }


# -- GET /api/update/check ---------------------------------------------------


async def fetch_latest_commit(
    client: httpx.AsyncClient, repo: str, branch: str, *, timeout: float = DEFAULT_CHECK_TIMEOUT_S,
) -> str:
    """Latest commit sha on `repo`'s `branch` via the GitHub REST API.
    Deliberately unauthenticated: this is one read-only lookup against a
    public repo, and requiring a GitHub token just to see "update available"
    would be a needless setup step for a self-hosted install."""
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    try:
        resp = await client.get(url, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as exc:
        raise UpdateError("github_unreachable", f"could not reach GitHub API: {exc}") from exc
    if resp.status_code != 200:
        raise UpdateError(
            "github_error", f"GitHub API returned {resp.status_code} for repos/{repo}/commits/{branch}"
        )
    try:
        sha = resp.json()["sha"]
        if not isinstance(sha, str) or not sha:
            raise ValueError("empty sha")
    except (ValueError, KeyError, TypeError) as exc:
        raise UpdateError("github_bad_response", f"unexpected GitHub API response shape: {exc}") from exc
    return sha


async def fetch_commits_behind(
    client: httpx.AsyncClient, repo: str, current: str, latest: str,
    *, timeout: float = DEFAULT_CHECK_TIMEOUT_S,
) -> int | None:
    """Number of commits `current` is behind `latest`, via GitHub's compare
    API. Returns None (never raises) if the compare call itself fails --
    `behind` degrading to unknown is not worth failing the whole check over,
    since `current`/`latest`/`update_available` are already answered."""
    if current == latest:
        return 0
    url = f"https://api.github.com/repos/{repo}/compare/{current}...{latest}"
    try:
        resp = await client.get(url, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            return None
        return int(resp.json()["ahead_by"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


@dataclass
class CheckState:
    """In-memory only, like quota.RefreshState -- resets on restart, which is
    fine: this cache exists to bound request rate within one running
    session, not to survive across restarts."""

    last_checked_monotonic: float | None = None
    cached: dict | None = None


async def check_for_update(
    config, dashboard_root: Path, state: CheckState, *, client: httpx.AsyncClient | None = None,
) -> dict:
    """GET /api/update/check's full body. Returns the previous result
    unmodified if called again within `update_check_min_interval_s` (default
    5 minutes) -- this is the backstop against a frontend bug turning a
    manual button into a tight poll; see the module docstring."""
    min_interval_s = float(config.sources.get("update_check_min_interval_s", DEFAULT_CHECK_MIN_INTERVAL_S))
    now = time.monotonic()
    if (
        state.cached is not None
        and state.last_checked_monotonic is not None
        and now - state.last_checked_monotonic < min_interval_s
    ):
        return state.cached

    repo = config.sources.get("update_repo") or DEFAULT_UPDATE_REPO
    branch = config.sources.get("update_branch") or DEFAULT_UPDATE_BRANCH
    timeout = float(config.sources.get("quota_timeout_s", DEFAULT_CHECK_TIMEOUT_S))
    current = git_identity(dashboard_root)["commit"]

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()
    try:
        latest = await fetch_latest_commit(client, repo, branch, timeout=timeout)
        behind = (
            await fetch_commits_behind(client, repo, current, latest, timeout=timeout)
            if current
            else None
        )
    finally:
        if own_client:
            await client.aclose()

    update_available = bool(current) and current != latest
    result = {
        "current": current,
        "latest": latest,
        "behind": behind,
        "checked_at": _now_iso(),
        "update_available": update_available,
    }
    state.cached = result
    state.last_checked_monotonic = now
    return result


# -- POST /api/update/apply ---------------------------------------------------


def _refuse_if_dirty(root: Path) -> None:
    status = _run_git(root, "status", "--porcelain")
    if status.returncode != 0:
        raise UpdateError("git_error", f"git status failed: {status.stderr.strip()}")
    if status.stdout.strip():
        raise UpdateError(
            "dirty_working_tree",
            "the working tree has uncommitted changes -- commit, stash, or discard them before updating",
        )


def _refuse_if_wrong_origin(root: Path, repo: str) -> None:
    origin = _run_git(root, "remote", "get-url", "origin")
    if origin.returncode != 0:
        raise UpdateError("no_origin_remote", "this checkout has no 'origin' git remote configured")
    raw_url = origin.stdout.strip()
    # normalize the SSH "scp-like" form (git@github.com:owner/repo) to the
    # same host/path shape as an https URL, so both are checked the same way
    url = raw_url.removesuffix(".git").replace("git@github.com:", "github.com/")
    expected = f"github.com/{repo}"
    if not (url.endswith(f"/{expected}") or url.endswith(expected)):
        raise UpdateError(
            "origin_mismatch",
            f"origin remote '{raw_url}' does not point at the configured "
            f"repo github.com/{repo} -- refusing to pull from an unexpected remote",
        )


def _restart_service(unit: str = "critdash.service") -> bool:
    """Best-effort `systemctl --user restart`, fired and detached. This
    deliberately does not wait: the response to this very POST has to reach
    the client before the process sending it gets killed by the restart."""
    if not shutil.which("systemctl"):
        return False
    try:
        subprocess.Popen(  # noqa: S603 -- fixed argv, no shell, no caller-supplied input
            ["systemctl", "--user", "restart", unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except OSError as exc:
        logger.warning("self-update: could not launch systemctl restart: %s", exc)
        return False


def apply_update(config, dashboard_root: Path) -> dict:
    """POST /api/update/apply's full body. Synchronous (git/uv are blocking
    subprocess calls) -- main.py runs this via asyncio.to_thread so it
    doesn't block the event loop. Every refusal raises UpdateError before
    anything on disk is touched. Restarting the service is the last step and
    is best-effort: if it doesn't fire, the pulled code is already on disk
    and `systemctl --user restart critdash.service` picks it up manually."""
    if not config.sources.get("allow_self_update", False):
        raise UpdateError(
            "self_update_disabled",
            'self-update is disabled. Set "allow_self_update": true in config/sources.json to enable it.',
        )

    root = Path(dashboard_root)
    if not (root / ".git").exists():
        raise UpdateError("not_a_git_checkout", f"{root} is not a git checkout -- self-update needs git")

    repo = config.sources.get("update_repo") or DEFAULT_UPDATE_REPO
    branch = config.sources.get("update_branch") or DEFAULT_UPDATE_BRANCH

    _refuse_if_dirty(root)
    _refuse_if_wrong_origin(root, repo)

    lockfile = root / "server" / "uv.lock"
    lock_before = lockfile.read_bytes() if lockfile.exists() else None

    fetch = _run_git(root, "fetch", "origin", branch)
    if fetch.returncode != 0:
        raise UpdateError("fetch_failed", f"git fetch origin {branch} failed: {fetch.stderr.strip()}")

    pull = _run_git(root, "pull", "--ff-only", "origin", branch)
    if pull.returncode != 0:
        raise UpdateError(
            "not_fast_forward",
            f"git pull --ff-only failed (local history has diverged from origin/{branch}): "
            f"{pull.stderr.strip()}",
        )
    logger.info("self-update: pulled origin/%s -- %s", branch, pull.stdout.strip())

    lock_after = lockfile.read_bytes() if lockfile.exists() else None
    reinstalled = False
    if lock_after is not None and lock_after != lock_before:
        server_dir = root / "server"
        uv_bin = shutil.which("uv")
        venv_python = str(server_dir / ".venv" / "bin" / "python")
        cmd = [uv_bin, "sync"] if uv_bin else [venv_python, "-m", "pip", "install", "-e", "."]
        sync = subprocess.run(  # noqa: S603 -- fixed argv built from a resolved binary path, no shell
            cmd, cwd=server_dir, capture_output=True, text=True, timeout=REINSTALL_TIMEOUT_S, check=False,
        )
        reinstalled = sync.returncode == 0
        if not reinstalled:
            logger.warning("self-update: dependency reinstall FAILED: %s", sync.stderr.strip()[:500])
            raise UpdateError(
                "reinstall_failed",
                f"git pull succeeded but dependency reinstall failed: {sync.stderr.strip()[:500]}",
            )
        logger.info("self-update: uv.lock changed, dependencies reinstalled")

    new_identity = git_identity(root)
    restarted = _restart_service()
    logger.info(
        "self-update: applied, now at %s, restart %s",
        new_identity.get("commit"), "requested" if restarted else "NOT requested (no systemctl)",
    )

    return {
        "applied": True,
        "commit": new_identity.get("commit"),
        "reinstalled": reinstalled,
        "restart_requested": restarted,
        "applied_at": _now_iso(),
    }
