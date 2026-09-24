import json
import os
import sqlite3
import time

import pytest

from critdash.collectors import CollectorIssue
from critdash.collectors import agents as agents_mod
from critdash.collectors.agents import (
    AgentsCollector,
    _apply_bead_cross_check,
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


# -- session-bead cross-check (_apply_bead_cross_check) ----------------------


def _tracked_agent(session_id, bead, claim_ts=1.0, last_activity=None, kind="claude"):
    """A bead_tracked agent with a single unreleased claim (or none, when
    `bead` is None) -- the shape AgentsCollector.collect() hands to
    _apply_bead_cross_check BEFORE it resolves "bead"/"bead_title"."""
    claims = [(bead, claim_ts)] if bead else []
    return {
        "id": session_id, "session_id": session_id, "kind": kind,
        "bead_tracked": True, "bead": None, "bead_title": None,
        "_bead_claims": claims, "last_activity": last_activity,
    }


def _tracked_agent_multi(session_id, claims, last_activity=None, kind="claude"):
    """A bead_tracked agent with several unreleased claims -- `claims` is
    the ordered (most-recent-first) [(bead_id, claim_ts), ...] list Defect 3
    requires resolve_session_bead to expose."""
    return {
        "id": session_id, "session_id": session_id, "kind": kind,
        "bead_tracked": True, "bead": None, "bead_title": None,
        "_bead_claims": list(claims), "last_activity": last_activity,
    }


def test_cross_check_null_when_bead_no_longer_in_progress():
    agents = [_tracked_agent("s1", "demo-a")]
    _apply_bead_cross_check(agents, {"demo-a": {"status": "closed", "title": "t"}})
    assert agents[0]["bead"] is None
    assert agents[0]["bead_title"] is None


def test_cross_check_keeps_bead_when_in_progress_and_sets_title():
    agents = [_tracked_agent("s1", "demo-a")]
    _apply_bead_cross_check(agents, {"demo-a": {"status": "in_progress", "title": "demo title"}})
    assert agents[0]["bead"] == "demo-a"
    assert agents[0]["bead_title"] == "demo title"


def test_cross_check_null_when_bead_unknown_to_beads_data():
    agents = [_tracked_agent("s1", "demo-a")]
    _apply_bead_cross_check(agents, {})
    assert agents[0]["bead"] is None


def test_cross_check_beads_collector_inactive_gives_null():
    agents = [_tracked_agent("s1", "demo-a")]
    _apply_bead_cross_check(agents, None)
    assert agents[0]["bead"] is None


def test_cross_check_untracked_kind_untouched_by_inactive_beads():
    agents = [{"id": "s1", "kind": "codex", "bead_tracked": False, "bead": None, "bead_title": None}]
    _apply_bead_cross_check(agents, None)
    assert agents[0]["bead"] is None
    assert agents[0]["bead_tracked"] is False


def test_cross_check_dedupe_most_recent_claim_wins():
    older = _tracked_agent("s1", "demo-a", claim_ts=100.0)
    newer = _tracked_agent("s2", "demo-a", claim_ts=200.0)
    agents = [older, newer]
    _apply_bead_cross_check(agents, {"demo-a": {"status": "in_progress", "title": "t"}})
    assert older["bead"] is None
    assert newer["bead"] == "demo-a"


def test_cross_check_dedupe_remote_uses_last_activity_fallback():
    # A remote agent carries no _bead_claim_ts (remote_probe.py returns
    # ONLY ids) -- last_activity is the recency fallback.
    remote = _tracked_agent("s1", "demo-a", claim_ts=None, last_activity="2026-01-01T00:00:00Z")
    local = _tracked_agent("s2", "demo-a", claim_ts=None, last_activity="2026-06-01T00:00:00Z")
    agents = [remote, local]
    _apply_bead_cross_check(agents, {"demo-a": {"status": "in_progress", "title": "t"}})
    assert remote["bead"] is None
    assert local["bead"] == "demo-a"


def test_cross_check_pops_internal_claim_ts():
    agents = [_tracked_agent("s1", "demo-a")]
    _apply_bead_cross_check(agents, {"demo-a": {"status": "in_progress", "title": "t"}})
    assert "_bead_claim_ts" not in agents[0]


def test_cross_check_pops_internal_claims_fields():
    agents = [_tracked_agent("s1", "demo-a")]
    _apply_bead_cross_check(agents, {"demo-a": {"status": "in_progress", "title": "t"}})
    assert "_bead_claims" not in agents[0]
    assert "_bead_candidates" not in agents[0]
    assert "_bead_idx" not in agents[0]


# -- Defect 3: selection = most recent UNRELEASED claim that is CURRENTLY
# in_progress, not just the most recent claim outright ----------------------


def test_cross_check_selects_older_in_progress_claim_over_newer_blocked_one():
    # Session claimed A (still in_progress), then later claimed B (now
    # blocked) -- the real incident this fix targets: the card must show A,
    # not go blank just because the MOST RECENT claim isn't in_progress.
    agent = _tracked_agent_multi("s1", [("demo-b", 200.0), ("demo-a", 100.0)])
    _apply_bead_cross_check(
        [agent],
        {"demo-a": {"status": "in_progress", "title": "a"}, "demo-b": {"status": "blocked", "title": "b"}},
    )
    assert agent["bead"] == "demo-a"
    assert agent["bead_title"] == "a"


def test_cross_check_selects_newest_when_both_in_progress():
    agent = _tracked_agent_multi("s1", [("demo-b", 200.0), ("demo-a", 100.0)])
    _apply_bead_cross_check(
        [agent],
        {
            "demo-a": {"status": "in_progress", "title": "a"},
            "demo-b": {"status": "in_progress", "title": "b"},
        },
    )
    assert agent["bead"] == "demo-b"


def test_cross_check_null_when_none_of_the_claims_are_in_progress():
    agent = _tracked_agent_multi("s1", [("demo-b", 200.0), ("demo-a", 100.0)])
    _apply_bead_cross_check(
        [agent],
        {"demo-a": {"status": "closed", "title": "a"}, "demo-b": {"status": "blocked", "title": "b"}},
    )
    assert agent["bead"] is None
    assert agent["bead_title"] is None


def test_cross_check_dedupe_loser_falls_through_to_its_own_next_in_progress_claim():
    # Both sessions' CURRENT top candidate is demo-a -- s2's claim is newer,
    # so s2 keeps demo-a. s1 does NOT just go null: it falls through to its
    # own next in_progress candidate, demo-c.
    s1 = _tracked_agent_multi("s1", [("demo-a", 100.0), ("demo-c", 50.0)])
    s2 = _tracked_agent_multi("s2", [("demo-a", 200.0)])
    _apply_bead_cross_check(
        [s1, s2],
        {
            "demo-a": {"status": "in_progress", "title": "a"},
            "demo-c": {"status": "in_progress", "title": "c"},
        },
    )
    assert s2["bead"] == "demo-a"
    assert s1["bead"] == "demo-c"


def test_cross_check_dedupe_chain_of_collisions_resolves():
    # s1's fallback (after losing demo-a to s2) collides with s3's only
    # candidate -- s3's claim is newer, so s1 falls through again to null.
    s1 = _tracked_agent_multi("s1", [("demo-a", 100.0), ("demo-b", 50.0)])
    s2 = _tracked_agent_multi("s2", [("demo-a", 200.0)])
    s3 = _tracked_agent_multi("s3", [("demo-b", 300.0)])
    _apply_bead_cross_check(
        [s1, s2, s3],
        {
            "demo-a": {"status": "in_progress", "title": "a"},
            "demo-b": {"status": "in_progress", "title": "b"},
        },
    )
    assert s2["bead"] == "demo-a"
    assert s3["bead"] == "demo-b"
    assert s1["bead"] is None


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
            "title", "label", "focused", "session_id", "bead", "bead_tracked", "bead_title",
            "last_activity", "status_since",
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


@pytest.mark.asyncio
async def test_collect_resolves_session_bead_end_to_end(monkeypatch, tmp_path):
    """A session-derived (no herdr) agent's bead comes from its OWN
    transcript, then survives (or not) the beads-data cross-check -- full
    AgentsCollector.collect() path, synthetic fixtures only."""

    async def fake_exec_no_herdr(*args, **kwargs):
        raise OSError("no herdr on this host")

    monkeypatch.setattr(agents_mod.asyncio, "create_subprocess_exec", fake_exec_no_herdr)

    projects_dir = tmp_path / "projects" / "-demo"
    projects_dir.mkdir(parents=True)
    session_path = projects_dir / "session-1.jsonl"
    line1 = json.dumps({
        "timestamp": "2026-09-23T00:00:00.000Z", "sessionId": "session-1", "cwd": "/srv/demo",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "bd update demo-a --claim", "description": "claim"}},
        ]},
    })
    line2 = json.dumps({
        "timestamp": "2026-09-23T00:00:01.000Z", "sessionId": "session-1",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": False,
             "content": "✓ Updated issue: demo-a — demo title"},
        ]},
    })
    session_path.write_text(line1 + "\n" + line2 + "\n")

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    ctx.latest_beads_by_id = {"demo-a": {"status": "in_progress", "title": "demo title"}}
    collector = AgentsCollector(
        ctx=ctx, herdr_bin="herdr", store=store,
        session_projects_glob=str(projects_dir / "*.jsonl"),
    )
    result = await collector.collect()
    store.close()

    agents = result["agents"]
    assert len(agents) == 1
    a = agents[0]
    assert a["kind"] == "claude"
    assert a["bead_tracked"] is True
    assert a["bead"] == "demo-a"
    assert a["bead_title"] == "demo title"

    # Now the SAME bead is no longer in_progress -- next poll must null it,
    # even though the transcript (and its cache) is unchanged.
    ctx.latest_beads_by_id = {"demo-a": {"status": "closed", "title": "demo title"}}
    result2 = await collector.collect()
    a2 = result2["agents"][0]
    assert a2["bead"] is None
    assert a2["bead_title"] is None


