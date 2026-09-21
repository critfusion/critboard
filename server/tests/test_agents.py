import json
import os
import time

import pytest

from critdash.collectors import agents as agents_mod
from critdash.collectors.agents import (
    AgentsCollector,
    agent_label,
    best_worktree_match,
    merge_agent_sources,
    parse_herdr_output,
    scan_session_agents,
    short_cwd,
)
from critdash.ctx import AppContext
from critdash.store import Store


def test_parse_herdr_output(fixtures_dir):
    text = (fixtures_dir / "herdr_agent_list.json").read_text()
    agents = parse_herdr_output(text)
    assert isinstance(agents, list)
    assert len(agents) >= 1
    a = agents[0]
    assert "agent" in a
    assert "agent_session" in a
    assert a["agent_session"]["value"]


def test_best_worktree_match_longest_prefix():
    worktrees = [
        {"path": "/home/user/work", "repo": "work", "branch": "n/a"},
        {"path": "/home/user/work/dashboard", "repo": "dashboard", "branch": "main"},
    ]
    match = best_worktree_match("/home/user/work/dashboard/server", worktrees)
    assert match["repo"] == "dashboard"


def test_best_worktree_match_none():
    assert best_worktree_match("/unrelated/path", [{"path": "/home/user/work"}]) is None


@pytest.mark.asyncio
async def test_collect_end_to_end(fixtures_dir, monkeypatch, tmp_path):
    # short_cwd()/agent_label() special-case the REAL home dir (os.path.
    # expanduser("~")), which varies by machine -- the fixture ships a
    # HOME_DIR placeholder for that reason, substituted here at load time
    # rather than hardcoding a path that only matches one developer's box.
    home = os.path.expanduser("~")
    herdr_text = (fixtures_dir / "herdr_agent_list.json").read_text().replace("HOME_DIR", home)

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return herdr_text.encode(), b""

        def kill(self):
            pass

        async def wait(self):
            pass

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(agents_mod.asyncio, "create_subprocess_exec", fake_exec)

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    collector = AgentsCollector(ctx=ctx, herdr_bin="herdr", store=store)
    result = await collector.collect()
    store.close()

    assert "agents" in result
    agents = result["agents"]
    raw = json.loads(herdr_text)["result"]["agents"]
    assert len(agents) == len(raw)
    for a in agents:
        for key in (
            "id", "kind", "status", "cwd", "cwd_short", "repo", "branch", "pane", "workspace",
            "title", "label", "focused", "session_id", "bead", "last_activity", "status_since",
            "tokens_today", "cost_today_usd", "msg_count_today", "subagents_active", "model", "source",
        ):
            assert key in a
        assert a["status"] in ("working", "idle", "done", "unknown")
        # session_projects_glob was never configured on this collector, so
        # every agent must come from herdr alone -- legacy behavior preserved.
        assert a["source"] == "herdr"

    # Fix 2: no worktree data in this fixture, so repo is null for all four --
    # the useless raw titles ("Claude Code" x2, "Logged in") must be replaced
    # by the cwd basename wherever cwd isn't just the home dir.
    by_cwd = {a["cwd"]: a for a in agents}
    assert by_cwd[home]["label"] == "Claude Code"  # cwd IS home -> falls back to title
    assert by_cwd[home]["cwd_short"] == "~"
    assert by_cwd["/srv/demo/ffw"]["label"] == "ffw"
    assert by_cwd["/srv/demo/ffw"]["cwd_short"] == "/srv/demo/ffw"  # unrelated path, left alone
    assert by_cwd[home + "/work/demo-app"]["label"] == "demo-app"
    assert by_cwd[home + "/work/demo-app"]["cwd_short"] == "~/work/demo-app"
    assert by_cwd[home + "/work/dashboard"]["label"] == "dashboard"
    assert by_cwd[home + "/work/dashboard"]["cwd_short"] == "~/work/dashboard"


# -- short_cwd / agent_label (Fix 2) -----------------------------------------


def test_short_cwd_collapses_home():
    home = agents_mod.os.path.expanduser("~")
    assert short_cwd(home) == "~"
    assert short_cwd(home + "/work/dashboard") == "~/work/dashboard"
    # only the real home dir is collapsed -- an unrelated absolute path
    # (e.g. a second repo root an install configures) is left as-is.
    assert short_cwd("/srv/demo") == "/srv/demo"
    assert short_cwd("/srv/demo/ffw") == "/srv/demo/ffw"


