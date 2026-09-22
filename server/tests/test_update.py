"""critdash/update.py: git identity, the GitHub "update available" check, and
the gated git pull --ff-only + reinstall + restart apply path.

apply_update() end-to-end tests use real local git repos (same pattern as
test_productivity.py's _init_repo) with a "remote" whose local filesystem
path is deliberately shaped .../github.com/<owner>/<repo>(.git) so
_refuse_if_wrong_origin's real string check (never mocked) passes against a
same-machine remote -- git treats a local path remote exactly like any other.
_restart is monkeypatched in every apply_update test that reaches it, so no
test ever calls the real `systemctl` and risks touching a real running
service. _restart's own pieces (systemd unit detection, PID-file ownership,
the self-restart helper) are covered directly further down.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from critdash import update
from critdash.store import Store

# server/tests/test_update.py -> dashboard root is three parents up
DASHBOARD_ROOT = Path(__file__).resolve().parents[2]
REAL_VENV = DASHBOARD_ROOT / "server" / ".venv"


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _Cfg:
    def __init__(self, *, server_dir=None, config_dir=None, **sources):
        self.sources = sources
        self.server_dir = server_dir
        self.config_dir = config_dir


def _run(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path, msg="first"):
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "-q"], path)
    _run(["config", "user.email", "user@example.com"], path)
    _run(["config", "user.name", "t"], path)
    (path / "a.txt").write_text(f"{msg}\n")
    _run(["add", "a.txt"], path)
    _run(["commit", "-qm", msg], path)


def _head(path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


# -- git_identity -------------------------------------------------------------


def test_git_identity_outside_a_checkout_is_all_none(tmp_path):
    ident = update.git_identity(tmp_path)
    assert ident == {"commit": None, "branch": None, "dirty": None}


def test_git_identity_reports_commit_branch_and_clean(tmp_path):
    _init_repo(tmp_path)
    ident = update.git_identity(tmp_path)
    assert ident["commit"] == _head(tmp_path)
    assert ident["branch"]  # some branch name, main or master depending on git config
    assert ident["dirty"] is False


def test_git_identity_reports_dirty_true_for_uncommitted_changes(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed\n")
    ident = update.git_identity(tmp_path)
    assert ident["dirty"] is True


# -- fetch_latest_commit / fetch_commits_behind --------------------------------


async def test_fetch_latest_commit_success():
    def handler(request):
        assert request.url.path == "/repos/o/r/commits/main"
        return httpx.Response(200, json={"sha": "abc123"})

    sha = await update.fetch_latest_commit(mock_client(handler), "o/r", "main")
    assert sha == "abc123"


async def test_fetch_latest_commit_non_200_raises_github_error():
    def handler(_request):
        return httpx.Response(404)

    with pytest.raises(update.UpdateError) as exc_info:
        await update.fetch_latest_commit(mock_client(handler), "o/r", "main")
    assert exc_info.value.reason == "github_error"


async def test_fetch_latest_commit_malformed_json_raises_bad_response():
    def handler(_request):
        return httpx.Response(200, json={"nope": "no sha field"})

    with pytest.raises(update.UpdateError) as exc_info:
        await update.fetch_latest_commit(mock_client(handler), "o/r", "main")
    assert exc_info.value.reason == "github_bad_response"


async def test_fetch_latest_commit_network_error_raises_unreachable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(update.UpdateError) as exc_info:
        await update.fetch_latest_commit(mock_client(handler), "o/r", "main")
    assert exc_info.value.reason == "github_unreachable"


async def test_fetch_commits_behind_identical_shas_is_zero():
    n = await update.fetch_commits_behind(mock_client(lambda r: httpx.Response(200)), "o/r", "same", "same")
    assert n == 0


async def test_fetch_commits_behind_reads_ahead_by():
    def handler(request):
        assert request.url.path == "/repos/o/r/compare/aaa...bbb"
        return httpx.Response(200, json={"ahead_by": 3})

    n = await update.fetch_commits_behind(mock_client(handler), "o/r", "aaa", "bbb")
    assert n == 3


async def test_fetch_commits_behind_failure_returns_none_not_raise():
    def handler(_request):
        return httpx.Response(500)

    n = await update.fetch_commits_behind(mock_client(handler), "o/r", "aaa", "bbb")
    assert n is None


# -- resolve_repo (defect 4: _update_settings_view must use this, not a naive
#    `.get("update_repo") or ""`) ------------------------------------------


def test_resolve_repo_absent_key_returns_default():
    assert update.resolve_repo(_Cfg()) == update.DEFAULT_UPDATE_REPO


def test_resolve_repo_explicit_empty_stays_empty():
    assert update.resolve_repo(_Cfg(update_repo="")) == ""


def test_resolve_repo_non_string_value_returns_empty():
    assert update.resolve_repo(_Cfg(update_repo=None)) == ""


def test_resolve_repo_returns_configured_value():
    assert update.resolve_repo(_Cfg(update_repo="someone/fork")) == "someone/fork"


# -- check_for_update ----------------------------------------------------------


async def test_check_for_update_shape_and_update_available(tmp_path):
    _init_repo(tmp_path)
    current = _head(tmp_path)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "deadbeef"})
        return httpx.Response(200, json={"ahead_by": 5})

    cfg = _Cfg(update_repo="o/r", update_branch="main")
    state = update.CheckState()
    result = await update.check_for_update(cfg, tmp_path, state, client=mock_client(handler))

    assert result["current"] == current
    assert result["latest"] == "deadbeef"
    assert result["behind"] == 5
    assert result["update_available"] is True
    assert set(result.keys()) == {"current", "latest", "behind", "checked_at", "update_available"}


async def test_check_for_update_no_git_checkout_is_not_update_available(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"sha": "deadbeef"})

    cfg = _Cfg(update_repo="o/r", update_branch="main")
    state = update.CheckState()
    result = await update.check_for_update(cfg, tmp_path, state, client=mock_client(handler))

    assert result["current"] is None
    assert result["update_available"] is False
    assert result["behind"] is None


async def test_check_for_update_respects_cache_within_min_interval(tmp_path):
    _init_repo(tmp_path)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"sha": "deadbeef", "ahead_by": 1})

    cfg = _Cfg(update_repo="o/r", update_branch="main", update_check_min_interval_s=300)
    state = update.CheckState()
    r1 = await update.check_for_update(cfg, tmp_path, state, client=mock_client(handler))
    calls_after_first = calls["n"]
    r2 = await update.check_for_update(cfg, tmp_path, state, client=mock_client(handler))

    assert r1 == r2
    assert calls["n"] == calls_after_first  # second call served entirely from cache, no new HTTP calls


async def test_check_for_update_refreshes_once_interval_elapses(tmp_path, monkeypatch):
    _init_repo(tmp_path)

    def handler(request):
        return httpx.Response(200, json={"sha": "deadbeef", "ahead_by": 1})

    cfg = _Cfg(update_repo="o/r", update_branch="main", update_check_min_interval_s=0.01)
    state = update.CheckState()
    await update.check_for_update(cfg, tmp_path, state, client=mock_client(handler))
    first_checked_at = state.last_checked_monotonic
    time.sleep(0.02)
    await update.check_for_update(cfg, tmp_path, state, client=mock_client(handler))
    assert state.last_checked_monotonic > first_checked_at


async def test_check_for_update_empty_repo_reports_not_configured_with_no_http_call(tmp_path):
    """Bug 3: update_repo defaults to "" (a third party has no access to
    critfusion/critboard, now private) -- the check must report this plainly
    and never touch the network."""
    _init_repo(tmp_path)
    current = _head(tmp_path)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"sha": "deadbeef"})

    cfg = _Cfg(update_repo="", update_branch="main")
    state = update.CheckState()
    result = await update.check_for_update(cfg, tmp_path, state, client=mock_client(handler))

    assert calls["n"] == 0
    assert result["current"] == current
    assert result["latest"] is None
    assert result["behind"] is None
    assert result["update_available"] is False
    assert result["repo_configured"] is False
    assert result["message"] == "no update repo configured"


async def test_check_for_update_uses_default_repo_when_unset(tmp_path):
    """No update_repo key at all in sources.json -- falls back to
    DEFAULT_UPDATE_REPO ("critfusion/critboard", now public) rather than
    "not configured": an old config that predates this key, or the owner's
    live instance, gets a working update check with zero edits."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        assert request.url.path == "/repos/critfusion/critboard/commits/main"
        return httpx.Response(200, json={"sha": "deadbeef"})

    cfg = _Cfg()  # no update_repo at all
    state = update.CheckState()
    result = await update.check_for_update(cfg, tmp_path, state, client=mock_client(handler))

    assert calls["n"] == 1
    assert result["latest"] == "deadbeef"