@pytest.mark.asyncio
async def test_collect_resolves_remote_bead_claims_list_end_to_end(monkeypatch, tmp_path):
    """Defect 3, remote path: remote_probe.py ships ONLY an ordered
    "bead_claims" id list (never a single "bead") -- AgentsCollector must
    rewrap that into this collector's own (bead_id, ts) candidate shape and
    run it through the exact same in_progress cross-check a local session
    gets."""

    async def fake_exec_no_herdr(*args, **kwargs):
        raise OSError("no herdr on this host")

    monkeypatch.setattr(agents_mod.asyncio, "create_subprocess_exec", fake_exec_no_herdr)

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    ctx.latest_beads_by_id = {
        "demo-b": {"status": "blocked", "title": "b"},
        "demo-a": {"status": "in_progress", "title": "a"},
    }
    ctx.remote_hosts = {
        "host2": {
            "ok": True,
            "agents": [{
                "id": "remote-session-1", "kind": "claude", "status": "working",
                "cwd": "/srv/demo", "repo": None, "branch": None, "pane": None,
                "workspace": None, "title": None, "label": "demo", "focused": False,
                "session_id": "remote-session-1",
                # newest claim first -- demo-b is blocked, demo-a is in_progress.
                "bead_claims": ["demo-b", "demo-a"],
                "bead_tracked": True, "last_activity": "2026-09-23T00:00:00Z",
                "status_since": None,
                "tokens_today": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0},
                "cost_today_usd": 0.0, "msg_count_today": 0, "subagents_active": 0,
                "model": None, "source": "session", "host": "host2", "stale": False,
            }],
        },
    }
    collector = AgentsCollector(ctx=ctx, herdr_bin="herdr", store=store)
    result = await collector.collect()
    store.close()

    remote_agents = [a for a in result["agents"] if a.get("session_id") == "remote-session-1"]
    assert len(remote_agents) == 1
    a = remote_agents[0]
    assert a["bead"] == "demo-a"
    assert a["bead_title"] == "a"
    assert "bead_claims" not in a
    assert "_bead_claims" not in a


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