def test_short_cwd_leaves_unrelated_paths_and_none_alone():
    assert short_cwd("/opt/other") == "/opt/other"
    assert short_cwd(None) is None


def test_agent_label_prefers_repo_over_everything():
    assert agent_label(repo="dashboard", cwd="/home/user", title="Claude Code", kind="claude") == "dashboard"


def test_agent_label_falls_back_to_cwd_basename_when_not_home():
    assert agent_label(repo=None, cwd="/srv/demo/ffw", title="Logged in", kind="claude") == "ffw"


def test_agent_label_skips_basename_when_cwd_is_home():
    home = agents_mod.os.path.expanduser("~")
    assert agent_label(repo=None, cwd=home, title="Claude Code", kind="claude") == "Claude Code"


def test_agent_label_falls_back_to_kind_as_last_resort():
    home = agents_mod.os.path.expanduser("~")
    assert agent_label(repo=None, cwd=home, title=None, kind="claude") == "claude"


# -- scan_session_agents (Bug 2: session-derived agent discovery) -----------


def _write_session_jsonl(path, session_id, cwd, git_branch, model, mtime_age_s, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = list(extra_lines) + [json.dumps({
        "type": "assistant", "sessionId": session_id, "cwd": cwd, "gitBranch": git_branch,
        "timestamp": "2026-09-18T12:00:00Z", "message": {"model": model, "id": "msg_1"},
    })]
    path.write_text("\n".join(lines) + "\n")
    now = time.time()
    os.utime(path, (now - mtime_age_s, now - mtime_age_s))


def test_scan_session_agents_working_vs_idle_by_recency(tmp_path):
    working_dir = tmp_path / "-home-user-work-foo"
    idle_dir = tmp_path / "-home-user-work-bar"
    _write_session_jsonl(
        working_dir / "sess-working.jsonl", "sess-working", "/home/user/work/foo", "main",
        "claude-sonnet-5", mtime_age_s=10,
    )
    _write_session_jsonl(
        idle_dir / "sess-idle.jsonl", "sess-idle", "/home/user/work/bar", "feature-x",
        "claude-opus-5", mtime_age_s=500,
    )

    results = scan_session_agents(str(tmp_path / "*/*.jsonl"), window_s=900)
    by_id = {r["session_id"]: r for r in results}
    assert by_id["sess-working"]["status"] == "working"
    assert by_id["sess-working"]["cwd"] == "/home/user/work/foo"
    assert by_id["sess-working"]["git_branch"] == "main"
    assert by_id["sess-working"]["model"] == "claude-sonnet-5"
    assert by_id["sess-idle"]["status"] == "idle"
    assert by_id["sess-idle"]["git_branch"] == "feature-x"


def test_scan_session_agents_excludes_files_outside_window(tmp_path):
    _write_session_jsonl(
        tmp_path / "-home-user-work-old" / "sess-old.jsonl", "sess-old", "/home/user/work/old",
        "main", "claude-sonnet-5", mtime_age_s=3600,
    )
    results = scan_session_agents(str(tmp_path / "*/*.jsonl"), window_s=900)
    assert results == []


def test_scan_session_agents_reads_tail_not_whole_file(tmp_path):
    # A huge unparseable first "line" must not stop the scan from finding
    # the real record near the end -- proves the tail-only read still works
    # when the file is bigger than one read chunk.
    junk = "x" * 200_000
    _write_session_jsonl(
        tmp_path / "-home-user-work-big" / "sess-big.jsonl", "sess-big", "/home/user/work/big",
        "main", "claude-sonnet-5", mtime_age_s=5, extra_lines=[junk],
    )
    results = scan_session_agents(str(tmp_path / "*/*.jsonl"), window_s=900, tail_bytes=4096)
    assert len(results) == 1
    assert results[0]["session_id"] == "sess-big"


def test_scan_session_agents_finds_model_when_last_line_has_none(tmp_path):
    # Real-world shape seen on host-c: the chronologically last line in the
    # jsonl is a housekeeping "system" event with no message.model, a few
    # lines after the last real assistant turn. The scan must still surface
    # the model from that earlier assistant line, not report it as unknown.
    d = tmp_path / "-home-user2-demo-notes"
    d.mkdir(parents=True)
    path = d / "sess.jsonl"
    lines = [
        json.dumps({
            "type": "assistant", "sessionId": "sess1", "cwd": "/home/user2/demo-notes",
            "gitBranch": "main", "timestamp": "2026-09-18T13:26:35Z",
            "message": {"model": "claude-sonnet-5", "id": "msg_1"},
        }),
        json.dumps({
            "type": "system", "sessionId": "sess1", "cwd": "/home/user2/demo-notes",
            "gitBranch": "main", "timestamp": "2026-09-18T13:29:37Z",
        }),
    ]
    path.write_text("\n".join(lines) + "\n")
    now = time.time()
    os.utime(path, (now - 60, now - 60))

    results = scan_session_agents(str(tmp_path / "*/*.jsonl"), window_s=900)
    assert len(results) == 1
    assert results[0]["session_id"] == "sess1"
    assert results[0]["model"] == "claude-sonnet-5"
    assert results[0]["cwd"] == "/home/user2/demo-notes"


def test_scan_session_agents_finds_cwd_when_last_lines_have_explicit_null(tmp_path):
    # Real-world shape seen on this very machine: trailing "queue-operation"
    # events carry the sessionId but an EXPLICIT null cwd/gitBranch (not a
    # missing key) -- the scan must still surface the real cwd/branch from
    # an earlier line, not report them as unknown.
    d = tmp_path / "-home-user-work-dashboard"
    d.mkdir(parents=True)
    path = d / "sess.jsonl"
    lines = [
        json.dumps({
            "type": "assistant", "sessionId": "sess1", "cwd": "/home/user/work/dashboard",
            "gitBranch": "main", "timestamp": "2026-09-18T19:20:00Z",
            "message": {"model": "claude-sonnet-5", "id": "msg_1"},
        }),
        json.dumps({
            "type": "queue-operation", "sessionId": "sess1", "cwd": None, "gitBranch": None,
            "timestamp": "2026-09-18T19:23:53Z",
        }),
    ]
    path.write_text("\n".join(lines) + "\n")
    now = time.time()
    os.utime(path, (now - 30, now - 30))

    results = scan_session_agents(str(tmp_path / "*/*.jsonl"), window_s=900)
    assert len(results) == 1
    assert results[0]["cwd"] == "/home/user/work/dashboard"
    assert results[0]["git_branch"] == "main"
    assert results[0]["model"] == "claude-sonnet-5"


def test_scan_session_agents_never_invents_done_status(tmp_path):
    _write_session_jsonl(
        tmp_path / "-home-user-work-foo" / "sess1.jsonl", "sess1", "/home/user/work/foo",
        "main", "claude-sonnet-5", mtime_age_s=890,  # inside the window, well past working
    )
    results = scan_session_agents(str(tmp_path / "*/*.jsonl"), window_s=900)
    assert results[0]["status"] in ("working", "idle")


# -- merge_agent_sources (Bug 2: merge-by-session-id, no duplicates) --------


def _herdr_entry(session_id, **overrides):
    base = {
        "id": session_id, "kind": "claude", "status": "working", "cwd": "/herdr/cwd",
        "cwd_short": "/herdr/cwd", "repo": None, "branch": None, "pane": "pane-1",
        "workspace": "ws-1", "title": "herdr title", "label": "herdr-label", "focused": True,
        "session_id": session_id, "bead": None, "last_activity": None,
        "tokens_today": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0},
        "cost_today_usd": 0.0, "msg_count_today": 0, "subagents_active": 0, "model": None,
        "host": "localhost", "stale": False, "source": "herdr",
        "_status_key": session_id, "_event_desc": "claude working (herdr title)",
    }
    base.update(overrides)
    return base


