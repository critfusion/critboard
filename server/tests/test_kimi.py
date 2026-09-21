"""Kimi Code collector: a second provider alongside Claude Code (briefing
2026-09-18). Fixtures under tests/fixtures/kimi/ are a captured, unmodified
copy of the real verified ~/.kimi-code format on localhost (session_index.jsonl,
workspaces.json, state.json, wire.jsonl -- see collectors/kimi.py's module
docstring for the exact commands used to verify it live)."""

from __future__ import annotations

from critdash.collectors.kimi import (
    KimiCollector,
    classify_kimi_error,
    discover_kimi_sessions,
    epoch_ms_to_iso,
    load_session_index,
    load_session_state,
    load_workspaces,
    tail_last_model,
    tail_last_model_across_agents,
)
from critdash.ctx import AppContext

# -- epoch-ms conversion -------------------------------------------------------


def test_epoch_ms_to_iso_real_value():
    # Real value captured live from the demo-fleet session's state.json
    # createdAt, cross-checked with `date -d @1789600857.315`.
    assert epoch_ms_to_iso(1789600857315) == "2026-09-16T23:20:57.315Z"


def test_epoch_ms_to_iso_none_and_garbage():
    assert epoch_ms_to_iso(None) is None
    assert epoch_ms_to_iso("not-a-number") is None


# -- session_index.jsonl / workspaces.json / state.json parsing --------------


def test_load_session_index_parses_real_shape(make_kimi_root):
    root = make_kimi_root()
    entries = load_session_index(str(root))
    assert {e["session_id"] for e in entries} == {"session_test-uuid-1", "session_test-uuid-2"}
    e1 = next(e for e in entries if e["session_id"] == "session_test-uuid-1")
    assert e1["work_dir"] == "/srv/demo/repos/testrepo"
    assert e1["session_dir"].endswith("sessions/wd_testrepo_abc123/session_test-uuid-1")


def test_load_session_index_missing_file_returns_empty(tmp_path):
    assert load_session_index(str(tmp_path)) == []


def test_load_workspaces_parses_real_shape(make_kimi_root):
    root = make_kimi_root()
    workspaces = load_workspaces(str(root))
    assert workspaces["wd_testrepo_abc123"]["root"] == "/srv/demo/repos/testrepo"
    assert workspaces["wd_testrepo_abc123"]["name"] == "testrepo"


def test_load_workspaces_missing_file_returns_empty(tmp_path):
    assert load_workspaces(str(tmp_path)) == {}


def test_load_session_state_parses_real_shape(make_kimi_root):
    root = make_kimi_root()
    session_dir = str(root / "sessions" / "wd_testrepo_abc123" / "session_test-uuid-1")
    state = load_session_state(session_dir)
    assert state["id"] == "session_test-uuid-1"
    assert state["cwd"] == "/srv/demo/repos/testrepo"
    assert state["lastTurnReason"] == "failed"
    assert "main" in state["agents"]


def test_load_session_state_missing_dir_returns_none(tmp_path):
    assert load_session_state(str(tmp_path / "nope")) is None


# -- discover_kimi_sessions: cwd from state.json, workspaces.json fallback ---


def test_discover_kimi_sessions_cwd_from_state_json(make_kimi_root):
    root = make_kimi_root()
    sessions = discover_kimi_sessions(str(root))
    assert len(sessions) == 2
    s1 = next(s for s in sessions if s["session_id"] == "session_test-uuid-1")
    assert s1["cwd"] == "/srv/demo/repos/testrepo"
    assert "main" in s1["agent_wire_paths"]
    assert s1["agent_wire_paths"]["main"].endswith("agents/main/wire.jsonl")


def test_discover_kimi_sessions_falls_back_to_workspaces_root_when_cwd_absent(make_kimi_root):
    # session_test-uuid-2's fixture state.json has NO "cwd" key at all --
    # discovery must fall back to workspaces.json's root for that workspace.
    root = make_kimi_root()
    sessions = discover_kimi_sessions(str(root))
    s2 = next(s for s in sessions if s["session_id"] == "session_test-uuid-2")
    assert s2["cwd"] == "/srv/demo/repos/testrepo"