# -- by-session-id transcript lookup: a herdr-listed pane with NO session
# record (its transcript went quiet longer ago than session_active_window_s)
# must still get its bead, by looking the transcript up by session id rather
# than by recent file activity (_resolve_transcript_paths). --------------


def _claude_bash_claim_lines(session_id, bead_id, tool_id="t1"):
    line1 = json.dumps({
        "timestamp": "2026-09-23T00:00:00.000Z", "sessionId": session_id,
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id, "name": "Bash",
             "input": {"command": f"bd update {bead_id} --claim", "description": "claim"}},
        ]},
    })
    line2 = json.dumps({
        "timestamp": "2026-09-23T00:00:01.000Z", "sessionId": session_id,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "is_error": False,
             "content": f"✓ Updated issue: {bead_id} — demo title"},
        ]},
    })
    return line1 + "\n" + line2 + "\n"


@pytest.mark.asyncio
async def test_collect_finds_bead_for_stale_claude_pane_via_session_id_lookup(monkeypatch, tmp_path):
    # herdr still lists the pane (status "done"), but its transcript's mtime
    # is old enough that scan_session_agents would never find it (no
    # session_projects_glob configured here at all -- proving the bead comes
    # from the by-id fallback, not from a session record). The transcript
    # also lives under a project dir name that does NOT match the pane's cwd
    # -- real case: a symlinked path decodes to a different project dirname
    # than the pane's own cwd -- proving the lookup goes by filename, never
    # by decoding/guessing a project dir from cwd.
    session_id = "stale-claude-session"
    bead_id = "demo-stale-a"
    _fake_herdr_exec(monkeypatch, [{
        "agent": "claude", "agent_status": "done", "cwd": "/srv/demo/ffw",
        "pane_id": "pane-stale", "workspace_id": "ws-1", "terminal_title_stripped": "ffw",
        "focused": False, "agent_session": {"value": session_id},
    }])
    projects_dir = tmp_path / "projects"
    mismatched_project_dir = projects_dir / "-srv-work-ffw"  # != "/srv/demo/ffw"
    mismatched_project_dir.mkdir(parents=True)
    transcript = mismatched_project_dir / f"{session_id}.jsonl"
    transcript.write_text(_claude_bash_claim_lines(session_id, bead_id))
    old = time.time() - 3600  # well outside any session_active_window_s
    os.utime(transcript, (old, old))

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    ctx.latest_beads_by_id = {bead_id: {"status": "in_progress", "title": "demo title"}}
    collector = AgentsCollector(
        ctx=ctx, herdr_bin="herdr", store=store, host="localhost",
        claude_projects_dir=str(projects_dir),
    )
    result = await collector.collect()
    store.close()

    agents = result["agents"]
    assert len(agents) == 1
    a = agents[0]
    assert a["source"] == "herdr"
    assert a["bead_tracked"] is True
    assert a["bead"] == bead_id
    assert a["bead_title"] == "demo title"