def _session_entry(session_id, **overrides):
    base = {
        "id": session_id, "kind": None, "status": "idle", "cwd": "/jsonl/cwd",
        "cwd_short": "/jsonl/cwd", "repo": None, "branch": "main", "pane": None,
        "workspace": None, "title": None, "label": "cwd", "focused": False,
        "session_id": session_id, "bead": None, "last_activity": "2026-09-18T12:00:00Z",
        "tokens_today": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0},
        "cost_today_usd": 0.0, "msg_count_today": 0, "subagents_active": 0,
        "model": "claude-sonnet-5", "host": "localhost", "stale": False, "source": "session",
        "_status_key": session_id, "_event_desc": "session idle (/jsonl/cwd)",
    }
    base.update(overrides)
    return base


def test_merge_produces_one_entry_for_session_known_to_both_sources():
    herdr = [_herdr_entry("s1")]
    session = [_session_entry("s1")]
    merged = merge_agent_sources(herdr, session)
    assert len(merged) == 1
    a = merged[0]
    assert a["source"] == "both"
    # cwd/branch/model come from the jsonl (session), not herdr
    assert a["cwd"] == "/jsonl/cwd"
    assert a["branch"] == "main"
    assert a["model"] == "claude-sonnet-5"
    # pane/workspace/title/status/focused/kind come from herdr
    assert a["pane"] == "pane-1"
    assert a["workspace"] == "ws-1"
    assert a["title"] == "herdr title"
    assert a["status"] == "working"
    assert a["focused"] is True
    assert a["kind"] == "claude"