def test_discover_kimi_sessions_no_index_file_returns_empty(tmp_path):
    assert discover_kimi_sessions(str(tmp_path)) == []


# -- tail-read of wire.jsonl for model -----------------------------------------


def _wire_path(root, session_id):
    return root / "sessions" / "wd_testrepo_abc123" / session_id / "agents" / "main" / "wire.jsonl"


def test_tail_last_model_reads_model_alias(make_kimi_root):
    root = make_kimi_root()
    wire_path = _wire_path(root, "session_test-uuid-1")
    assert tail_last_model(str(wire_path)) == "kimi-code/kimi-for-coding"


def test_tail_last_model_picks_most_recent_of_several_requests(make_kimi_root):
    # session_test-uuid-2's wire.jsonl has TWO llm.request lines (turn 0 and
    # turn 1) -- tail read must not just grab the first one it sees.
    root = make_kimi_root()
    wire_path = _wire_path(root, "session_test-uuid-2")
    assert tail_last_model(str(wire_path)) == "kimi-code/kimi-for-coding"


def test_tail_last_model_missing_file_is_none(tmp_path):
    assert tail_last_model(str(tmp_path / "nope.jsonl")) is None


def test_tail_last_model_across_agents_picks_most_recent_not_first(tmp_path):
    # Reproduces a real bug found live on localhost 2026-09-18: an active
    # session had 4 agent wire.jsonl files (main + 3 subagents). "main"'s
    # tail had NO llm.request in its last 64KB (dominated by tool-call
    # bookkeeping) while a subagent's did -- checking only "main" (or
    # whichever dict key happens to be first) reported model=None despite
    # the session being actively in use.
    main_wire = tmp_path / "main.jsonl"
    main_wire.write_text(
        '{"type":"llm.request","model":"old-model","modelAlias":"kimi/old-model","time":1000}\n'
        '{"type":"tool.call","time":2000}\n'
    )
    subagent_wire = tmp_path / "agent-0.jsonl"
    subagent_wire.write_text(
        '{"type":"llm.request","model":"kimi-for-coding","modelAlias":"kimi-code/kimi-for-coding","time":5000}\n'
    )
    wire_paths = {"main": str(main_wire), "agent-0": str(subagent_wire)}
    assert tail_last_model_across_agents(wire_paths) == "kimi-code/kimi-for-coding"


def test_tail_last_model_across_agents_empty_dict_is_none():
    assert tail_last_model_across_agents({}) is None


# -- quota error classification -------------------------------------------------


def test_classify_kimi_error_real_quota_message_is_quota_exceeded():
    message = (
        "403 You've reached your monthly usage limit for this billing cycle. "
        "Your quota will be refreshed in the next cycle. To continue now, "
        "purchase extra usage or upgrade your plan: "
        "https://www.kimi.com/membership/subscription?tab=quota"
    )
    assert classify_kimi_error(message, "provider.auth_error") == "quota_exceeded"


def test_classify_kimi_error_falls_back_to_code_when_no_message():
    # A code alone with no matching text keyword falls through to "other" --
    # honest, not a fabricated match.
    assert classify_kimi_error(None, "provider.auth_error") == "other"


# -- KimiCollector: discovery + agents[] + token/error ingestion --------------


async def _run(store, kimi_dir, host="localhost", session_active_window_s=900.0):
    ctx = AppContext(config=None, store=store)
    collector = KimiCollector(
        ctx=ctx, store=store, host=host, kimi_dir=kimi_dir,
        session_active_window_s=session_active_window_s,
    )
    await collector.collect()
    return ctx