def _kimi_bash_claim_lines(bead_id, tool_id="k1"):
    line1 = json.dumps({
        "type": "agent.message.appended", "time": 1758000000000,
        "message": {"message": {"role": "assistant", "toolCalls": [
            {"type": "function", "id": tool_id, "name": "Bash",
             "arguments": json.dumps({"command": f"bd update {bead_id} --claim"})},
        ]}},
    })
    line2 = json.dumps({
        "type": "agent.message.appended", "time": 1758000001000,
        "message": {"message": {"role": "tool", "toolCallId": tool_id, "content": [
            {"type": "text", "text": f"✓ Updated issue: {bead_id} — demo title"},
        ]}}},
    )
    return line1 + "\n" + line2 + "\n"


@pytest.mark.asyncio
async def test_collect_finds_bead_for_stale_kimi_pane_via_session_id_lookup(monkeypatch, tmp_path):
    # Mirrors the Claude test above: herdr still lists the kimi pane, but
    # KimiCollector never published it to ctx.latest_kimi_agents (as if its
    # state.json updatedAt fell outside session_active_window_s) -- the bead
    # must still come from a by-session-id lookup under kimi_dir.
    session_id = "session_stalekimi1"
    bead_id = "demo-stale-kimi-a"
    _fake_herdr_exec(monkeypatch, [{
        "agent": "kimi", "agent_status": "idle", "cwd": "/srv/demo/repos/testrepo",
        "pane_id": "pane-k1", "workspace_id": "ws-k1", "terminal_title_stripped": "testrepo",
        "focused": False, "agent_session": {"value": session_id},
    }])

    kimi_dir = tmp_path / "kimi"
    session_dir = kimi_dir / "sessions" / "wd_testrepo_abc" / session_id
    agent_home = session_dir / "agents" / "main"
    agent_home.mkdir(parents=True)
    (agent_home / "wire.jsonl").write_text(_kimi_bash_claim_lines(bead_id))
    (session_dir / "state.json").write_text(json.dumps({
        "id": session_id, "cwd": "/srv/demo/repos/testrepo", "updatedAt": 1,  # ancient
        "agents": {"main": {"homedir": str(agent_home)}},
    }))
    kimi_dir.mkdir(exist_ok=True)
    (kimi_dir / "session_index.jsonl").write_text(json.dumps({
        "sessionId": session_id, "sessionDir": str(session_dir), "workDir": "/srv/demo/repos/testrepo",
    }) + "\n")

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    ctx.latest_beads_by_id = {bead_id: {"status": "in_progress", "title": "demo title"}}
    ctx.latest_kimi_agents = []  # KimiCollector hasn't (re-)published this stale session
    collector = AgentsCollector(
        ctx=ctx, herdr_bin="herdr", store=store, host="localhost", kimi_dir=str(kimi_dir),
    )
    result = await collector.collect()
    store.close()

    agents = result["agents"]
    assert len(agents) == 1
    a = agents[0]
    assert a["kind"] == "kimi"
    assert a["bead_tracked"] is True
    assert a["bead"] == bead_id
    assert a["bead_title"] == "demo title"


