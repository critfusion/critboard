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
  - `update_repo` defaults to "critfusion/critboard" -- that repo is public
    again, so an unauthenticated GET against it returns 200 and both the
    manual check and the periodic background check (see below) work with
    zero setup. GET /api/update/check reports "no update repo configured"
    for an empty repo instead of hitting GitHub at all; POST
    /api/update/apply refuses the same way (reason
    "update_repo_not_configured"). A fork should set "update_repo" in
    config/sources.json to its own "owner/repo".

Periodic background check (periodic_update_check, briefing Task 2):
  - Runs on an interval (`update_check_interval_s`, default 900s) driven by
    main.py's update_check_loop -- this module only implements one tick.
    Does nothing at all -- no network call -- when `update_repo` is empty
    or `update_check_enabled` is false.
  - Uses a conditional GET (ETag / If-None-Match): GitHub's unauthenticated
    limit is 60/hour, and a 304 response does NOT count against it -- this
    is what makes a 15-minute poll free. The ETag (and the last real
    result) persists across restarts via `store` (Store.get/
    set_update_check_state), so a restart doesn't throw away the cache and
    force a full request. A 304 changes NO persisted state at all -- see
    the "not_modified" branch below.
  - Never applies an update itself. `update_auto_apply` (default false) is
    read by main.py's update_check_loop, which -- separately from this
    function -- calls apply_update() when true and an update is available;
    every existing apply_update safety check still gates that call (dirty
    tree, fast-forward only, configured origin only, allow_self_update
    still required).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
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

# Periodic background check (briefing Task 2/3) -- separate from
# DEFAULT_CHECK_MIN_INTERVAL_S above, which only bounds the manual
# GET /api/update/check cache. This is main.py's update_check_loop cadence.
DEFAULT_UPDATE_CHECK_INTERVAL_S = 900.0
# Floor POST /api/settings/updates clamps check_interval_s to -- nobody can
# configure a rate-limit violation from the settings UI (see main.py's
# post_settings_updates).
MIN_UPDATE_CHECK_INTERVAL_S = 300


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def resolve_repo(config) -> str:
    """`update_repo`'s effective value: DEFAULT_UPDATE_REPO only when the
    key is entirely absent from sources.json (an old config that predates
    this key, or a from-scratch dict a test built). An explicit "" (or a
    non-string, e.g. JSON null) means self-update was deliberately disabled
    and must NOT silently fall back to the default repo -- `... or
    DEFAULT_UPDATE_REPO` would do exactly that once DEFAULT_UPDATE_REPO
    stopped being empty itself. Public (not `_resolve_repo`) so main.py's
    _update_settings_view() can report the same effective repo the
    check/apply paths actually use, instead of reaching into a private name
    or re-deriving the same logic and drifting out of sync."""
    if "update_repo" not in config.sources:
        return DEFAULT_UPDATE_REPO
    val = config.sources.get("update_repo")
    return val if isinstance(val, str) else ""


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