def test_merge_keeps_herdr_only_and_session_only_separate():
    herdr = [_herdr_entry("herdr-only")]
    session = [_session_entry("session-only")]
    merged = merge_agent_sources(herdr, session)
    by_id = {a["session_id"]: a for a in merged}
    assert len(merged) == 2
    assert by_id["herdr-only"]["source"] == "herdr"
    assert by_id["session-only"]["source"] == "session"


def test_merge_never_duplicates_a_shared_session():
    herdr = [_herdr_entry("dup")]
    session = [_session_entry("dup")]
    merged = merge_agent_sources(herdr, session)
    ids = [a["session_id"] for a in merged]
    assert ids.count("dup") == 1


# -- AgentsCollector end-to-end with session-derived detection --------------


def _fake_herdr_exec(monkeypatch, agents_json):
    class FakeProc:
        returncode = 0

        async def communicate(self):
            return json.dumps({"id": "x", "result": {"agents": agents_json}}).encode(), b""

        def kill(self):
            pass

        async def wait(self):
            pass

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(agents_mod.asyncio, "create_subprocess_exec", fake_exec)


@pytest.mark.asyncio
async def test_collect_surfaces_session_only_agent_invisible_to_herdr(monkeypatch, tmp_path):
    # Reproduces Bug 2: herdr returns no agents (as on host-c), but a real
    # session's jsonl is being written to -- it must still show up.
    _fake_herdr_exec(monkeypatch, [])
    _write_session_jsonl(
        tmp_path / "-home-user2-demo-notes" / "39eb414c.jsonl", "39eb414c-session",
        "/home/user2/demo-notes", "main", "claude-sonnet-5", mtime_age_s=30,
    )

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    collector = AgentsCollector(
        ctx=ctx, herdr_bin="herdr", store=store, host="host-c",
        session_projects_glob=str(tmp_path / "*/*.jsonl"), session_active_window_s=900,
    )
    result = await collector.collect()
    store.close()

    agents = result["agents"]
    assert len(agents) == 1
    a = agents[0]
    assert a["source"] == "session"
    assert a["session_id"] == "39eb414c-session"
    assert a["cwd"] == "/home/user2/demo-notes"
    assert a["model"] == "claude-sonnet-5"
    assert a["status"] == "working"
    assert a["host"] == "host-c"
    # regression: a session found by scanning claude_projects_dir is known to
    # be produced by Claude Code even when herdr has never heard of it.
    assert a["kind"] == "claude"


@pytest.mark.asyncio
async def test_collect_merges_herdr_and_session_for_same_session_without_duplicating(monkeypatch, tmp_path):
    session_id = "shared-session"
    _fake_herdr_exec(monkeypatch, [{
        "agent": "claude", "agent_status": "working", "cwd": "/home/user/work/dashboard",
        "pane_id": "pane-9", "workspace_id": "ws-9", "terminal_title_stripped": "dashboard",
        "focused": True, "agent_session": {"value": session_id},
    }])
    _write_session_jsonl(
        tmp_path / "-home-user-work-dashboard" / f"{session_id}.jsonl", session_id,
        "/home/user/work/dashboard", "main", "claude-sonnet-5", mtime_age_s=10,
    )

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    collector = AgentsCollector(
        ctx=ctx, herdr_bin="herdr", store=store, host="localhost",
        session_projects_glob=str(tmp_path / "*/*.jsonl"), session_active_window_s=900,
    )
    result = await collector.collect()
    store.close()

    agents = result["agents"]
    matching = [a for a in agents if a["session_id"] == session_id]
    assert len(matching) == 1
    assert matching[0]["source"] == "both"
    assert matching[0]["pane"] == "pane-9"