async def test_check_for_update_explicit_empty_repo_stays_not_configured(tmp_path):
    """An explicit "" is different from an absent key -- it means the owner
    deliberately disabled the check, and must NOT silently fall back to
    DEFAULT_UPDATE_REPO just because that default is no longer empty."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"sha": "deadbeef"})

    cfg = _Cfg(update_repo="")
    state = update.CheckState()
    result = await update.check_for_update(cfg, tmp_path, state, client=mock_client(handler))

    assert calls["n"] == 0
    assert result["repo_configured"] is False


def test_apply_update_refuses_empty_repo(tmp_path):
    _init_repo(tmp_path)
    with pytest.raises(update.UpdateError) as exc_info:
        update.apply_update(_Cfg(allow_self_update=True, update_repo=""), tmp_path)
    assert exc_info.value.reason == "update_repo_not_configured"


# -- apply_update: refusal paths -----------------------------------------------


def test_apply_update_refuses_when_disabled_by_default(tmp_path):
    _init_repo(tmp_path)
    with pytest.raises(update.UpdateError) as exc_info:
        update.apply_update(_Cfg(), tmp_path)
    assert exc_info.value.reason == "self_update_disabled"


def test_apply_update_refuses_when_not_a_git_checkout(tmp_path):
    with pytest.raises(update.UpdateError) as exc_info:
        update.apply_update(_Cfg(allow_self_update=True), tmp_path)
    assert exc_info.value.reason == "not_a_git_checkout"


def test_apply_update_refuses_dirty_working_tree(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("uncommitted change\n")
    with pytest.raises(update.UpdateError) as exc_info:
        update.apply_update(_Cfg(allow_self_update=True, update_repo="o/r"), tmp_path)
    assert exc_info.value.reason == "dirty_working_tree"


def test_apply_update_refuses_missing_origin_remote(tmp_path):
    _init_repo(tmp_path)
    with pytest.raises(update.UpdateError) as exc_info:
        update.apply_update(_Cfg(allow_self_update=True, update_repo="o/r"), tmp_path)
    assert exc_info.value.reason == "no_origin_remote"


def test_apply_update_refuses_origin_that_does_not_match_configured_repo(tmp_path):
    _init_repo(tmp_path)
    _run(["remote", "add", "origin", "https://github.com/someone-else/other-repo.git"], tmp_path)
    with pytest.raises(update.UpdateError) as exc_info:
        update.apply_update(_Cfg(allow_self_update=True, update_repo="critfusion/critboard"), tmp_path)
    assert exc_info.value.reason == "origin_mismatch"


def test_refuse_if_wrong_origin_accepts_https_and_scp_like_ssh_forms(tmp_path):
    for url in (
        "https://github.com/critfusion/critboard.git",
        "https://github.com/critfusion/critboard",
        "git@github.com:critfusion/critboard.git",
    ):
        repo = tmp_path / url.replace("/", "_").replace(":", "_")
        _init_repo(repo)
        _run(["remote", "add", "origin", url], repo)
        update._refuse_if_wrong_origin(repo, "critfusion/critboard")  # must not raise


# -- apply_update: end-to-end success / not-fast-forward -----------------------


def _make_remote_and_clone(tmp_path, owner="testowner", repo="testrepo"):
    """A bare 'remote' repo whose local filesystem path ends in
    .../github.com/<owner>/<repo>.git, and a clone of it -- so the clone's
    real `origin` URL (a plain local path, never mocked) satisfies
    _refuse_if_wrong_origin's github.com/<owner>/<repo> suffix check."""
    remote_dir = tmp_path / "remote" / "github.com" / owner / f"{repo}.git"
    remote_dir.parent.mkdir(parents=True, exist_ok=True)
    seed = tmp_path / "seed"
    _init_repo(seed)
    _run(["init", "-q", "--bare", str(remote_dir)], tmp_path)
    _run(["push", "-q", str(remote_dir), "HEAD:main"], seed)

    clone_dir = tmp_path / "clone"
    _run(["clone", "-q", str(remote_dir), str(clone_dir)], tmp_path)
    _run(["checkout", "-q", "-B", "main"], clone_dir)
    return remote_dir, seed, clone_dir