async def fetch_latest_commit_conditional(
    client: httpx.AsyncClient, repo: str, branch: str, etag: str | None,
    *, timeout: float = DEFAULT_CHECK_TIMEOUT_S,
) -> tuple[str | None, str | None, bool]:
    """Conditional variant of fetch_latest_commit, for the periodic
    background check (briefing Task 2): sends `If-None-Match: etag` when a
    cached ETag is already known. Returns (sha, response_etag,
    not_modified). On a 304, sha is None (not_modified=True) and the
    caller must keep using its own cached `latest` -- see
    periodic_update_check. A 304 does not count against GitHub's
    unauthenticated 60/hour rate limit, which is what makes a 15-minute
    poll free."""
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    headers = {"Accept": "application/vnd.github+json"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        resp = await client.get(url, timeout=timeout, headers=headers)
    except httpx.HTTPError as exc:
        raise UpdateError("github_unreachable", f"could not reach GitHub API: {exc}") from exc
    if resp.status_code == 304:
        return None, etag, True
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
    return sha, resp.headers.get("ETag"), False


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

    repo = resolve_repo(config)
    branch = config.sources.get("update_branch") or DEFAULT_UPDATE_BRANCH
    timeout = float(config.sources.get("quota_timeout_s", DEFAULT_CHECK_TIMEOUT_S))
    current = git_identity(dashboard_root)["commit"]

    if not repo:
        # No update_repo configured (the default -- see module docstring):
        # report this plainly instead of a GitHub 404/error, and never make
        # a network call for it.
        result = {
            "current": current,
            "latest": None,
            "behind": None,
            "checked_at": _now_iso(),
            "update_available": False,
            "repo_configured": False,
            "message": "no update repo configured",
        }
        state.cached = result
        state.last_checked_monotonic = now
        return result

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


# -- periodic background check (briefing Task 2) ------------------------------


async def periodic_update_check(
    config, dashboard_root: Path, store, *, client: httpx.AsyncClient | None = None,
) -> dict:
    """One tick of the periodic background check -- main.py's
    update_check_loop calls this every `update_check_interval_s`. Returns
    the /api/snapshot "update" object shape: {repo, branch, current,
    latest, behind, update_available, checked_at, last_error, enabled,
    auto_apply}. Makes no network call at all when `update_check_enabled`
    is false or `update_repo` is empty. See module docstring for the
    ETag/persistence contract."""
    repo = resolve_repo(config).strip()
    branch = config.sources.get("update_branch") or DEFAULT_UPDATE_BRANCH
    enabled = bool(config.sources.get("update_check_enabled", True))
    auto_apply = bool(config.sources.get("update_auto_apply", False))
    timeout = float(config.sources.get("quota_timeout_s", DEFAULT_CHECK_TIMEOUT_S))

    prior = store.get_update_check_state() or {}
    current = git_identity(dashboard_root)["commit"]

    if not enabled or not repo:
        return {
            "repo": repo or None, "branch": branch, "current": current,
            "latest": prior.get("latest"), "behind": prior.get("behind"),
            "update_available": False, "checked_at": prior.get("checked_at"),
            "last_error": None, "enabled": enabled, "auto_apply": auto_apply,
        }

    etag = prior.get("etag")
    latest = prior.get("latest")
    behind = prior.get("behind")
    checked_at = prior.get("checked_at")
    last_error = None

    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        try:
            sha, resp_etag, not_modified = await fetch_latest_commit_conditional(
                client, repo, branch, etag, timeout=timeout,
            )
        except UpdateError as exc:
            last_error = exc.message
            checked_at = _now_iso()
            store.set_update_check_state({
                "etag": etag, "latest": latest, "behind": behind,
                "checked_at": checked_at, "last_error": last_error,
            })
        else:
            if not_modified:
                # No state change at all -- GitHub confirmed the cached ETag
                # is still current. This is exactly what a 304 is for: it
                # doesn't count against the unauthenticated 60/hour limit,
                # so nothing is written and `checked_at`/`latest`/`behind`
                # stay exactly what they were.
                pass
            else:
                latest_changed = sha != latest
                latest = sha
                if latest_changed and current:
                    behind = await fetch_commits_behind(client, repo, current, latest, timeout=timeout)
                elif not current:
                    behind = None
                checked_at = _now_iso()
                store.set_update_check_state({
                    "etag": resp_etag, "latest": latest, "behind": behind,
                    "checked_at": checked_at, "last_error": None,
                })
    finally:
        if own_client:
            await client.aclose()

    update_available = bool(current) and bool(latest) and current != latest
    return {
        "repo": repo, "branch": branch, "current": current, "latest": latest,
        "behind": behind, "update_available": update_available, "checked_at": checked_at,
        "last_error": last_error, "enabled": enabled, "auto_apply": auto_apply,
    }


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


DEFAULT_SYSTEMD_UNIT = "critdash.service"

# Self-restart (PID-file / install.sh --start path -- see _restart_via_pidfile):
# how long the detached helper waits before touching the old process (lets
# this very POST's HTTP response finish flushing to the client first), how
# long it gives the old process to exit after SIGTERM before escalating to
# SIGKILL, and how long it then waits for the port to be released.
SELF_RESTART_FLUSH_DELAY_S = 1.0
SELF_RESTART_GRACEFUL_TIMEOUT_S = 5.0
SELF_RESTART_PORT_RELEASE_TIMEOUT_S = 2.0


def _unit_name_from_cgroup_text(text: str) -> str | None:
    """The deepest (leaf-most) `*.service` path segment in `text` (the
    contents of /proc/self/cgroup), or None if there isn't one.

    A systemd --user process's cgroup path looks like
    .../user@1000.service/app.slice/critdash.service -- there can be more
    than one `.service` segment (user@1000.service is systemd's own
    per-user manager, an ANCESTOR, not us), so this deliberately takes the
    last one in the path, which is always the unit directly containing
    this process, never an ancestor slice/service."""
    for line in text.splitlines():
        path = line.rsplit(":", 1)[-1]
        segments = [s for s in path.split("/") if s]
        for segment in reversed(segments):
            if segment.endswith(".service"):
                return segment
    return None


def _read_own_cgroup() -> str:
    try:
        return Path("/proc/self/cgroup").read_text()
    except OSError:
        return ""


def _detect_own_systemd_unit(config) -> tuple[str | None, bool]:
    """(unit_name_or_None, clearly_under_systemd). `clearly_under_systemd`
    is True only when INVOCATION_ID is set in our own environment --
    systemd sets this for every unit it starts (systemd.exec(5)), and
    nothing else does, so its absence means this process is definitely NOT
    supervised by systemd and must never be restarted via systemctl no
    matter what a stale/misread cgroup path might suggest.

    When under systemd, the unit name comes from THIS process's own
    /proc/self/cgroup (see _unit_name_from_cgroup_text) -- never a
    hardcoded name, so a restart can never target some other unit (real
    incident: an earlier hardcoded "critdash.service" bounced a live
    dashboard while testing against an unrelated throwaway clone). The
    configured/default name is used only as a last resort, and only once
    INVOCATION_ID has already confirmed systemd is genuinely involved."""
    if not os.environ.get("INVOCATION_ID"):
        return None, False
    detected = _unit_name_from_cgroup_text(_read_own_cgroup())
    if detected:
        return detected, True
    configured = config.sources.get("systemd_unit") if config is not None else None
    fallback = configured if isinstance(configured, str) and configured else DEFAULT_SYSTEMD_UNIT
    return fallback, True


def _pidfile_path(config) -> Path:
    return Path(config.server_dir) / "data" / "critdash.pid"


def _pidfile_names_us(config) -> bool:
    """True iff the PID file exists and names exactly THIS process. A
    missing file, an unreadable/non-numeric file, or a file naming any
    other PID (stale, or another install's process) all return False --
    the self-restart path must never act on a PID file it doesn't own."""
    try:
        pid = int(_pidfile_path(config).read_text().strip())
    except (OSError, ValueError):
        return False
    return pid == os.getpid()


# Runs as a fully detached child (start_new_session=True) so it survives
# the old process going away. Args: old_pid, uvicorn_bin, server_dir, host,
# port, pidfile, logfile, config_dir, flush_delay_s, graceful_timeout_s,
# port_release_timeout_s. Mirrors install.sh's --start step exactly:
# same uvicorn binary, same --app-dir/--host/--port invocation, same
# CRITDASH_CONFIG_DIR env var, same log file, same PID file.
_SELF_RESTART_HELPER_SRC = r"""
import os, subprocess, sys, time

(old_pid, uvicorn_bin, server_dir, host, port, pidfile, logfile, config_dir,
 flush_delay_s, graceful_timeout_s, port_release_timeout_s) = sys.argv[1:12]
old_pid = int(old_pid)
flush_delay_s = float(flush_delay_s)
graceful_timeout_s = float(graceful_timeout_s)
port_release_timeout_s = float(port_release_timeout_s)


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_while_alive(pid, timeout_s):
    deadline = time.monotonic() + timeout_s
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)


# Let the HTTP response to the POST that triggered this finish flushing to
# the client before the old process is touched.
time.sleep(flush_delay_s)

try:
    os.kill(old_pid, 15)  # SIGTERM -- ask nicely first
except OSError:
    pass
wait_while_alive(old_pid, graceful_timeout_s)
if alive(old_pid):
    try:
        os.kill(old_pid, 9)  # SIGKILL -- it didn't leave in time
    except OSError:
        pass
    wait_while_alive(old_pid, port_release_timeout_s)

env = dict(os.environ)
env["CRITDASH_CONFIG_DIR"] = config_dir
with open(logfile, "ab") as log:
    proc = subprocess.Popen(
        [uvicorn_bin, "critdash.main:app", "--app-dir", server_dir, "--host", host, "--port", port],
        cwd=server_dir, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
with open(pidfile, "w") as f:
    f.write(str(proc.pid))
"""


def _restart_via_pidfile(config) -> bool:
    """Self-restart for the install.sh --start (PID-file, no systemd) path
    -- the only supervisor a macOS install has. Spawns a detached helper
    that waits, kills the old process, and starts a new one the same way
    install.sh --start does, writing its PID to the same PID file. Fires
    and returns immediately for the same reason _detect_own_systemd_unit's
    caller does: this POST's HTTP response has to reach the client before
    the old process (the one sending it) goes away."""
    if not _pidfile_names_us(config):
        return False
    server_dir = Path(config.server_dir)
    uvicorn_bin = server_dir / ".venv" / "bin" / "uvicorn"
    if not uvicorn_bin.exists():
        return False
    host = str(config.sources.get("bind_host", "127.0.0.1"))
    port = str(config.sources.get("bind_port", 9999))
    pidfile = _pidfile_path(config)
    logfile = server_dir / "data" / "critdash.log"
    try:
        subprocess.Popen(  # noqa: S603 -- fixed argv, args are our own config values, no shell
            [
                sys.executable, "-c", _SELF_RESTART_HELPER_SRC,
                str(os.getpid()), str(uvicorn_bin), str(server_dir), host, port,
                str(pidfile), str(logfile), str(config.config_dir),
                str(SELF_RESTART_FLUSH_DELAY_S), str(SELF_RESTART_GRACEFUL_TIMEOUT_S),
                str(SELF_RESTART_PORT_RELEASE_TIMEOUT_S),
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError as exc:
        logger.warning("self-update: could not launch self-restart helper: %s", exc)
        return False


def _restart(config) -> tuple[bool, str, str]:
    """Restart whatever is actually supervising THIS process, in order:
    (a) our own systemd --user unit, detected fresh every call, never a
        hardcoded name -- restarting the wrong unit is a real outage (see
        _detect_own_systemd_unit's docstring); (b) the install.sh --start
        PID-file path (the only mechanism a macOS/no-systemd install has);
        (c) neither -- nothing here can restart the process.

    Returns (restarted, method, hint): method is "systemd" | "self" |
    "none" -- main.py's response includes this so the frontend can react
    to what actually happened instead of assuming a restart occurred just
    because git pull succeeded (macOS install report: previously restart
    silently failed on macOS and the UI claimed success anyway). `hint` is
    the command to restart manually, populated only when method == "none",
    chosen from what this call actually observed rather than guessed."""
    unit, under_systemd = _detect_own_systemd_unit(config)
    if under_systemd and unit:
        if shutil.which("systemctl"):
            try:
                subprocess.Popen(  # noqa: S603 -- fixed argv, no shell, no caller-supplied input
                    ["systemctl", "--user", "restart", unit],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return True, "systemd", ""
            except OSError as exc:
                logger.warning("self-update: could not launch systemctl restart: %s", exc)
        return False, "none", f"systemctl --user restart {unit}"

    if _restart_via_pidfile(config):
        return True, "self", ""

    return False, "none", "./install.sh --start"


def apply_update(config, dashboard_root: Path) -> dict:
    """POST /api/update/apply's full body. Synchronous (git/uv are blocking
    subprocess calls) -- main.py runs this via asyncio.to_thread so it
    doesn't block the event loop. Every refusal raises UpdateError before
    anything on disk is touched. Restarting is the last step and is
    best-effort -- see _restart for the systemd / PID-file / neither
    decision. If it doesn't fire, the pulled code is already on disk but
    the running process is still serving the OLD code; the response's
    `restart_hint` says what to run manually to pick it up."""
    if not config.sources.get("allow_self_update", False):
        raise UpdateError(
            "self_update_disabled",
            'self-update is disabled. Turn on "Allow self-update" in Settings -> Updates to enable it.',
        )

    root = Path(dashboard_root)
    if not (root / ".git").exists():
        raise UpdateError("not_a_git_checkout", f"{root} is not a git checkout -- self-update needs git")

    repo = resolve_repo(config)
    branch = config.sources.get("update_branch") or DEFAULT_UPDATE_BRANCH

    if not repo:
        raise UpdateError(
            "update_repo_not_configured",
            'no update repo configured. Set "update_repo" to "owner/repo" in '
            "config/sources.json (e.g. your own fork) to enable self-update.",
        )

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
    restarted, restart_method, restart_hint = _restart(config)
    logger.info(
        "self-update: applied, now at %s, restart %s",
        new_identity.get("commit"),
        f"via {restart_method}" if restarted else "NOT requested (none available)",
    )

    return {
        "applied": True,
        "commit": new_identity.get("commit"),
        "reinstalled": reinstalled,
        "restart_requested": restarted,
        "restart_method": restart_method,
        "restart_hint": restart_hint,
        "applied_at": _now_iso(),
    }