@pytest.mark.asyncio
async def test_collect_herdr_pane_with_no_transcript_anywhere_gets_null_bead(monkeypatch, tmp_path):
    session_id = "ghost-session"
    _fake_herdr_exec(monkeypatch, [{
        "agent": "claude", "agent_status": "done", "cwd": "/srv/demo/gone",
        "pane_id": "pane-ghost", "workspace_id": "ws-1", "terminal_title_stripped": "gone",
        "focused": False, "agent_session": {"value": session_id},
    }])
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    collector = AgentsCollector(
        ctx=ctx, herdr_bin="herdr", store=store, host="localhost",
        claude_projects_dir=str(projects_dir),
    )
    result = await collector.collect()
    store.close()

    a = result["agents"][0]
    assert a["bead_tracked"] is True
    assert a["bead"] is None
    assert a["bead_title"] is None


@pytest.mark.asyncio
async def test_transcript_lookup_cached_and_invalidated_on_delete_or_move(monkeypatch, tmp_path):
    session_id = "cached-lookup-session"
    bead_id = "demo-cache-a"
    _fake_herdr_exec(monkeypatch, [{
        "agent": "claude", "agent_status": "done", "cwd": "/srv/demo/cache-me",
        "pane_id": "pane-cache", "workspace_id": "ws-1", "terminal_title_stripped": "cache-me",
        "focused": False, "agent_session": {"value": session_id},
    }])
    projects_dir = tmp_path / "projects"
    project_a = projects_dir / "-project-a"
    project_a.mkdir(parents=True)
    transcript_a = project_a / f"{session_id}.jsonl"
    transcript_a.write_text(_claude_bash_claim_lines(session_id, bead_id))

    call_count = 0
    real_glob = agents_mod.glob.glob

    def counting_glob(pattern, *a, **kw):
        nonlocal call_count
        call_count += 1
        return real_glob(pattern, *a, **kw)

    monkeypatch.setattr(agents_mod.glob, "glob", counting_glob)

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    ctx.latest_beads_by_id = {bead_id: {"status": "in_progress", "title": "t"}}
    collector = AgentsCollector(
        ctx=ctx, herdr_bin="herdr", store=store, host="localhost",
        claude_projects_dir=str(projects_dir),
    )

    result1 = await collector.collect()
    assert result1["agents"][0]["bead"] == bead_id
    assert call_count == 1

    # Second tick, transcript unchanged: no fresh directory search.
    result2 = await collector.collect()
    assert result2["agents"][0]["bead"] == bead_id
    assert call_count == 1

    # Transcript deleted: the cached path no longer exists -> a fresh lookup
    # is forced (even though it now finds nothing).
    transcript_a.unlink()
    result3 = await collector.collect()
    assert result3["agents"][0]["bead"] is None
    assert call_count == 2

    # Transcript "moved" (recreated under a different project dir, same
    # session id) -- since nothing was cached after the miss above, the next
    # tick searches again and finds it at its new location.
    project_b = projects_dir / "-project-b"
    project_b.mkdir(parents=True)
    (project_b / f"{session_id}.jsonl").write_text(_claude_bash_claim_lines(session_id, bead_id))
    result4 = await collector.collect()
    store.close()
    assert result4["agents"][0]["bead"] == bead_id
    assert call_count == 3


