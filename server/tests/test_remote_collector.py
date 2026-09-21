import json

import pytest

from critdash.collectors import remote as remote_mod
from critdash.collectors.agents import AgentsCollector
from critdash.collectors.remote import RemoteCollector
from critdash.collectors.worktrees import WorktreesCollector
from critdash.ctx import AppContext
from critdash.store import Store


def _probe_payload(host, agents=None, worktrees=None, usage_buckets=None, **analytics_buckets):
    return {
        "host": host, "generated_at": "2026-09-18T12:00:00Z",
        "agents": agents or [], "worktrees": worktrees or [],
        "system": {"load1": 0.1}, "usage_buckets": usage_buckets or [],
        **analytics_buckets,
    }


class FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self, input=None):  # noqa: A002
        if self._hang:
            import asyncio
            await asyncio.sleep(999)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return None


def _target_from_cmd(args) -> str:
    # cmd = ["ssh", *ssh_opts, target, remote_cmd_string]
    return args[-2]


@pytest.fixture
def ctx(tmp_path):
    store = Store(tmp_path / "t.db")
    c = AppContext(config=None, store=store, pricing={"models": {"default": {
        "input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write_5m": 1.0, "cache_write_1h": 1.0,
    }}})
    yield c
    store.close()


@pytest.mark.asyncio
async def test_healthy_host_marked_ok_and_stashed_on_ctx(ctx, monkeypatch):
    payload = _probe_payload("host-b", worktrees=[{"path": "/x", "repo": "x"}])

    async def fake_exec(*args, **kwargs):
        target = _target_from_cmd(args)
        assert target == "host-b-target"
        return FakeProc(stdout=(json.dumps(payload) + "\n").encode())

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)

    collector = RemoteCollector(
        ctx=ctx, store=ctx.store,
        hosts=[{"name": "host-b", "mode": "ssh", "target": "host-b-target", "enabled": True}],
    )
    result = await collector.collect()
    hosts = result["hosts"]
    assert len(hosts) == 1
    h = hosts[0]
    assert h["ok"] is True
    assert h["name"] == "host-b"
    assert h["worktrees"] == 1
    assert ctx.remote_hosts["host-b"]["ok"] is True
    assert ctx.remote_hosts["host-b"]["worktrees"][0]["repo"] == "x"


@pytest.mark.asyncio
async def test_failing_host_isolated_to_ok_false_with_real_error(ctx, monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(stderr=b"user@host-c: Permission denied (publickey).\n", returncode=255)

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)

    collector = RemoteCollector(
        ctx=ctx, store=ctx.store,
        hosts=[{"name": "host-c", "mode": "ssh", "target": "host-c", "enabled": True}],
    )
    result = await collector.collect()
    h = result["hosts"][0]
    assert h["ok"] is False
    assert "Permission denied" in h["error"]
    assert h["reachable"] is False
    assert h["reason_code"] == "command_failed"
    assert h["detail"] == h["error"]
    assert h["optional"] is False


@pytest.mark.asyncio
async def test_one_failing_host_never_blocks_a_healthy_one(ctx, monkeypatch):
    good_payload = _probe_payload("host-b", worktrees=[{"path": "/x", "repo": "x"}])

    async def fake_exec(*args, **kwargs):
        target = _target_from_cmd(args)
        if target == "host-c":
            return FakeProc(stderr=b"Permission denied (publickey).\n", returncode=255)
        return FakeProc(stdout=(json.dumps(good_payload) + "\n").encode())

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)

    collector = RemoteCollector(
        ctx=ctx, store=ctx.store,
        hosts=[
            {"name": "host-b", "mode": "ssh", "target": "host-b", "enabled": True},
            {"name": "host-c", "mode": "ssh", "target": "host-c", "enabled": True},
        ],
    )
    result = await collector.collect()
    by_name = {h["name"]: h for h in result["hosts"]}
    assert by_name["host-b"]["ok"] is True
    assert by_name["host-c"]["ok"] is False


@pytest.mark.asyncio
async def test_ssh_timeout_marks_host_down_without_raising(ctx, monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(hang=True)

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)

    collector = RemoteCollector(
        ctx=ctx, store=ctx.store, ssh_timeout_s=0.05,
        hosts=[{"name": "slow", "mode": "ssh", "target": "slow", "enabled": True}],
    )
    result = await collector.collect()
    h = result["hosts"][0]
    assert h["ok"] is False
    assert "timed out" in h["error"]
    assert h["reason_code"] == "unreachable"
    assert h["remedy"] is not None