def test_apply_update_success_pulls_ff_and_skips_reinstall_when_lock_unchanged(tmp_path, monkeypatch):
    remote_dir, seed, clone_dir = _make_remote_and_clone(tmp_path)

    # advance the remote past the clone by one commit
    (seed / "a.txt").write_text("second\n")
    _run(["commit", "-qam", "second"], seed)
    _run(["push", "-q", str(remote_dir), "HEAD:main"], seed)
    new_head = _head(seed)

    monkeypatch.setattr(update, "_restart", lambda config: (True, "systemd", ""))
    cfg = _Cfg(allow_self_update=True, update_repo="testowner/testrepo", update_branch="main")
    result = update.apply_update(cfg, clone_dir)

    assert result["applied"] is True
    assert result["commit"] == new_head
    assert result["reinstalled"] is False
    assert result["restart_requested"] is True
    assert result["restart_method"] == "systemd"
    assert result["restart_hint"] == ""
    assert _head(clone_dir) == new_head


def test_apply_update_reinstalls_when_lockfile_changed(tmp_path, monkeypatch):
    remote_dir, seed, clone_dir = _make_remote_and_clone(tmp_path)
    (seed / "server").mkdir()
    (seed / "server" / "uv.lock").write_text("v1\n")
    _run(["add", "-A"], seed)
    _run(["commit", "-qm", "add lockfile"], seed)
    _run(["push", "-q", str(remote_dir), "HEAD:main"], seed)
    _run(["pull", "-q", str(remote_dir), "main"], clone_dir)  # sync clone to v1 first

    (seed / "server" / "uv.lock").write_text("v2\n")
    _run(["commit", "-qam", "bump lockfile"], seed)
    _run(["push", "-q", str(remote_dir), "HEAD:main"], seed)

    monkeypatch.setattr(update, "_restart", lambda config: (False, "none", "./install.sh --start"))
    reinstall_calls = []
    real_run = subprocess.run

    def fake_run(cmd, *, cwd, capture_output, text, timeout, check):
        if cmd and cmd[0] == "git":
            return real_run(
                cmd, cwd=cwd, capture_output=capture_output, text=text, timeout=timeout, check=check
            )
        reinstall_calls.append((cmd, cwd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    cfg = _Cfg(allow_self_update=True, update_repo="testowner/testrepo", update_branch="main")
    result = update.apply_update(cfg, clone_dir)

    assert result["reinstalled"] is True
    assert len(reinstall_calls) == 1
    assert reinstall_calls[0][1] == clone_dir / "server"
    assert result["restart_requested"] is False
    assert result["restart_method"] == "none"
    assert result["restart_hint"] == "./install.sh --start"


def test_apply_update_refuses_non_fast_forward_on_diverged_history(tmp_path, monkeypatch):
    remote_dir, seed, clone_dir = _make_remote_and_clone(tmp_path)

    # remote advances...
    (seed / "a.txt").write_text("remote-side change\n")
    _run(["commit", "-qam", "remote change"], seed)
    _run(["push", "-q", str(remote_dir), "HEAD:main"], seed)

    # ...while the clone ALSO commits locally, on top of the old HEAD --
    # history has now diverged, so --ff-only must refuse.
    (clone_dir / "b.txt").write_text("local-only change\n")
    _run(["add", "b.txt"], clone_dir)
    _run(["commit", "-qm", "local change"], clone_dir)

    monkeypatch.setattr(update, "_restart", lambda config: (True, "systemd", ""))
    cfg = _Cfg(allow_self_update=True, update_repo="testowner/testrepo", update_branch="main")
    with pytest.raises(update.UpdateError) as exc_info:
        update.apply_update(cfg, clone_dir)
    assert exc_info.value.reason == "not_fast_forward"


# -- periodic_update_check (briefing Task 2) -----------------------------------


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "periodic.db")
    return s


async def test_periodic_check_skips_with_no_network_call_when_disabled(tmp_path):
    _init_repo(tmp_path)
    store = _store(tmp_path)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"sha": "deadbeef"})

    cfg = _Cfg(update_repo="o/r", update_check_enabled=False)
    result = await update.periodic_update_check(cfg, tmp_path, store, client=mock_client(handler))

    assert calls["n"] == 0
    assert result["enabled"] is False
    assert result["update_available"] is False
    store.close()