async def test_kimi_collector_missing_dir_is_silent_noop(tmp_store, tmp_path):
    ctx = await _run(tmp_store, str(tmp_path / "no-such-kimi-dir"))
    assert ctx.latest_kimi_agents == []


async def test_kimi_collector_builds_agents_with_kind_kimi(tmp_store, make_kimi_root):
    root = make_kimi_root(age_s_1=300.0, age_s_2=30.0)
    ctx = await _run(tmp_store, str(root))
    ids = {a["session_id"] for a in ctx.latest_kimi_agents}
    assert ids == {"session_test-uuid-1", "session_test-uuid-2"}
    for a in ctx.latest_kimi_agents:
        assert a["kind"] == "kimi"
        assert a["source"] == "session"
        assert a["host"] == "localhost"
        # null, not 0.0 -- Kimi bills by subscription quota (see module
        # docstring); reporting 0.0 would claim "free", which is false.
        assert a["cost_today_usd"] is None


async def test_kimi_collector_agent_cwd_matches_state_json(tmp_store, make_kimi_root):
    root = make_kimi_root()
    ctx = await _run(tmp_store, str(root))
    a1 = next(a for a in ctx.latest_kimi_agents if a["session_id"] == "session_test-uuid-1")
    assert a1["cwd"] == "/srv/demo/repos/testrepo"


async def test_kimi_collector_status_idle_vs_working_from_updated_at(tmp_store, make_kimi_root):
    root = make_kimi_root(age_s_1=300.0, age_s_2=30.0)  # session1 idle, session2 working
    ctx = await _run(tmp_store, str(root))
    a1 = next(a for a in ctx.latest_kimi_agents if a["session_id"] == "session_test-uuid-1")
    a2 = next(a for a in ctx.latest_kimi_agents if a["session_id"] == "session_test-uuid-2")
    assert a1["status"] == "idle"
    assert a2["status"] == "working"


async def test_kimi_collector_session_outside_window_is_excluded(tmp_store, make_kimi_root):
    root = make_kimi_root(age_s_1=3000.0, age_s_2=30.0)  # session1 well past the 900s window
    ctx = await _run(tmp_store, str(root), session_active_window_s=900.0)
    ids = {a["session_id"] for a in ctx.latest_kimi_agents}
    assert ids == {"session_test-uuid-2"}


async def test_kimi_collector_ingests_token_counting_events(tmp_store, make_kimi_root):
    root = make_kimi_root()
    await _run(tmp_store, str(root))
    totals = tmp_store.kimi_usage_totals()
    # session1: one failed turn, 208 tokens (real captured value). session2:
    # two successful turns, 22402 + 598 tokens.
    assert totals["tokens"] == 208 + 22402 + 598
    assert totals["messages"] == 3


async def test_kimi_collector_ingestion_is_incremental_no_double_count(tmp_store, make_kimi_root):
    root = make_kimi_root()
    await _run(tmp_store, str(root))
    await _run(tmp_store, str(root))  # second poll, no new bytes written
    totals = tmp_store.kimi_usage_totals()
    assert totals["tokens"] == 208 + 22402 + 598
    assert totals["messages"] == 3


async def test_kimi_collector_per_session_token_totals(tmp_store, make_kimi_root):
    root = make_kimi_root()
    await _run(tmp_store, str(root))
    by_session = {r["session_id"]: r for r in tmp_store.kimi_usage_by_session()}
    assert by_session["session_test-uuid-1"]["tokens"] == 208
    assert by_session["session_test-uuid-1"]["messages"] == 1
    assert by_session["session_test-uuid-2"]["tokens"] == 22402 + 598
    assert by_session["session_test-uuid-2"]["messages"] == 2


async def test_kimi_collector_ingests_quota_error(tmp_store, make_kimi_root):
    root = make_kimi_root()
    await _run(tmp_store, str(root))
    errors = tmp_store.kimi_error_counts()
    assert len(errors) == 1
    assert errors[0]["kind"] == "quota_exceeded"
    assert errors[0]["count"] == 1