# -- by-session-id transcript lookup: codex/grok/cursor ---------------------


@pytest.mark.asyncio
async def test_collect_finds_bead_for_stale_codex_pane_via_session_id_lookup(monkeypatch, tmp_path):
    session_id = "stale-codex-session"
    bead_id = "demo-stale-codex-a"
    _fake_herdr_exec(monkeypatch, [{
        "agent": "codex", "agent_status": "idle", "cwd": "/srv/demo/ffw",
        "pane_id": "pane-codex", "workspace_id": "ws-1", "terminal_title_stripped": "ffw",
        "focused": False, "agent_session": {"value": session_id},
    }])
    codex_dir = tmp_path / "codex"
    day_dir = codex_dir / "sessions" / "2026" / "09" / "18"
    day_dir.mkdir(parents=True)
    line = json.dumps({
        "type": "event_msg", "timestamp": "2026-09-18T00:00:00.000Z",
        "payload": {
            "type": "item_completed", "completed_at_ms": 1000,
            "item": {
                "type": "CommandExecution",
                "command": ["/bin/bash", "-lc", f"bd update {bead_id} --claim"],
                "status": "completed", "exit_code": 0,
                "aggregated_output": f"✓ Updated issue: {bead_id} — demo title",
            },
        },
    })
    (day_dir / f"rollout-2026-09-18T00-00-00-{session_id}.jsonl").write_text(line + "\n")

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    ctx.latest_beads_by_id = {bead_id: {"status": "in_progress", "title": "demo title"}}
    collector = AgentsCollector(
        ctx=ctx, herdr_bin="herdr", store=store, host="localhost", codex_dir=str(codex_dir),
    )
    result = await collector.collect()
    store.close()

    a = result["agents"][0]
    assert a["kind"] == "codex"
    assert a["bead_tracked"] is True
    assert a["bead"] == bead_id
    assert a["bead_title"] == "demo title"


@pytest.mark.asyncio
async def test_collect_finds_bead_for_stale_grok_pane_via_session_id_lookup(monkeypatch, tmp_path):
    session_id = "stale-grok-session"
    bead_id = "demo-stale-grok-a"
    _fake_herdr_exec(monkeypatch, [{
        "agent": "grok", "agent_status": "idle", "cwd": "/srv/demo/ffw",
        "pane_id": "pane-grok", "workspace_id": "ws-1", "terminal_title_stripped": "ffw",
        "focused": False, "agent_session": {"value": session_id},
    }])
    grok_dir = tmp_path / "grok"
    session_dir = grok_dir / "sessions" / "%2Fsrv%2Fdemo%2Fffw" / session_id
    session_dir.mkdir(parents=True)
    tool_id = "g1"
    chat_line = json.dumps({
        "type": "assistant", "content": "demo turn",
        "tool_calls": [{
            "id": tool_id, "name": "run_terminal_command",
            "arguments": json.dumps({"command": f"bd update {bead_id} --claim", "description": "run"}),
        }],
    })
    (session_dir / "chat_history.jsonl").write_text(chat_line + "\n")
    event_line = json.dumps({
        "ts": "2026-09-18T00:00:01.000Z", "type": "tool_completed", "tool_name": "run_terminal_command",
        "duration_ms": 10, "outcome": "success", "tool_call_id": tool_id,
    })
    (session_dir / "events.jsonl").write_text(event_line + "\n")

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    ctx.latest_beads_by_id = {bead_id: {"status": "in_progress", "title": "demo title"}}
    collector = AgentsCollector(
        ctx=ctx, herdr_bin="herdr", store=store, host="localhost", grok_dir=str(grok_dir),
    )
    result = await collector.collect()
    store.close()

    a = result["agents"][0]
    assert a["kind"] == "grok"
    assert a["bead_tracked"] is True
    assert a["bead"] == bead_id
    assert a["bead_title"] == "demo title"