async def test_periodic_check_skips_with_no_network_call_when_repo_empty(tmp_path):
    _init_repo(tmp_path)
    store = _store(tmp_path)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"sha": "deadbeef"})

    cfg = _Cfg(update_repo="", update_check_enabled=True)
    result = await update.periodic_update_check(cfg, tmp_path, store, client=mock_client(handler))

    assert calls["n"] == 0
    assert result["repo"] is None
    assert result["update_available"] is False
    store.close()


async def test_periodic_check_first_run_persists_etag_and_state(tmp_path):
    _init_repo(tmp_path)
    current = _head(tmp_path)
    store = _store(tmp_path)

    def handler(request):
        assert "If-None-Match" not in request.headers
        return httpx.Response(200, json={"sha": "deadbeef"}, headers={"ETag": '"abc123"'})

    cfg = _Cfg(update_repo="o/r", update_branch="main", update_check_enabled=True)
    result = await update.periodic_update_check(cfg, tmp_path, store, client=mock_client(handler))

    assert result["current"] == current
    assert result["latest"] == "deadbeef"
    assert result["update_available"] is True
    assert result["last_error"] is None
    assert set(result.keys()) == {
        "repo", "branch", "current", "latest", "behind", "update_available",
        "checked_at", "last_error", "enabled", "auto_apply",
    }

    persisted = store.get_update_check_state()
    assert persisted["etag"] == '"abc123"'
    assert persisted["latest"] == "deadbeef"
    store.close()