@pytest.mark.asyncio
async def test_ssh_binary_missing_marks_host_dependency_missing_and_optional(ctx, monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'ssh'")

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)

    collector = RemoteCollector(
        ctx=ctx, store=ctx.store,
        hosts=[{"name": "host-d", "mode": "ssh", "target": "host-d", "enabled": True}],
    )
    result = await collector.collect()
    h = result["hosts"][0]
    assert h["ok"] is False
    assert h["reason_code"] == "dependency_missing"
    assert h["optional"] is True
    assert h["remedy"] is not None


@pytest.mark.asyncio
async def test_stale_but_present_keeps_last_good_data_on_next_failure(ctx, monkeypatch):
    good_payload = _probe_payload("host-b", worktrees=[{"path": "/x", "repo": "x"}])
    call_count = {"n": 0}

    async def fake_exec(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakeProc(stdout=(json.dumps(good_payload) + "\n").encode())
        return FakeProc(stderr=b"connection refused\n", returncode=255)

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)

    collector = RemoteCollector(
        ctx=ctx, store=ctx.store,
        hosts=[{"name": "host-b", "mode": "ssh", "target": "host-b", "enabled": True}],
    )
    first = await collector.collect()
    assert first["hosts"][0]["ok"] is True

    second = await collector.collect()
    h = second["hosts"][0]
    assert h["ok"] is False
    assert h["worktrees"] == 1  # last-known count preserved, not zeroed
    assert ctx.remote_hosts["host-b"]["worktrees"][0]["repo"] == "x"  # data itself preserved


@pytest.mark.asyncio
async def test_local_host_entry_present_and_ok(ctx):
    collector = RemoteCollector(
        ctx=ctx, store=ctx.store, local_host="localhost",
        hosts=[{"name": "localhost", "mode": "local", "enabled": True}],
    )
    result = await collector.collect()
    assert len(result["hosts"]) == 1
    h = result["hosts"][0]
    assert h["mode"] == "local"
    assert h["ok"] is True


@pytest.mark.asyncio
async def test_bad_probe_output_marks_host_down(ctx, monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(stdout=b"not json at all\n")

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)
    collector = RemoteCollector(
        ctx=ctx, store=ctx.store,
        hosts=[{"name": "weird", "mode": "ssh", "target": "weird", "enabled": True}],
    )
    result = await collector.collect()
    assert result["hosts"][0]["ok"] is False
    assert "bad probe output" in result["hosts"][0]["error"]


# -- Bug 1 regression: per-host claude_projects_dir must reach the remote ----
# -- shell UNEXPANDED, so remote_probe.py's own os.path.expanduser() resolves
# -- "~" against the REMOTE host's home, not the local host's. Pre-fix, main.py
# -- expanded it locally (config.expand()) before handing it to
# -- RemoteCollector, so every host got the local host's absolute
# -- /home/user/.claude/projects -- which only happens to exist on host-b
# -- (same username as the local host) and silently globs to nothing, with no
# -- error, for any host with a different username (host-c: a different user).


def test_probe_args_ships_projects_dir_unexpanded_by_default():
    collector = RemoteCollector(claude_projects_dir="~/.claude/projects")
    args = collector._probe_args({"name": "host-b", "mode": "ssh", "target": "host-b"})
    i = args.index("--projects-glob")
    assert args[i + 1] == "~/.claude/projects/*/*.jsonl"  # NOT locally expanded


def test_probe_args_per_host_claude_projects_dir_override():
    collector = RemoteCollector(claude_projects_dir="~/.claude/projects")
    args = collector._probe_args({
        "name": "host-c", "mode": "ssh", "target": "host-c",
        "claude_projects_dir": "~/.claude/projects",
    })
    i = args.index("--projects-glob")
    assert args[i + 1] == "~/.claude/projects/*/*.jsonl"