@pytest.mark.asyncio
async def test_collect_finds_bead_for_stale_cursor_pane_via_session_id_lookup(monkeypatch, tmp_path):
    session_id = "stale-cursor-session"
    bead_id = "demo-stale-cursor-a"
    _fake_herdr_exec(monkeypatch, [{
        "agent": "cursor", "agent_status": "idle", "cwd": "/srv/demo/ffw",
        "pane_id": "pane-cursor", "workspace_id": "ws-1", "terminal_title_stripped": "ffw",
        "focused": False, "agent_session": {"value": session_id},
    }])
    cursor_dir = tmp_path / "cursor"
    session_dir = cursor_dir / "chats" / "demo-hash" / session_id
    session_dir.mkdir(parents=True)
    db_path = session_dir / "store.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    tool_id = "c1"
    call_row = {
        "role": "assistant",
        "content": [{"type": "tool-call", "toolCallId": tool_id, "toolName": "Shell",
                     "args": {"command": f"bd update {bead_id} --claim", "description": "run"}}],
        "id": "demo-msg-0",
    }
    con.execute("INSERT INTO blobs (id, data) VALUES (?, ?)", ("demo-blob-0", json.dumps(call_row)))
    result_row = {
        "role": "tool",
        "content": [{"type": "tool-result", "toolCallId": tool_id, "result": "ok",
                     "experimental_content": [{"type": "text", "text": "ok"}]}],
        "id": "demo-msg-1",
        "providerOptions": {"cursor": {"highLevelToolCallResult": {
            "output": {"command": f"bd update {bead_id} --claim", "stdout": "ok"}, "isError": False,
        }}},
    }
    con.execute("INSERT INTO blobs (id, data) VALUES (?, ?)", ("demo-blob-1", json.dumps(result_row)))
    con.commit()
    con.close()

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    ctx.latest_beads_by_id = {bead_id: {"status": "in_progress", "title": "demo title"}}
    collector = AgentsCollector(
        ctx=ctx, herdr_bin="herdr", store=store, host="localhost", cursor_dir=str(cursor_dir),
    )
    result = await collector.collect()
    store.close()

    a = result["agents"][0]
    assert a["kind"] == "cursor"
    assert a["bead_tracked"] is True
    assert a["bead"] == bead_id
    assert a["bead_title"] == "demo title"


# -- herdr present-but-broken: structured failure, not a bare RuntimeError --
# (herdr ABSENT entirely is not an error at all -- see collect()'s OSError
# catch and test_collect_surfaces_session_only_agent_invisible_to_herdr.)


@pytest.mark.asyncio
async def test_herdr_timeout_raises_classified_command_failed(monkeypatch, tmp_path):
    class HangingProc:
        returncode = 0

        async def communicate(self):
            import asyncio as _asyncio

            await _asyncio.sleep(999)

        def kill(self):
            pass

        async def wait(self):
            pass

    async def fake_exec(*args, **kwargs):
        return HangingProc()

    # collect() hardcodes a 10s wait_for timeout on the herdr call -- shrink
    # it here so the test doesn't actually wait 10s for the real timeout path.
    orig_wait_for = agents_mod.asyncio.wait_for

    async def fast_wait_for(coro, timeout):
        return await orig_wait_for(coro, timeout=0.02)

    monkeypatch.setattr(agents_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(agents_mod.asyncio, "wait_for", fast_wait_for)

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    collector = AgentsCollector(ctx=ctx, herdr_bin="herdr", store=store)

    with pytest.raises(CollectorIssue) as exc_info:
        await collector.collect()
    store.close()
    issue = exc_info.value
    assert issue.reason_code == "command_failed"
    assert "timed out" in issue.detail
    assert issue.optional is False


@pytest.mark.asyncio
async def test_herdr_nonzero_exit_raises_classified_command_failed_with_stderr(monkeypatch, tmp_path):
    class FailingProc:
        returncode = 1

        async def communicate(self):
            return b"", b"herdr: socket connection refused\n"

        def kill(self):
            pass

        async def wait(self):
            pass

    async def fake_exec(*args, **kwargs):
        return FailingProc()

    monkeypatch.setattr(agents_mod.asyncio, "create_subprocess_exec", fake_exec)

    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    collector = AgentsCollector(ctx=ctx, herdr_bin="herdr", store=store)

    with pytest.raises(CollectorIssue) as exc_info:
        await collector.collect()
    store.close()
    issue = exc_info.value
    assert issue.reason_code == "command_failed"
    assert "socket connection refused" in issue.detail
    assert issue.optional is False
