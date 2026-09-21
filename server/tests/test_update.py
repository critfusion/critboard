"""critdash/update.py: git identity, the GitHub "update available" check, and
the gated git pull --ff-only + reinstall + restart apply path.

apply_update() end-to-end tests use real local git repos (same pattern as
test_productivity.py's _init_repo) with a "remote" whose local filesystem
path is deliberately shaped .../github.com/<owner>/<repo>(.git) so
_refuse_if_wrong_origin's real string check (never mocked) passes against a
same-machine remote -- git treats a local path remote exactly like any other.
_restart_service is monkeypatched in every test that reaches it, so no test
ever calls the real `systemctl` and risks touching a real running service.
"""

from __future__ import annotations

import subprocess
import time

import httpx
import pytest

from critdash import update


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _Cfg:
    def __init__(self, **sources):
        self.sources = sources


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
        update.apply_update(_Cfg(allow_self_update=True), tmp_path)
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

    monkeypatch.setattr(update, "_restart_service", lambda unit="critdash.service": True)
    cfg = _Cfg(allow_self_update=True, update_repo="testowner/testrepo", update_branch="main")
    result = update.apply_update(cfg, clone_dir)

    assert result["applied"] is True
    assert result["commit"] == new_head
    assert result["reinstalled"] is False
    assert result["restart_requested"] is True
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

    monkeypatch.setattr(update, "_restart_service", lambda unit="critdash.service": False)
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

    monkeypatch.setattr(update, "_restart_service", lambda unit="critdash.service": True)
    cfg = _Cfg(allow_self_update=True, update_repo="testowner/testrepo", update_branch="main")
    with pytest.raises(update.UpdateError) as exc_info:
        update.apply_update(cfg, clone_dir)
    assert exc_info.value.reason == "not_fast_forward"