async def test_periodic_check_304_makes_no_state_change(tmp_path):
    """The core ETag contract: once a 304 comes back, NOTHING persisted
    changes -- not latest, not behind, not checked_at, not the etag
    itself. This is what makes a 15-minute poll free against GitHub's
    unauthenticated 60/hour limit."""
    _init_repo(tmp_path)
    store = _store(tmp_path)

    def handler_200(request):
        return httpx.Response(200, json={"sha": "deadbeef"}, headers={"ETag": '"etag-1"'})

    cfg = _Cfg(update_repo="o/r", update_branch="main", update_check_enabled=True)
    await update.periodic_update_check(cfg, tmp_path, store, client=mock_client(handler_200))
    before = store.get_update_check_state()

    seen_if_none_match = {}

    def handler_304(request):
        seen_if_none_match["value"] = request.headers.get("If-None-Match")
        return httpx.Response(304)

    result = await update.periodic_update_check(cfg, tmp_path, store, client=mock_client(handler_304))
    after = store.get_update_check_state()

    assert seen_if_none_match["value"] == '"etag-1"'
    assert after == before  # no state change at all
    assert result["latest"] == "deadbeef"  # still reports the cached value
    assert result["checked_at"] == before["checked_at"]
    store.close()


async def test_periodic_check_error_persists_last_error_but_keeps_prior_facts(tmp_path):
    _init_repo(tmp_path)
    store = _store(tmp_path)

    def handler_200(request):
        return httpx.Response(200, json={"sha": "deadbeef"}, headers={"ETag": '"etag-1"'})

    cfg = _Cfg(update_repo="o/r", update_branch="main", update_check_enabled=True)
    await update.periodic_update_check(cfg, tmp_path, store, client=mock_client(handler_200))

    def handler_fail(request):
        return httpx.Response(500)

    result = await update.periodic_update_check(cfg, tmp_path, store, client=mock_client(handler_fail))

    assert result["last_error"] is not None
    assert result["latest"] == "deadbeef"  # kept from the last successful check
    persisted = store.get_update_check_state()
    assert persisted["last_error"] is not None
    assert persisted["latest"] == "deadbeef"
    store.close()


async def test_periodic_check_auto_apply_false_by_default(tmp_path):
    _init_repo(tmp_path)
    store = _store(tmp_path)

    def handler(request):
        return httpx.Response(200, json={"sha": "deadbeef"})

    cfg = _Cfg(update_repo="o/r", update_check_enabled=True)
    result = await update.periodic_update_check(cfg, tmp_path, store, client=mock_client(handler))
    assert result["auto_apply"] is False
    store.close()


# -- systemd unit detection (macOS install report: restart must target OUR
#    OWN unit, never a hardcoded name -- a hardcoded "critdash.service"
#    once bounced an unrelated live dashboard during a throwaway-clone test)
# ------------------------------------------------------------------------