def test_probe_args_per_host_repo_roots_override_falls_back_when_absent():
    collector = RemoteCollector(repo_roots=["/home/user/repos"])
    prod1_args = collector._probe_args({
        "name": "host-c", "mode": "ssh", "target": "host-c",
        "repo_roots": ["/opt", "/var/www", "/home/user2"],
    })
    host_b_args = collector._probe_args({"name": "host-b", "mode": "ssh", "target": "host-b"})

    def _roots(args):
        return [args[i + 1] for i, a in enumerate(args) if a == "--repo-root"]

    assert _roots(prod1_args) == ["/opt", "/var/www", "/home/user2"]
    assert _roots(host_b_args) == ["/home/user/repos"]  # no override -> global fallback


def test_probe_args_herdr_bin_null_ships_no_herdr_not_global_fallback():
    collector = RemoteCollector(herdr_bin="/home/user/.local/bin/herdr")
    args = collector._probe_args({
        "name": "host-d", "mode": "ssh", "target": "host-d", "herdr_bin": None,
    })
    i = args.index("--herdr-bin")
    assert args[i + 1] == ""  # never the global /home/user/.local/bin/herdr


def test_probe_args_herdr_bin_absent_falls_back_to_global():
    collector = RemoteCollector(herdr_bin="/home/user/.local/bin/herdr")
    args = collector._probe_args({"name": "host-b", "mode": "ssh", "target": "host-b"})
    i = args.index("--herdr-bin")
    assert args[i + 1] == "/home/user/.local/bin/herdr"


def test_probe_args_session_window_ships_global_default():
    collector = RemoteCollector(session_active_window_s=900.0)
    args = collector._probe_args({"name": "host-b", "mode": "ssh", "target": "host-b"})
    i = args.index("--session-window")
    assert args[i + 1] == "900.0"


def test_probe_args_session_window_per_host_override():
    collector = RemoteCollector(session_active_window_s=900.0)
    args = collector._probe_args({
        "name": "host-c", "mode": "ssh", "target": "host-c", "session_active_window_s": 1800,
    })
    i = args.index("--session-window")
    assert args[i + 1] == "1800"


def test_probe_args_kimi_dir_ships_global_default_unexpanded():
    collector = RemoteCollector(kimi_dir="~/.kimi-code")
    args = collector._probe_args({"name": "host-b", "mode": "ssh", "target": "host-b"})
    i = args.index("--kimi-dir")
    assert args[i + 1] == "~/.kimi-code"  # NOT locally expanded, same rule as claude_projects_dir


def test_probe_args_kimi_dir_per_host_override():
    collector = RemoteCollector(kimi_dir="~/.kimi-code")
    args = collector._probe_args({
        "name": "host-c", "mode": "ssh", "target": "host-c", "kimi_dir": "/opt/kimi-code",
    })
    i = args.index("--kimi-dir")
    assert args[i + 1] == "/opt/kimi-code"


@pytest.mark.asyncio
async def test_kimi_buckets_from_probe_payload_are_persisted(ctx, monkeypatch):
    payload = _probe_payload(
        "host-b", agents=[{
            "id": "session_kimi1", "kind": "kimi", "status": "working", "cwd": "/x",
            "session_id": "session_kimi1", "cost_today_usd": None, "source": "session",
        }],
        kimi_usage_buckets=[{"day": "2026-09-18", "model": "kimi-for-coding", "tokens": 208, "turns": 1}],
        kimi_error_buckets=[{"day": "2026-09-18", "kind": "quota_exceeded", "count": 1,
                              "last_seen": "2026-09-18T23:20:59.291Z"}],
    )

    async def fake_exec(*args, **kwargs):
        return FakeProc(stdout=(json.dumps(payload) + "\n").encode())

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)

    collector = RemoteCollector(
        ctx=ctx, store=ctx.store,
        hosts=[{"name": "host-b", "mode": "ssh", "target": "host-b-target", "enabled": True}],
    )
    await collector.collect()

    kimi_totals = ctx.store.remote_kimi_usage_totals()
    assert kimi_totals["tokens"] == 208
    kimi_errors = ctx.store.remote_kimi_error_counts("2000-01-01")
    assert kimi_errors[0]["kind"] == "quota_exceeded"
    assert kimi_errors[0]["count"] == 1
    # the kimi-kind agent from a REMOTE host rides through the same "agents"
    # key every other remote agent uses -- no separate merge path needed.
    kimi_agents = [a for a in ctx.remote_hosts["host-b"]["agents"] if a["kind"] == "kimi"]
    assert len(kimi_agents) == 1
    assert kimi_agents[0]["cost_today_usd"] is None