@pytest.mark.asyncio
async def test_collect_keeps_herdr_kind_when_it_differs_from_claude(monkeypatch, tmp_path):
    # A session-only build defaults kind to "claude" (it was found by scanning
    # a Claude Code session log), but herdr, when it DOES know about the
    # session, stays authoritative -- it may legitimately report a different
    # kind (e.g. this session was actually driven through a codex wrapper).
    session_id = "shared-session-codex"
    _fake_herdr_exec(monkeypatch, [{
        "agent": "codex", "agent_status": "working", "cwd": "/home/user/work/dashboard",
        "pane_id": "pane-1", "workspace_id": "ws-1", "terminal_title_stripped": "dashboard",
        "focused": True, "agent_session": {"value": session_id},
    }])
    _write_session_jsonl(
        tmp_path / "-home-user-work-dashboard" / f"{session_id}.jsonl", session_id,
        "/home/user/work/dashboard", "main", "claude-sonnet-5", mtime_age_s=10,
    )

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    collector = AgentsCollector(
        ctx=ctx, herdr_bin="herdr", store=store, host="localhost",
        session_projects_glob=str(tmp_path / "*/*.jsonl"), session_active_window_s=900,
    )
    result = await collector.collect()
    store.close()

    matching = [a for a in result["agents"] if a["session_id"] == session_id]
    assert len(matching) == 1
    assert matching[0]["source"] == "both"
    assert matching[0]["kind"] == "codex"


# -- Kimi (second provider, briefing 2026-09-18) merged in via ctx -----------


@pytest.mark.asyncio
async def test_collect_folds_in_kimi_agents_from_ctx(monkeypatch, tmp_path):
    _fake_herdr_exec(monkeypatch, [])
    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    ctx.latest_kimi_agents = [{
        "id": "session_kimi1", "kind": "kimi", "status": "working", "cwd": "/srv/demo/repos/testrepo",
        "cwd_short": "/srv/demo/repos/testrepo", "repo": None, "branch": None, "pane": None,
        "workspace": None, "title": None, "label": "testrepo", "focused": False,
        "session_id": "session_kimi1", "bead": None, "last_activity": "2026-09-18T12:00:00Z",
        "tokens_today": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 208},
        "cost_today_usd": None, "msg_count_today": 1, "subagents_active": 0,
        "model": "kimi-code/kimi-for-coding", "host": "localhost", "stale": False, "source": "session",
        "_status_key": "session_kimi1", "_event_desc": "kimi session working",
    }]

    collector = AgentsCollector(ctx=ctx, herdr_bin="herdr", store=store, host="localhost")
    result = await collector.collect()
    store.close()

    kimi_agents = [a for a in result["agents"] if a["kind"] == "kimi"]
    assert len(kimi_agents) == 1
    assert kimi_agents[0]["session_id"] == "session_kimi1"
    assert kimi_agents[0]["cost_today_usd"] is None


@pytest.mark.asyncio
async def test_collect_merges_herdr_and_kimi_for_same_session_without_duplicating(monkeypatch, tmp_path):
    session_id = "session_kimi_shared"
    _fake_herdr_exec(monkeypatch, [{
        "agent": "kimi", "agent_status": "working", "cwd": "/srv/demo/repos/testrepo",
        "pane_id": "pane-5", "workspace_id": "ws-5", "terminal_title_stripped": "testrepo",
        "focused": True, "agent_session": {"value": session_id},
    }])
    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    ctx.latest_kimi_agents = [{
        "id": session_id, "kind": "kimi", "status": "idle", "cwd": "/srv/demo/repos/testrepo",
        "cwd_short": "/srv/demo/repos/testrepo", "repo": None, "branch": None, "pane": None,
        "workspace": None, "title": None, "label": "testrepo", "focused": False,
        "session_id": session_id, "bead": None, "last_activity": "2026-09-18T12:00:00Z",
        "tokens_today": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 208},
        "cost_today_usd": None, "msg_count_today": 1, "subagents_active": 0,
        "model": "kimi-code/kimi-for-coding", "host": "localhost", "stale": False, "source": "session",
        "_status_key": session_id, "_event_desc": "kimi session idle",
    }]

    collector = AgentsCollector(ctx=ctx, herdr_bin="herdr", store=store, host="localhost")
    result = await collector.collect()
    store.close()

    matching = [a for a in result["agents"] if a["session_id"] == session_id]
    assert len(matching) == 1
    assert matching[0]["source"] == "both"
    assert matching[0]["pane"] == "pane-5"