def test_unit_name_from_cgroup_text_picks_the_leaf_service():
    """Real-world cgroup v2 path for a systemd --user unit: multiple
    .service segments (user@1000.service is systemd's own per-user
    manager, an ANCESTOR, not us) -- the deepest one is always ours."""
    text = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/critdash.service\n"
    assert update._unit_name_from_cgroup_text(text) == "critdash.service"


def test_unit_name_from_cgroup_text_returns_the_actual_unit_not_a_hardcoded_name():
    """A process running under some OTHER unit must report that unit's
    real name, never the hardcoded default -- this is the exact bug that
    caused the real incident (see module docstring)."""
    text = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/some-other-app.service\n"
    name = update._unit_name_from_cgroup_text(text)
    assert name == "some-other-app.service"
    assert name != update.DEFAULT_SYSTEMD_UNIT


def test_unit_name_from_cgroup_text_no_service_segment_returns_none():
    text = "0::/user.slice/user-1000.slice/session-260.scope\n"
    assert update._unit_name_from_cgroup_text(text) is None


def test_detect_own_systemd_unit_without_invocation_id_is_never_under_systemd(monkeypatch):
    """Even if /proc/self/cgroup LOOKS like a systemd unit path, without
    INVOCATION_ID in our own env we are definitely not running under
    systemd (systemd sets this for every unit it starts, nothing else
    does) -- detection must refuse to guess a unit to restart at all."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(update, "_read_own_cgroup", lambda: "0::/user.slice/.../app.slice/critdash.service\n")
    unit, under_systemd = update._detect_own_systemd_unit(_Cfg())
    assert under_systemd is False
    assert unit is None


def test_detect_own_systemd_unit_uses_real_cgroup_when_under_systemd(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "deadbeefdeadbeefdeadbeefdeadbeef")
    monkeypatch.setattr(
        update, "_read_own_cgroup",
        lambda: "0::/user.slice/user-1000.slice/user@1000.service/app.slice/my-actual-unit.service\n",
    )
    unit, under_systemd = update._detect_own_systemd_unit(_Cfg())
    assert under_systemd is True
    assert unit == "my-actual-unit.service"


def test_detect_own_systemd_unit_falls_back_to_default_only_when_clearly_under_systemd(monkeypatch):
    """cgroup parsing failed (no .service segment at all) but INVOCATION_ID
    confirms systemd IS involved -- only then is the configured/default
    name used."""
    monkeypatch.setenv("INVOCATION_ID", "deadbeefdeadbeefdeadbeefdeadbeef")
    monkeypatch.setattr(update, "_read_own_cgroup", lambda: "0::/user.slice/session-1.scope\n")
    unit, under_systemd = update._detect_own_systemd_unit(_Cfg())
    assert under_systemd is True
    assert unit == update.DEFAULT_SYSTEMD_UNIT

    unit2, _ = update._detect_own_systemd_unit(_Cfg(systemd_unit="configured-unit.service"))
    assert unit2 == "configured-unit.service"


# -- PID-file ownership (install.sh --start / macOS path): must refuse to
#    act on a PID file that doesn't name THIS process ------------------


def test_pidfile_names_us_true_for_own_pid(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "critdash.pid").write_text(str(os.getpid()))
    assert update._pidfile_names_us(_Cfg(server_dir=tmp_path)) is True


def test_pidfile_names_us_false_when_missing(tmp_path):
    assert update._pidfile_names_us(_Cfg(server_dir=tmp_path)) is False


def test_pidfile_names_us_false_for_someone_elses_pid(tmp_path):
    (tmp_path / "data").mkdir()
    # PID 1 (init/systemd) is essentially guaranteed to not be us
    (tmp_path / "data" / "critdash.pid").write_text("1")
    assert update._pidfile_names_us(_Cfg(server_dir=tmp_path)) is False


def test_pidfile_names_us_false_for_garbage_content(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "critdash.pid").write_text("not-a-pid")
    assert update._pidfile_names_us(_Cfg(server_dir=tmp_path)) is False


def test_restart_via_pidfile_refuses_and_spawns_nothing_when_pidfile_is_not_ours(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "critdash.pid").write_text("1")
    spawned = []
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))
    assert update._restart_via_pidfile(_Cfg(server_dir=tmp_path, config_dir=tmp_path)) is False
    assert spawned == []


# -- _restart dispatch order: systemd, then PID-file self-restart, then
#    neither -- and the systemd branch must never fire when we are not
#    clearly running under systemd -------------------------------------


def test_restart_prefers_systemd_when_detected(monkeypatch):
    monkeypatch.setattr(update, "_detect_own_systemd_unit", lambda config: ("my-unit.service", True))
    monkeypatch.setattr(
        update.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
    )
    calls = []
    monkeypatch.setattr(update.subprocess, "Popen", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(update, "_restart_via_pidfile", lambda config: pytest.fail("must not try pidfile"))

    restarted, method, hint = update._restart(_Cfg())
    assert (restarted, method, hint) == (True, "systemd", "")
    assert calls == [["systemctl", "--user", "restart", "my-unit.service"]]


def test_restart_does_not_use_systemd_when_not_clearly_under_it(monkeypatch):
    """Even if systemctl happens to be on PATH (e.g. a Linux dev box with
    systemd installed but this particular process wasn't started by it),
    detection reporting under_systemd=False must skip the systemd branch
    entirely -- this is what stops the real incident's class of bug."""
    monkeypatch.setattr(update, "_detect_own_systemd_unit", lambda config: (None, False))
    monkeypatch.setattr(update.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: pytest.fail("must not call systemctl"))
    monkeypatch.setattr(update, "_restart_via_pidfile", lambda config: True)

    restarted, method, hint = update._restart(_Cfg())
    assert (restarted, method, hint) == (True, "self", "")


def test_restart_falls_back_to_pidfile_when_no_systemd(monkeypatch):
    monkeypatch.setattr(update, "_detect_own_systemd_unit", lambda config: (None, False))
    monkeypatch.setattr(update, "_restart_via_pidfile", lambda config: True)
    restarted, method, hint = update._restart(_Cfg())
    assert (restarted, method, hint) == (True, "self", "")


def test_restart_reports_none_with_a_usable_hint_when_nothing_available(monkeypatch):
    monkeypatch.setattr(update, "_detect_own_systemd_unit", lambda config: (None, False))
    monkeypatch.setattr(update, "_restart_via_pidfile", lambda config: False)
    restarted, method, hint = update._restart(_Cfg())
    assert restarted is False
    assert method == "none"
    assert hint == "./install.sh --start"


def test_restart_hint_uses_systemctl_command_when_under_systemd_but_restart_failed(monkeypatch):
    monkeypatch.setattr(update, "_detect_own_systemd_unit", lambda config: ("my-unit.service", True))
    monkeypatch.setattr(update.shutil, "which", lambda name: None)  # systemctl "vanished"
    restarted, method, hint = update._restart(_Cfg())
    assert restarted is False
    assert method == "none"
    assert hint == "systemctl --user restart my-unit.service"


# -- end-to-end self-restart (install.sh --start / macOS path), real
#    processes: a throwaway clone one commit behind a local origin, served
#    by a test instance with NO systemctl reachable on PATH. Proves the
#    actual restart mechanism, not just the HTTP response shape.
#
#    ABSOLUTE SAFETY RULE: the child server's PATH is built from scratch
#    (symlinks to just nohup/curl/git) so `shutil.which("systemctl")`
#    inside it can only ever return None -- this test can NEVER reach the
#    systemd branch, let alone a real systemctl. ------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ignore_for_seed(dirpath, names):
    ignored = {".git", ".venv", "data", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
    result = {n for n in names if n in ignored}
    if os.path.basename(dirpath) == "config":
        result |= {n for n in names if n in {"sources.json", "layout.json"}}
    return result


@pytest.mark.skipif(
    not REAL_VENV.exists(), reason="server/.venv not built -- nothing to reuse for the real server"
)
def test_apply_update_self_restart_end_to_end_swaps_the_process(tmp_path):
    seed = tmp_path / "origin_seed"
    shutil.copytree(DASHBOARD_ROOT, seed, ignore=_ignore_for_seed)

    remote_dir = tmp_path / "origin" / "github.com" / "testowner" / "testrepo.git"
    remote_dir.parent.mkdir(parents=True)
    _run(["init", "-q", "--bare", str(remote_dir)], tmp_path)

    _run(["init", "-q", "-b", "main"], seed)
    _run(["config", "user.email", "t@example.com"], seed)
    _run(["config", "user.name", "t"], seed)
    _run(["add", "-A"], seed)
    _run(["commit", "-qm", "v1"], seed)
    _run(["push", "-q", str(remote_dir), "HEAD:main"], seed)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote_dir, check=True)

    workdir = tmp_path / "workdir"
    _run(["clone", "-q", str(remote_dir), str(workdir)], tmp_path)
    v1_head = _head(workdir)

    (workdir / "server" / ".venv").symlink_to(REAL_VENV)
    (workdir / ".git" / "info" / "exclude").write_text("server/.venv\n")
    (workdir / "server" / "data").mkdir()
    (workdir / "config").mkdir(exist_ok=True)
    port = _free_port()
    (workdir / "config" / "sources.json").write_text(
        '{"allow_self_update": true, "update_repo": "testowner/testrepo", '
        f'"update_branch": "main", "bind_host": "127.0.0.1", "bind_port": {port}}}'
    )
    (workdir / "config" / "layout.json").write_text("{}")
    assert subprocess.run(["git", "status", "--porcelain"], cwd=workdir, capture_output=True, text=True,
                           check=True).stdout == ""

    # advance the remote by one commit -- workdir is now "one commit behind"
    (seed / "README.md").write_text("e2e marker\n")
    _run(["commit", "-qam", "v2"], seed)
    _run(["push", "-q", str(remote_dir), "HEAD:main"], seed)
    v2_head = _head(seed)
    assert v2_head != v1_head

    # PATH for the child server: symlinks to real nohup/curl/git ONLY --
    # shutil.which("systemctl") inside it is guaranteed to return None.
    safe_bin = tmp_path / "safe_bin"
    safe_bin.mkdir()
    for tool in ("nohup", "curl", "git"):
        found = shutil.which(tool)
        assert found, f"{tool} required on the host running this test"
        (safe_bin / tool).symlink_to(found)
    assert shutil.which("systemctl", path=str(safe_bin)) is None

    server_dir = workdir / "server"
    pidfile = server_dir / "data" / "critdash.pid"
    logfile = server_dir / "data" / "critdash.log"
    env = {
        "PATH": str(safe_bin),
        "CRITDASH_CONFIG_DIR": str(workdir / "config"),
        "HOME": os.environ.get("HOME", ""),
    }
    with logfile.open("wb") as log:
        proc = subprocess.Popen(
            [str(server_dir / ".venv" / "bin" / "uvicorn"), "critdash.main:app",
             "--app-dir", str(server_dir), "--host", "127.0.0.1", "--port", str(port)],
            cwd=server_dir, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        )
    pidfile.write_text(str(proc.pid))
    new_pid = None

    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        healthy = False
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base}/api/healthz", timeout=1).status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        assert healthy, f"server never answered healthz; log:\n{logfile.read_text()}"

        before = httpx.get(f"{base}/api/version", timeout=5).json()
        assert before["commit"] == v1_head

        resp = httpx.post(f"{base}/api/update/apply", timeout=15)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["applied"] is True
        assert body["commit"] == v2_head
        assert body["restart_requested"] is True
        assert body["restart_method"] == "self"
        assert body["restart_hint"] == ""

        # the OLD process must actually go away, a NEW one must come up,
        # and it must be serving the NEW commit -- not just "some process
        # exists at that pidfile".
        deadline = time.monotonic() + 20
        new_pid = None
        while time.monotonic() < deadline:
            try:
                candidate = int(pidfile.read_text().strip())
            except (OSError, ValueError):
                candidate = None
            if candidate and candidate != proc.pid:
                new_pid = candidate
                break
            time.sleep(0.2)
        assert new_pid is not None, "pidfile was never rewritten to a new PID"

        # `proc` is pytest's own subprocess.Popen child -- once the helper
        # kills it, it is a zombie (still "alive" to kill(pid, 0)) until
        # reaped here. wait() both reaps it and confirms it actually died;
        # it raises TimeoutExpired (failing this test) if it somehow didn't.
        proc.wait(timeout=10)

        deadline = time.monotonic() + 20
        after = None
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{base}/api/version", timeout=1)
                if r.status_code == 200:
                    after = r.json()
                    if after.get("commit") == v2_head:
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        assert after is not None, "new server never answered"
        assert after["commit"] == v2_head, f"new process is still serving the old commit: {after}"

        assert _head(workdir) == v2_head
    finally:
        for candidate_pid in {proc.pid, new_pid}:
            if not candidate_pid:
                continue
            try:
                os.kill(candidate_pid, 9)
            except OSError:
                pass