@pytest.mark.asyncio
async def test_host_with_only_historical_usage_still_persists_and_is_ok(ctx, monkeypatch):
    """Bug 1 regression, at the RemoteCollector/store level: a host whose
    entire usage history is historical (no bucket for the current hour, so
    tokens_today is legitimately 0) must still land its buckets in
    remote_usage_buckets and be reported ok:true -- zero-today is real data,
    not a reason to drop the host's contribution to non-today rollup windows.
    """
    old_hour_payload = _probe_payload(
        "host-c",
        usage_buckets=[
            {"hour": "2026-06-01T09", "model": "claude-sonnet-4-6", "project": "appstore",
             "input": 5, "output": 100, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0,
             "messages": 3},
        ],
    )

    async def fake_exec(*args, **kwargs):
        return FakeProc(stdout=(json.dumps(old_hour_payload) + "\n").encode())

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)
    collector = RemoteCollector(
        ctx=ctx, store=ctx.store,
        hosts=[{"name": "host-c", "mode": "ssh", "target": "host-c", "enabled": True}],
    )
    result = await collector.collect()
    h = result["hosts"][0]
    assert h["ok"] is True
    assert h["tokens_today"] == 0  # correct: no bucket for today

    all_time_rows = ctx.store.remote_usage_grouped(("host",), host="host-c")
    assert len(all_time_rows) == 1
    assert all_time_rows[0]["output"] == 100  # the historical bucket did land in the store


@pytest.mark.asyncio
async def test_usage_buckets_persisted_to_store(ctx, monkeypatch):
    payload = _probe_payload("host-b", usage_buckets=[
        {"hour": "2026-09-18T12", "model": "claude-opus-5", "project": "demo",
         "input": 10, "output": 20, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0, "messages": 1},
    ])

    async def fake_exec(*args, **kwargs):
        return FakeProc(stdout=(json.dumps(payload) + "\n").encode())

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)
    collector = RemoteCollector(
        ctx=ctx, store=ctx.store,
        hosts=[{"name": "host-b", "mode": "ssh", "target": "host-b", "enabled": True}],
    )
    await collector.collect()
    rows = ctx.store.remote_usage_grouped(("host", "model"), host="host-b")
    assert len(rows) == 1
    assert rows[0]["output"] == 20


