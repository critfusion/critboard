"""Codex and Grok token usage ingestion (critfusion-fleet-q733.11 phase 2).

All fixtures are synthetic: the formats were learned from real files, but no
transcript content is copied here."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest

from critdash import remote_probe as rp
from critdash.collectors import agents as agents_mod
from critdash.collectors.provider_usage import (
    ProviderUsageCollector,
    grok_ts,
    new_codex_state,
    parse_codex_lines,
    parse_grok_usage,
)
from critdash.collectors.usage import UsageCollector, _provider_session_map
from critdash.ctx import AppContext
from critdash.history import build_grouped_history, parse_window
from critdash.pricing import compute_provider_cost_usd
from critdash.store import Store

PRICING = {
    "models": {
        "default": {
            "input": 3.0,
            "output": 15.0,
            "cache_write_5m": 3.75,
            "cache_write_1h": 6.0,
            "cache_read": 0.3,
        }
    },
    "fast_mode": {},
    "monthly_budget_usd": 2000.0,
    # synthetic, test-only rates (USD per 1M tokens)
    "codex_models": {"gpt-test": {"input": 2.0, "output": 10.0, "cache_read": 0.5, "cache_write": 4.0}},
}
SESSION = "11111111-aaaa-bbbb-cccc-000000000001"


def _now_ts(offset_s: float = 0.0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _codex_records(model="gpt-test", ts=None, session=SESSION, turn="turn-1", n=2) -> str:
    ts = ts or _now_ts()
    out = _line(
        {"timestamp": ts, "type": "session_meta", "payload": {"id": session, "cwd": "/work/demo-repo"}}
    )
    out += _line(
        {
            "timestamp": ts,
            "type": "turn_context",
            "payload": {"turn_id": turn, "model": model, "cwd": "/work/demo-repo"},
        }
    )
    for i in range(n):
        out += _line(
            {
                "timestamp": ts,
                "type": "token_usage_record",
                "payload": {
                    "response_id": f"resp_{turn}_{i}",
                    "turn_id": turn,
                    "session_id": session,
                    # input 1000 INCLUDES 600 cached + 100 cache-written; output 50 INCLUDES 20 reasoning
                    "usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 600,
                        "cache_write_input_tokens": 100,
                        "output_tokens": 50,
                        "reasoning_output_tokens": 20,
                        "total_tokens": 1050,
                    },
                    "turn_token_usage": {"input_tokens": 99999, "output_tokens": 99999},
                    "thread_token_usage": {"input_tokens": 99999, "output_tokens": 99999},
                },
            }
        )
    # the id-less duplicate event the parser must ignore
    out += _line(
        {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 7777, "output_tokens": 7777}},
            },
        }
    )
    return out


def _grok_doc(session="grok-sess-1", ended=None, models=None, ticks=15739395600):
    models = models or {
        "grok-test": {
            "inputTokens": 1000,
            "outputTokens": 50,
            "cachedReadTokens": 600,
            "cacheCreationTokens": 0,
            "reasoningTokens": 20,
            "totalTokens": 1050,
            "modelCalls": 2,
            "costUsdTicks": ticks,
        }
    }
    return {
        "sessionId": session,
        "updatedAt": ended,
        "session": {},
        "turns": [
            {
                "turnNumber": 1,
                "endedAt": ended or "2026-11-04T10:15:30.123456789+00:00",
                "modelUsage": models,
            },
        ],
    }


def _write_codex(root, name="rollout-2026-11-04T10-00-00-" + SESSION + ".jsonl", text=None):
    d = root / "sessions" / "2026" / "11" / "04"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text if text is not None else _codex_records())
    return p


def _write_grok(root, doc=None, cwd="/work/demo-repo", session="grok-sess-1"):
    d = root / "sessions" / quote(cwd, safe="") / session
    d.mkdir(parents=True, exist_ok=True)
    p = d / "usage.json"
    p.write_text(json.dumps(doc or _grok_doc(session=session, ended=_now_ts())))
    return p


def _collector(store, tmp_path):
    return ProviderUsageCollector(
        store=store, host="localhost", codex_dir=str(tmp_path / "codex"), grok_dir=str(tmp_path / "grok")
    )


# -- parsers -------------------------------------------------------------------


def test_codex_input_excludes_cached_and_output_keeps_reasoning():
    events = parse_codex_lines(_codex_records(n=1).encode(), new_codex_state())
    assert len(events) == 1  # token_count event ignored
    e = events[0]
    assert (e["input"], e["cache_read"], e["cache_write"], e["output"]) == (300, 600, 100, 50)
    assert e["provider"] == "codex"
    assert e["session_id"] == SESSION
    assert e["model"] == "gpt-test"
    assert e["project"] == "demo-repo"
    assert e["event_key"] == "resp_turn-1_0"


def test_codex_uses_per_response_usage_not_running_totals():
    events = parse_codex_lines(_codex_records(n=2).encode(), new_codex_state())
    assert sum(e["input"] + e["output"] + e["cache_read"] + e["cache_write"] for e in events) == 2 * 1050


def test_codex_model_follows_turn_and_skips_garbage():
    text = (
        _codex_records(model="gpt-a", turn="t1", n=1)
        + "not json\n"
        + _codex_records(model="gpt-b", turn="t2", n=1)
    )
    events = parse_codex_lines(text.encode(), new_codex_state())
    assert [e["model"] for e in events] == ["gpt-a", "gpt-b"]


def test_codex_unknown_model_when_no_turn_context():
    line = _line(
        {
            "timestamp": _now_ts(),
            "type": "token_usage_record",
            "payload": {
                "response_id": "r1",
                "turn_id": "x",
                "session_id": SESSION,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }
    )
    [e] = parse_codex_lines(line.encode(), new_codex_state())
    assert e["model"] == "unknown"


def test_grok_events_ignore_cost_ticks_and_normalise_timestamp():
    doc = _grok_doc(ticks=123456789)
    [e] = parse_grok_usage(doc, quote("/work/demo-repo", safe=""))
    assert (e["input"], e["cache_read"], e["cache_write"], e["output"]) == (400, 600, 0, 50)
    assert e["ts"] == "2026-11-04T10:15:30.123Z"
    assert e["project"] == "demo-repo"
    assert e["provider"] == "grok"
    assert "cost" not in e and "cost_usd" not in e
    assert "123456789" not in json.dumps(e)


def test_grok_one_event_per_turn_model_and_bad_input():
    models = {
        "grok-a": {"inputTokens": 10, "outputTokens": 1},
        "grok-b": {"inputTokens": 20, "outputTokens": 2},
    }
    events = parse_grok_usage(_grok_doc(models=models))
    assert sorted(e["model"] for e in events) == ["grok-a", "grok-b"]
    assert parse_grok_usage({"turns": []}) == []
    assert parse_grok_usage(["nope"]) == []
    assert grok_ts("not a time") is None


# -- collector -----------------------------------------------------------------


def test_collector_ingests_codex_and_grok(tmp_store, tmp_path):
    _write_codex(tmp_path / "codex")
    _write_grok(tmp_path / "grok")
    n = asyncio.run(_collector(tmp_store, tmp_path).collect())
    assert n["_inserted"] == 3
    rows = {r["provider"]: r for r in tmp_store.provider_usage_grouped(("provider",))}
    assert rows["codex"]["messages"] == 2
    assert rows["codex"]["input"] == 600 and rows["codex"]["cache_read"] == 1200
    assert rows["grok"]["messages"] == 1 and rows["grok"]["cache_read"] == 600


def test_collector_is_idempotent_across_polls_and_restarts(tmp_store, tmp_path):
    codex = tmp_path / "codex"
    p = _write_codex(codex)
    _write_grok(tmp_path / "grok")
    c = _collector(tmp_store, tmp_path)
    assert c._ingest() == 3
    assert c._ingest() == 0
    # a new process has no in-memory model state: it re-reads the rollout, inserting nothing twice
    assert _collector(tmp_store, tmp_path)._ingest() == 0
    # appended record is picked up incrementally, with the model remembered from the earlier turn_context
    with open(p, "a") as f:
        f.write(
            _line(
                {
                    "timestamp": _now_ts(),
                    "type": "token_usage_record",
                    "payload": {
                        "response_id": "resp_new",
                        "turn_id": "turn-1",
                        "session_id": SESSION,
                        "usage": {"input_tokens": 40, "output_tokens": 2},
                    },
                }
            )
        )
    assert c._ingest() == 1
    [row] = [r for r in tmp_store.provider_usage_grouped(("provider", "model")) if r["provider"] == "codex"]
    assert row["model"] == "gpt-test" and row["messages"] == 3
    # ...and after a restart: the new process must recover the model, not file it under "unknown"
    restarted = _collector(tmp_store, tmp_path)
    with open(p, "a") as f:
        f.write(
            _line(
                {
                    "timestamp": _now_ts(),
                    "type": "token_usage_record",
                    "payload": {
                        "response_id": "resp_after_restart",
                        "turn_id": "turn-1",
                        "session_id": SESSION,
                        "usage": {"input_tokens": 8, "output_tokens": 1},
                    },
                }
            )
        )
    assert restarted._ingest() == 1
    models = {
        r["model"]
        for r in tmp_store.provider_usage_grouped(("provider", "model"))
        if r["provider"] == "codex"
    }
    assert models == {"gpt-test"}


def test_collector_waits_for_complete_line(tmp_store, tmp_path):
    p = _write_codex(tmp_path / "codex", text="")
    full = _codex_records(n=1)
    head = full.splitlines(True)
    p.write_text("".join(head[:2]) + head[2][:-10])  # usage record cut mid-write: no trailing newline
    c = _collector(tmp_store, tmp_path)
    c._ingest()
    assert sum(r["messages"] for r in tmp_store.provider_usage_grouped(("provider",))) == 0
    p.write_text(full)
    c._ingest()
    assert sum(r["messages"] for r in tmp_store.provider_usage_grouped(("provider",))) == 1


def test_collector_rewrites_grok_usage_when_a_turn_is_added(tmp_store, tmp_path):
    grok = tmp_path / "grok"
    p = _write_grok(grok)
    c = _collector(tmp_store, tmp_path)
    assert c._ingest() == 1
    doc = json.loads(p.read_text())
    doc["turns"].append(
        {
            "turnNumber": 2,
            "endedAt": _now_ts(),
            "modelUsage": {"grok-test": {"inputTokens": 5, "outputTokens": 1}},
        }
    )
    p.write_text(json.dumps(doc))
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert c._ingest() == 1


def test_collector_missing_dirs_are_silent(tmp_store, tmp_path):
    assert _collector(tmp_store, tmp_path)._ingest() == 0


# -- pricing -------------------------------------------------------------------


def test_codex_cost_priced_and_unpriced():
    cost = compute_provider_cost_usd(PRICING, "codex", "gpt-test-mini", 300, 50, 600, 100)
    assert cost == pytest.approx((300 * 2.0 + 50 * 10.0 + 600 * 0.5 + 100 * 4.0) / 1e6)
    # no entry -> unknown, and the Claude "default" rate must NOT be borrowed
    assert compute_provider_cost_usd(PRICING, "codex", "gpt-9-unpriced", 300, 50, 600, 100) is None
    assert (
        compute_provider_cost_usd({"models": {"default": {"input": 3.0}}}, "codex", "gpt-test", 1, 1, 0, 0)
        is None
    )


def test_grok_and_other_providers_cost_is_always_unknown():
    for provider in ("grok", "kimi", "cursor"):
        assert compute_provider_cost_usd(PRICING, provider, "gpt-test", 10, 10, 0, 0) is None


def test_shipped_codex_prices_resolve_each_local_model_to_its_own_row():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "pricing.json")
    shipped = json.load(open(path))
    # (model, input, cache_read, output) USD per 1M; gpt-6.1-* must not fall into gpt-6-*
    expected = {
        "gpt-5.6-terra": (2.00, 0.20, 12.00), "gpt-6-astra": (10.00, 1.00, 50.00),
        "gpt-6-luna": (0.10, 0.01, 0.50), "gpt-6.1-sol": (2.00, 0.10, 10.00),
        "gpt-5.6-sol": (4.00, 0.40, 20.00), "gpt-6.1-terra": (1.00, 0.05, 5.00),
        "gpt-6-sol": (2.00, 0.20, 10.00), "gpt-5.6-luna": (0.20, 0.02, 1.20),
    }
    # cache_write is the 30-minute write rate, 1.25x input
    for model in expected:
        row = shipped["codex_models"][model]
        assert row["cache_write"] == pytest.approx(expected[model][0] * 1.25), model
    for model, (inp, cached, out) in expected.items():
        cost = compute_provider_cost_usd(
            shipped, "codex", model + "-2026-11-01", *(1_000_000,) * 4
        )
        assert cost == pytest.approx(inp + out + cached + inp * 1.25), model
    # a model with no row stays unknown
    assert compute_provider_cost_usd(shipped, "codex", "gpt-7-unlisted", 1000, 1000, 0, 0) is None


# -- rollups (usage.by_provider, per-session map) --------------------------------


def _by_provider(rollups, provider, window="all"):
    return next(r for r in rollups["by_provider"] if r["provider"] == provider and r["window"] == window)


def test_by_provider_codex_priced_grok_tokens_cursor_unknown(tmp_store, tmp_path):
    _write_codex(tmp_path / "codex")
    _write_grok(tmp_path / "grok")
    _collector(tmp_store, tmp_path)._ingest()
    rollups = UsageCollector(store=tmp_store, pricing=PRICING)._compute_rollups()

    codex = _by_provider(rollups, "codex")
    assert codex["tokens"] == 2 * 1050 and codex["messages"] == 2
    assert codex["cost_usd"] == pytest.approx(2 * (300 * 2.0 + 50 * 10.0 + 600 * 0.5 + 100 * 4.0) / 1e6)

    grok = _by_provider(rollups, "grok")
    assert grok["tokens"] == 1000 + 50 and grok["messages"] == 1
    assert grok["cost_usd"] is None

    cursor = _by_provider(rollups, "cursor")
    assert cursor["tokens"] is None and cursor["messages"] is None and cursor["cost_usd"] is None


def test_by_provider_codex_cost_is_null_when_any_model_unpriced(tmp_store, tmp_path):
    text = _codex_records(model="gpt-test", turn="t1", n=1) + _codex_records(model="gpt-nope", turn="t2", n=1)
    _write_codex(tmp_path / "codex", text=text)
    _collector(tmp_store, tmp_path)._ingest()
    codex = _by_provider(UsageCollector(store=tmp_store, pricing=PRICING)._compute_rollups(), "codex")
    assert codex["tokens"] == 2 * 1050
    assert codex["cost_usd"] is None  # not the partial sum of the priced half


def test_by_provider_empty_codex_is_zero_cost_not_unknown(tmp_store):
    codex = _by_provider(UsageCollector(store=tmp_store, pricing=PRICING)._compute_rollups(), "codex")
    assert codex["tokens"] == 0 and codex["cost_usd"] == 0.0


def test_claude_totals_unchanged_by_provider_usage(tmp_store, tmp_path):
    before = UsageCollector(store=tmp_store, pricing=PRICING)._compute_rollups()["totals"]
    _write_codex(tmp_path / "codex")
    _write_grok(tmp_path / "grok")
    _collector(tmp_store, tmp_path)._ingest()
    assert UsageCollector(store=tmp_store, pricing=PRICING)._compute_rollups()["totals"] == before


def test_session_map_carries_codex_and_grok_sessions(tmp_store, tmp_path):
    _write_codex(tmp_path / "codex")
    _write_grok(tmp_path / "grok")
    _collector(tmp_store, tmp_path)._ingest()
    ctx = AppContext(config=None, store=tmp_store)
    UsageCollector(ctx=ctx, store=tmp_store, pricing=PRICING)._update_ctx_session_map()
    codex = ctx.usage_by_session[SESSION]
    assert codex["tokens_today"]["total"] == 2 * 1050
    assert codex["cost_today_usd"] is not None and codex["last_model"] == "gpt-test"
    grok = ctx.usage_by_session["grok-sess-1"]
    assert grok["tokens_today"]["total"] == 1050  # input 400 + cache 600 + output 50
    assert grok["cost_today_usd"] is None


def test_session_map_prices_each_provider_with_its_own_rates_on_id_collision(tmp_store, tmp_path):
    same = "shared-session-id"
    _write_codex(tmp_path / "codex", text=_codex_records(session=same, n=1))
    # grok model named like a priced codex model: codex rates WOULD price it if groups mixed
    grok_models = {"gpt-test": {"inputTokens": 1000, "outputTokens": 50, "cachedReadTokens": 600}}
    grok_doc = _grok_doc(session=same, ended=_now_ts(), models=grok_models)
    _write_grok(tmp_path / "grok", doc=grok_doc, session=same)
    _collector(tmp_store, tmp_path)._ingest()
    since = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = _provider_session_map(tmp_store, PRICING, since, "localhost")[same]
    # codex 1050 + grok 1050 tokens; the grok group must not be priced at codex rates
    assert entry["tokens_today"]["total"] == 2 * 1050
    assert entry["cost_today_usd"] is None
    # a lone codex session stays priced, so the None above comes from the merge, not a lost group
    other = "codex-only"
    _write_codex(
        tmp_path / "codex", name="rollout-x-" + other + ".jsonl", text=_codex_records(session=other, n=1)
    )
    _collector(tmp_store, tmp_path)._ingest()
    assert _provider_session_map(tmp_store, PRICING, since, "localhost")[other]["cost_today_usd"] is not None


# -- agent cards -----------------------------------------------------------------


def test_card_usage_fields_by_kind():
    ubs = {
        "cx": {"tokens_today": {"total": 5}, "cost_today_usd": 0.5, "msg_count_today": 1},
        "gk": {"tokens_today": {"total": 7}, "cost_today_usd": None, "msg_count_today": 1},
    }
    f = agents_mod._usage_fields
    assert f("cx", ubs, "codex")["cost_today_usd"] == 0.5
    assert (
        f("gk", ubs, "grok")["cost_today_usd"] is None and f("gk", ubs, "grok")["tokens_today"]["total"] == 7
    )
    assert f("none", ubs, "grok")["cost_today_usd"] is None  # grok never defaults to $0
    assert f("none", ubs, "codex")["cost_today_usd"] == 0.0  # codex with no events spent nothing
    cur = f("cur", ubs, "cursor")
    assert cur["tokens_today"] is None and cur["cost_today_usd"] is None and cur["msg_count_today"] is None
    assert f("c", {}, "claude")["cost_today_usd"] == 0.0  # existing behaviour untouched


def test_herdr_cursor_card_is_null_and_remote_cards_are_unknown(tmp_path, monkeypatch):
    herdr = json.dumps(
        {
            "result": {
                "agents": [
                    {
                        "agent": "cursor",
                        "agent_session": {"value": "cur-1"},
                        "agent_status": "working",
                        "cwd": "/w/x",
                        "pane_id": "p1",
                    },
                    {
                        "agent": "codex",
                        "agent_session": {"value": SESSION},
                        "agent_status": "idle",
                        "cwd": "/w/x",
                        "pane_id": "p2",
                    },
                ]
            }
        }
    )

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return herdr.encode(), b""

    async def fake_exec(*a, **k):
        return FakeProc()

    monkeypatch.setattr(agents_mod.asyncio, "create_subprocess_exec", fake_exec)
    store = Store(tmp_path / "t.db")
    ctx = AppContext(config=None, store=store)
    ctx.usage_by_session = {
        SESSION: {
            "tokens_today": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0, "total": 2},
            "cost_today_usd": 0.25,
            "msg_count_today": 1,
        }
    }
    ctx.remote_hosts = {
        "h2": {
            "ok": True,
            "agents": [
                {"kind": "codex", "session_id": "r1", "tokens_today": {"total": 0}, "cost_today_usd": 0.0},
                {"kind": "claude", "session_id": "r2", "tokens_today": {"total": 0}, "cost_today_usd": 0.0},
            ],
        }
    }
    result = asyncio.run(AgentsCollector_collect(store, ctx))
    store.close()
    by = {a["session_id"]: a for a in result["agents"]}
    assert by["cur-1"]["tokens_today"] is None and by["cur-1"]["cost_today_usd"] is None
    assert by[SESSION]["cost_today_usd"] == 0.25 and by[SESSION]["tokens_today"]["total"] == 2
    assert by["r1"]["tokens_today"] is None and by["r1"]["cost_today_usd"] is None
    assert by["r2"]["cost_today_usd"] == 0.0  # remote Claude card unchanged


async def AgentsCollector_collect(store, ctx):
    return await agents_mod.AgentsCollector(ctx=ctx, herdr_bin="herdr", store=store).collect()


# -- history -----------------------------------------------------------------------


def _history(store, group_by="provider", bucket="day"):
    now = datetime.now(UTC)
    since, until = parse_window("7d", now)
    return build_grouped_history(store, PRICING, "7d", bucket, group_by, since, until)


def test_history_codex_priced_grok_null_and_totals_stay_claude_only(tmp_store, tmp_path):
    _write_codex(tmp_path / "codex")
    _write_grok(tmp_path / "grok")
    _collector(tmp_store, tmp_path)._ingest()
    resp = _history(tmp_store)
    codex = next(s for s in resp["series"] if s["key"] == "codex")
    grok = next(s for s in resp["series"] if s["key"] == "grok")
    assert sum(codex["tokens"]) == 2 * 1050 and sum(grok["tokens"]) == 1050
    assert codex["cost_usd"][-1] is not None and codex["cost_usd"][-1] > 0
    assert codex["cost_usd"][0] is None  # no data in the bucket: unknown, not 0.0
    assert all(c is None for c in grok["cost_usd"])
    assert resp["totals"]["cost_usd"] == 0.0 and resp["totals"]["cost_usd_basis"] == "claude_only"
    assert resp["totals"]["tokens"] == 2 * 1050 + 1050


def test_history_mixed_priced_and_unpriced_codex_models_is_null(tmp_store, tmp_path):
    text = _codex_records(model="gpt-test", turn="t1", n=1) + _codex_records(model="gpt-nope", turn="t2", n=1)
    _write_codex(tmp_path / "codex", text=text)
    _collector(tmp_store, tmp_path)._ingest()
    codex = next(s for s in _history(tmp_store)["series"] if s["key"] == "codex")
    assert codex["cost_usd"][-1] is None
    by_model = {s["key"]: s for s in _history(tmp_store, "model")["series"]}
    assert by_model["gpt-test"]["cost_usd"][-1] is not None
    assert by_model["gpt-nope"]["cost_usd"][-1] is None


def test_history_group_by_host_cost_is_claude_only(tmp_store, tmp_path):
    _write_codex(tmp_path / "codex")
    _collector(tmp_store, tmp_path)._ingest()
    host = next(s for s in _history(tmp_store, "host")["series"] if s["key"] == "localhost")
    assert sum(host["tokens"]) == 2 * 1050
    assert host["cost_usd"][-1] == 0.0


# -- remote probe parity -------------------------------------------------------------


def test_probe_parsers_match_collector_parsers():
    data = (
        _codex_records(model="gpt-a", turn="t1", n=2) + _codex_records(model="gpt-b", turn="t2", n=1)
    ).encode()
    assert rp.parse_codex_usage(data) == parse_codex_lines(data, new_codex_state())
    doc = _grok_doc(
        models={
            "grok-a": {"inputTokens": 9, "outputTokens": 3, "cachedReadTokens": 4, "cacheCreationTokens": 1}
        }
    )
    dirname = quote("/work/demo-repo", safe="")
    assert rp.parse_grok_usage(doc, dirname) == parse_grok_usage(doc, dirname)


def test_probe_missing_dirs_is_silent_noop(tmp_path):
    out = rp.collect_provider_usage(
        str(tmp_path / "no-codex"), str(tmp_path / "no-grok"), str(tmp_path / "s.json")
    )
    assert out == {"provider_usage_buckets": []}


def test_remote_buckets_roll_up_like_local_events(tmp_path):
    codex_root, grok_root = tmp_path / "codex", tmp_path / "grok"
    _write_codex(codex_root)
    _write_grok(grok_root)

    local = Store(tmp_path / "local.db")
    ProviderUsageCollector(
        store=local, host="localhost", codex_dir=str(codex_root), grok_dir=str(grok_root)
    )._ingest()

    buckets = rp.collect_provider_usage(str(codex_root), str(grok_root), str(tmp_path / "state.json"))[
        "provider_usage_buckets"
    ]
    # second call hits the state-file cache and must give the same buckets
    again = rp.collect_provider_usage(str(codex_root), str(grok_root), str(tmp_path / "state.json"))[
        "provider_usage_buckets"
    ]
    assert sorted(buckets, key=json.dumps) == sorted(again, key=json.dumps)

    remote = Store(tmp_path / "remote.db")
    remote.upsert_remote_provider_usage_buckets([{**b, "host": "host-b"} for b in buckets])
    remote.upsert_remote_provider_usage_buckets(
        [{**b, "host": "host-b"} for b in buckets]
    )  # replace, not double

    def totals(rows):
        return {
            r["provider"]: (r["input"], r["output"], r["cache_read"], r["cache_write"], r["messages"])
            for r in rows
        }

    expect = totals(local.provider_usage_grouped(("provider",)))
    assert totals(remote.provider_usage_grouped(("provider",), remote=True)) == expect
    assert expect["codex"] == (600, 100, 1200, 200, 2)
    local.close()
    remote.close()


def test_remote_codex_cost_priced_centrally_grok_unknown(tmp_store, tmp_path):
    root = tmp_path / "codex"
    _write_codex(root)
    _write_grok(tmp_path / "grok")
    buckets = rp.collect_provider_usage(str(root), str(tmp_path / "grok"), str(tmp_path / "state.json"))[
        "provider_usage_buckets"
    ]
    tmp_store.upsert_remote_provider_usage_buckets([{**b, "host": "host-b"} for b in buckets])
    rollups = UsageCollector(store=tmp_store, pricing=PRICING)._compute_rollups()
    assert _by_provider(rollups, "codex")["tokens"] == 2 * 1050
    assert _by_provider(rollups, "codex")["cost_usd"] > 0
    assert _by_provider(rollups, "grok")["cost_usd"] is None
    assert _by_provider(rollups, "grok")["tokens"] == 1050


def test_probe_state_save_keeps_keys_written_during_the_scan(tmp_path, monkeypatch):
    codex_root = tmp_path / "codex"
    _write_codex(codex_root)
    state = tmp_path / "state.json"
    real_parse = rp.parse_codex_usage

    def parse_and_race(data):
        # another probe process saves its own section while this scan is running
        rp.save_state(str(state), {"files": {"x": 1}, "kimi_files": {"y": 2}})
        return real_parse(data)

    monkeypatch.setattr(rp, "parse_codex_usage", parse_and_race)
    rp.collect_provider_usage(str(codex_root), str(tmp_path / "no-grok"), str(state))
    saved = json.loads(state.read_text())
    assert saved["files"] == {"x": 1} and saved["kimi_files"] == {"y": 2}
    assert "provider_files" in saved