@pytest.mark.asyncio
async def test_wave2_analytics_buckets_persisted_to_store(ctx, monkeypatch):
    payload = _probe_payload(
        "host-b",
        tool_buckets=[{"day": "2026-09-18", "tool": "Edit", "calls": 5, "errors": 2}],
        error_buckets=[{"day": "2026-09-18", "kind": "file_not_found", "tool": "Edit",
                         "count": 2, "last_seen": "2026-09-18T12:00:00Z"}],
        error_examples=[
            {"kind": "file_not_found", "example": "no such file", "last_seen": "2026-09-18T12:00:00Z"}
        ],
        trouble_file_buckets=[{"day": "2026-09-18", "path": "a.py", "tool": "Edit", "errors": 2}],
        api_error_buckets=[
            {"day": "2026-09-18", "kind": "rate_limit", "count": 1, "last_seen": "2026-09-18T12:00:00Z"}
        ],
        session_buckets=[{"day": "2026-09-18", "session_id": "s1", "is_sidechain": 1,
                           "model": "claude-sonnet-5", "project": "demo", "input": 1, "output": 1,
                           "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0, "messages": 1}],
        productivity_buckets=[{"day": "2026-09-18", "repo": "demo", "commits": 3,
                                "lines_added": 10, "lines_removed": 2, "files_changed": 4}],
    )

    async def fake_exec(*args, **kwargs):
        return FakeProc(stdout=(json.dumps(payload) + "\n").encode())

    monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", fake_exec)
    collector = RemoteCollector(
        ctx=ctx, store=ctx.store,
        hosts=[{"name": "host-b", "mode": "ssh", "target": "host-b", "enabled": True}],
    )
    await collector.collect()

    assert ctx.store.remote_tool_by_tool("2026-01-01")[0]["calls"] == 5
    assert ctx.store.remote_error_top_kinds("2026-01-01")[0]["count"] == 2
    assert ctx.store.remote_error_example("file_not_found")["example"] == "no such file"
    assert ctx.store.remote_trouble_files("2026-01-01")[0]["errors"] == 2
    assert ctx.store.remote_api_errors("2026-01-01")[0]["count"] == 1
    session_rows = ctx.store.remote_session_buckets_grouped("2026-01-01")
    assert session_rows[0]["is_sidechain"] == 1
    assert session_rows[0]["project"] == "demo"
    assert ctx.store.remote_productivity_grouped("2026-01-01")[0]["commits"] == 3


# -- fleet merge into AgentsCollector / WorktreesCollector -------------------


@pytest.mark.asyncio
async def test_agents_collector_merges_remote_agents_with_stale_flag(ctx, monkeypatch):
    async def fake_exec(*a, **k):
        class P:
            returncode = 0

            async def communicate(self):
                return json.dumps({"id": "x", "result": {"agents": []}}).encode(), b""

            def kill(self):
                pass

            async def wait(self):
                pass
        return P()

    from critdash.collectors import agents as agents_mod
    monkeypatch.setattr(agents_mod.asyncio, "create_subprocess_exec", fake_exec)

    ctx.remote_hosts["host-b"] = {
        "ok": True, "error": None,
        "agents": [{"id": "remote-1", "host": "host-b", "cwd": "/home/user/repos/foo"}],
        "worktrees": [], "system": {},
    }
    collector = AgentsCollector(ctx=ctx, herdr_bin="herdr", store=ctx.store, host="localhost")
    result = await collector.collect()
    remote_entries = [a for a in result["agents"] if a["host"] == "host-b"]
    assert len(remote_entries) == 1
    assert remote_entries[0]["stale"] is False

    ctx.remote_hosts["host-b"]["ok"] = False
    result2 = await collector.collect()
    remote_entries2 = [a for a in result2["agents"] if a["host"] == "host-b"]
    assert remote_entries2[0]["stale"] is True


@pytest.mark.asyncio
async def test_worktrees_collector_merges_remote_worktrees_and_does_not_cross_join_agents(ctx, tmp_path):
    (tmp_path / ".git").mkdir()
    ctx.remote_hosts["host-b"] = {
        "ok": True, "error": None,
        "worktrees": [{"path": str(tmp_path), "repo": "collision", "host": "host-b", "agents": []}],
        "agents": [], "system": {},
    }
    # a LOCAL agent whose cwd happens to share the exact same path string as
    # the remote worktree above -- must NOT be attributed to the remote entry
    ctx.latest_agents = [{"id": "local-agent", "host": "localhost", "cwd": str(tmp_path)}]

    collector = WorktreesCollector(
        ctx=ctx, repo_roots=[str(tmp_path.parent)], store=ctx.store, host="localhost"
    )
    result = await collector.collect()
    by_host = {}
    for w in result["worktrees"]:
        by_host.setdefault(w["host"], []).append(w)

    local_wt = [w for w in by_host.get("localhost", []) if w["path"] == str(tmp_path)]
    remote_wt = by_host["host-b"][0]
    assert remote_wt["agents"] == []  # remote worktree's own (empty) join, untouched by local agent
    if local_wt:
        assert "local-agent" in local_wt[0]["agents"]


# -- availability_issue (Bug 2: auto-disable when no ssh-mode host is configured)


def test_availability_issue_none_when_ssh_host_configured():
    hosts = [
        {"name": "localhost", "mode": "local", "enabled": True},
        {"name": "box", "mode": "ssh", "enabled": True},
    ]
    assert remote_mod.availability_issue(hosts) is None


def test_availability_issue_config_missing_when_only_local_host():
    hosts = [{"name": "localhost", "mode": "local", "enabled": True}]
    issue = remote_mod.availability_issue(hosts)
    assert issue is not None
    assert issue.reason_code == "config_missing"
    assert issue.optional is True


def test_availability_issue_config_missing_when_no_hosts_at_all():
    assert remote_mod.availability_issue([]) is not None
    assert remote_mod.availability_issue(None) is not None


def test_availability_issue_ignores_disabled_ssh_host():
    hosts = [{"name": "box", "mode": "ssh", "enabled": False}]
    issue = remote_mod.availability_issue(hosts)
    assert issue is not None
    assert issue.reason_code == "config_missing"
