"""Tests for critdash.history: window parsing and the group_by=model/
provider/host grouped response backing the new historical usage graph."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from critdash.history import (
    InvalidWindowError,
    bucket_count,
    build_grouped_history,
    parse_window,
)
from critdash.store import dense_bucket_keys, fold_hour_rows_to_local_day

PRICING = {
    "models": {
        "claude-opus-5": {
            "input": 5.0, "output": 25.0,
            "cache_write_5m": 6.25, "cache_write_1h": 10.0, "cache_read": 0.50,
        },
        "claude-sonnet-5": {
            "input": 3.0, "output": 15.0,
            "cache_write_5m": 3.75, "cache_write_1h": 6.0, "cache_read": 0.30,
        },
        "default": {
            "input": 3.0, "output": 15.0,
            "cache_write_5m": 3.75, "cache_write_1h": 6.0, "cache_read": 0.30,
        },
    },
    "fast_mode": {},
    "monthly_budget_usd": 2000,
}

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


# -- parse_window / bucket_count ---------------------------------------------


def test_parse_window_today_is_calendar_anchored():
    since, until = parse_window("today", NOW)
    assert since == NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    assert until == NOW


def test_parse_window_named_and_arbitrary_forms():
    for window, expected in [
        ("24h", timedelta(hours=24)), ("7d", timedelta(days=7)),
        ("30d", timedelta(days=30)), ("90d", timedelta(days=90)),
        ("6h", timedelta(hours=6)), ("3d", timedelta(days=3)),
    ]:
        since, until = parse_window(window, NOW)
        assert until - since == expected, window


@pytest.mark.parametrize("bad", ["bogus", "", "5x", "-5d", "0d", "0h", "7 days", "d7"])
def test_parse_window_rejects_garbage(bad):
    with pytest.raises(InvalidWindowError):
        parse_window(bad, NOW)


def test_bucket_count_matches_old_formula_for_fixed_windows():
    since, until = parse_window("90d", NOW)
    assert bucket_count(since, until, "day", "90d") == 90
    since, until = parse_window("24h", NOW)
    assert bucket_count(since, until, "hour", "24h") == 24


def test_bucket_count_today_hour_is_hours_since_midnight_inclusive():
    since, until = parse_window("today", NOW)  # NOW is 12:00:00
    assert bucket_count(since, until, "hour", "today") == 13  # hours 00..12


def test_bucket_count_today_day_is_one():
    since, until = parse_window("today", NOW)
    assert bucket_count(since, until, "day", "today") == 1


# -- helpers to seed a tmp_store ----------------------------------------------


def _local_row(ts, model, tokens_in=100, tokens_out=50, host="localhost", message_id=None):
    return {
        "message_id": message_id or f"m-{ts}-{model}-{host}",
        "ts": ts, "session_id": "s1", "project": "p", "project_path": "/p",
        "model": model, "input": tokens_in, "output": tokens_out,
        "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0,
        "speed": None, "is_sidechain": 0, "web_searches": 0,
        "cost_usd": (tokens_in * 3.0 + tokens_out * 15.0) / 1_000_000,
        "host": host,
    }


def _remote_row(hour, model, host, tokens_in=200, tokens_out=100, project="p"):
    return {
        "host": host, "hour": hour, "model": model, "project": project,
        "input": tokens_in, "output": tokens_out, "cache_read": 0,
        "cache_write_5m": 0, "cache_write_1h": 0, "messages": 1,
    }


def _kimi_row(ts, ts_ms, model="kimi-code/kimi-for-coding", host="localhost", tokens=500, session_id="k1"):
    return {
        "host": host, "session_id": session_id, "ts": ts, "ts_ms": ts_ms,
        "model": model, "tokens": tokens, "project": "p",
    }


def _remote_kimi_row(day, model, host, tokens=700, turns=1):
    return {"host": host, "day": day, "model": model, "tokens": tokens, "turns": turns}


# -- group_by shapes + dense series -------------------------------------------


def test_group_by_model_dense_series_and_cost(tmp_store):
    tmp_store.insert_usage_events([
        _local_row("2026-09-18T10:00:00Z", "claude-opus-5"),
        _local_row("2026-09-19T10:00:00Z", "claude-opus-5"),
    ])
    since, until = parse_window("7d", NOW)
    resp = build_grouped_history(tmp_store, PRICING, "7d", "day", "model", since, until)

    assert resp["window"] == "7d"
    assert resp["bucket"] == "day"
    assert resp["group_by"] == "model"
    assert len(resp["buckets"]) == 7
    assert resp["buckets"][-1] == "2026-09-20"

    opus = next(s for s in resp["series"] if s["key"] == "claude-opus-5")
    assert opus["provider"] == "claude"
    for field in ("tokens", "cost_usd", "input", "output", "cache_read", "cache_write"):
        assert len(opus[field]) == 7, field
    # both rows land on distinct dense buckets, no double counting
    i18 = resp["buckets"].index("2026-09-18")
    i19 = resp["buckets"].index("2026-09-19")
    assert opus["input"][i18] == 100
    assert opus["input"][i19] == 100
    assert opus["cost_usd"][i18] == pytest.approx((100 * 3.0 + 50 * 15.0) / 1_000_000, rel=1e-6)
    # untouched buckets are real zeros, not missing
    other_idx = [i for i in range(7) if i not in (i18, i19)]
    for i in other_idx:
        assert opus["tokens"][i] == 0
        assert opus["cost_usd"][i] == 0.0


def test_group_by_provider_kimi_cost_is_always_null(tmp_store):
    tmp_store.insert_usage_events([_local_row("2026-09-19T10:00:00Z", "claude-opus-5")])
    tmp_store.insert_kimi_turn_events([_kimi_row("2026-09-19T11:00:00Z", 1758277200000, tokens=321)])

    since, until = parse_window("7d", NOW)
    resp = build_grouped_history(tmp_store, PRICING, "7d", "day", "provider", since, until)

    claude = next(s for s in resp["series"] if s["key"] == "claude")
    kimi = next(s for s in resp["series"] if s["key"] == "kimi")
    assert all(c is None for c in kimi["cost_usd"])
    assert sum(kimi["tokens"]) == 321
    assert any(c is not None for c in claude["cost_usd"])
    # totals.cost_usd is explicitly claude-only and labelled as such
    assert resp["totals"]["cost_usd_basis"] == "claude_only"
    i19 = resp["buckets"].index("2026-09-19")
    assert resp["totals"]["cost_usd"] == pytest.approx(claude["cost_usd"][i19])
    assert resp["totals"]["tokens"] == sum(claude["tokens"]) + sum(kimi["tokens"])


def test_group_by_host_mixes_providers_cost_is_claude_only_real_number(tmp_store):
    tmp_store.insert_usage_events([_local_row("2026-09-19T10:00:00Z", "claude-opus-5", host="localhost")])
    tmp_store.insert_kimi_turn_events([
        _kimi_row("2026-09-19T11:00:00Z", 1758277200000, host="localhost", tokens=42),
    ])
    tmp_store.upsert_remote_usage_buckets([_remote_row("2026-09-19T09", "claude-sonnet-5", "host-b")])

    since, until = parse_window("7d", NOW)
    resp = build_grouped_history(tmp_store, PRICING, "7d", "day", "host", since, until)

    localhost = next(s for s in resp["series"] if s["key"] == "localhost")
    host_b = next(s for s in resp["series"] if s["key"] == "host-b")
    assert localhost["provider"] is None  # mixed claude+kimi, no single provider label
    i19 = resp["buckets"].index("2026-09-19")
    # tokens include BOTH providers for localhost
    assert localhost["tokens"][i19] == (100 + 50) + 42
    # cost_usd is real (never null) and is the claude-only portion
    assert localhost["cost_usd"][i19] is not None
    assert localhost["cost_usd"][i19] == pytest.approx((100 * 3.0 + 50 * 15.0) / 1_000_000, rel=1e-6)
    # a host with zero claude activity anywhere still has real 0.0s, not null
    assert all(c is not None for c in localhost["cost_usd"])
    assert host_b["cost_usd"][i19] > 0


def test_group_by_host_hour_bucket_today(tmp_store):
    tmp_store.insert_usage_events([_local_row("2026-09-20T09:15:00Z", "claude-opus-5")])
    since, until = parse_window("today", NOW)
    resp = build_grouped_history(tmp_store, PRICING, "today", "hour", "host", since, until)
    assert resp["bucket"] == "hour"
    assert len(resp["buckets"]) == 13  # hours 00..12 inclusive
    localhost = next(s for s in resp["series"] if s["key"] == "localhost")
    assert sum(localhost["tokens"]) == 150


# -- no double counting -------------------------------------------------------


def test_no_double_counting_local_and_remote_hosts_are_disjoint(tmp_store):
    tmp_store.insert_usage_events([_local_row("2026-09-19T10:00:00Z", "claude-opus-5", host="localhost")])
    tmp_store.upsert_remote_usage_buckets([
        _remote_row("2026-09-19T10", "claude-opus-5", "host-b", tokens_in=200, tokens_out=100),
        _remote_row("2026-09-19T10", "claude-opus-5", "host-c", tokens_in=300, tokens_out=150),
    ])
    since, until = parse_window("7d", NOW)
    resp = build_grouped_history(tmp_store, PRICING, "7d", "day", "model", since, until)
    opus = next(s for s in resp["series"] if s["key"] == "claude-opus-5")
    i19 = resp["buckets"].index("2026-09-19")
    # 150 (local) + 300 (host-b) + 450 (host-c) = 900, each counted exactly once
    assert opus["tokens"][i19] == 150 + 300 + 450
    assert resp["totals"]["tokens"] == 900


def test_no_double_counting_kimi_local_and_remote_are_disjoint(tmp_store):
    tmp_store.insert_kimi_turn_events([
        _kimi_row("2026-09-19T10:00:00Z", 1758276000000, host="localhost", tokens=10),
    ])
    tmp_store.upsert_remote_kimi_usage_buckets([
        _remote_kimi_row("2026-09-19", "kimi-code/kimi-for-coding", "host-b", tokens=20),
    ])
    since, until = parse_window("7d", NOW)
    resp = build_grouped_history(tmp_store, PRICING, "7d", "day", "provider", since, until)
    kimi = next(s for s in resp["series"] if s["key"] == "kimi")
    assert sum(kimi["tokens"]) == 30
    assert resp["totals"]["tokens"] == 30


# -- top-N capping -------------------------------------------------------------


def test_group_by_model_caps_series_and_folds_other(tmp_store):
    rows = []
    for i in range(15):
        rows.append(_local_row("2026-09-19T10:00:00Z", f"model-{i:02d}", tokens_in=100 - i, tokens_out=0))
    tmp_store.insert_usage_events(rows)
    since, until = parse_window("7d", NOW)
    resp = build_grouped_history(tmp_store, PRICING, "7d", "day", "model", since, until)

    assert resp["series_capped"] is True
    assert len(resp["series"]) == 11  # top 10 + "other"
    other = next(s for s in resp["series"] if s["key"] == "other")
    assert other["is_other"] is True
    assert other["folded_series_count"] == 5
    # totals still reflect ALL 15 models, not just the kept 10
    assert resp["totals"]["tokens"] == sum(100 - i for i in range(15))
    # the folded models are the 5 smallest by tokens (model-10..model-14)
    kept_keys = {s["key"] for s in resp["series"] if s["key"] != "other"}
    assert kept_keys == {f"model-{i:02d}" for i in range(10)}


def test_group_by_provider_never_caps_with_two_providers(tmp_store):
    tmp_store.insert_usage_events([_local_row("2026-09-19T10:00:00Z", "claude-opus-5")])
    tmp_store.insert_kimi_turn_events([_kimi_row("2026-09-19T10:00:00Z", 1758276000000)])
    since, until = parse_window("7d", NOW)
    resp = build_grouped_history(tmp_store, PRICING, "7d", "day", "provider", since, until)
    assert resp["series_capped"] is False
    assert {s["key"] for s in resp["series"]} == {"claude", "kimi"}


# -- coverage metadata ---------------------------------------------------------


def test_coverage_reflects_fixture_first_data_dates(tmp_store):
    # Mirrors the briefing's real dates: localhost's local Claude data starts
    # 2026-07-13, a remote host (host-b) starts earlier (2026-06-14), and
    # the requested window (90d back from 2026-09-20) starts 2026-06-22 --
    # BEFORE localhost's own first_data, so localhost must come back partial.
    tmp_store.insert_usage_events([_local_row("2026-07-13T17:55:10Z", "claude-opus-5")])
    tmp_store.insert_usage_events([
        _local_row("2026-09-19T10:00:00Z", "claude-opus-5", message_id="m-recent"),
    ])
    tmp_store.upsert_remote_usage_buckets([_remote_row("2026-06-14T00", "claude-opus-5", "host-b")])

    since, until = parse_window("90d", NOW)
    resp = build_grouped_history(tmp_store, PRICING, "90d", "day", "host", since, until)

    cov = resp["coverage"]
    assert cov["requested_from"] == since.strftime("%Y-%m-%d")
    assert cov["requested_to"] == until.strftime("%Y-%m-%d")

    localhost_cov = next(s for s in cov["series"] if s["key"] == "localhost")
    assert localhost_cov["first_data"] == "2026-07-13"
    assert localhost_cov["complete_from"] == "2026-07-13"
    assert localhost_cov["partial"] is True

    host_b_cov = next(s for s in cov["series"] if s["key"] == "host-b")
    assert host_b_cov["first_data"] == "2026-06-14"
    # host-b's data predates the requested window, so it is complete for the
    # ENTIRE requested range -- complete_from clamps to requested_from, not
    # host-b's own (earlier) first_data.
    assert host_b_cov["complete_from"] == cov["requested_from"]
    assert host_b_cov["partial"] is False

    # fleet_complete_from is gated by the LATEST-starting series (localhost)
    assert cov["fleet_complete_from"] == "2026-07-13"


def test_coverage_series_never_fabricates_zeros_before_first_data(tmp_store):
    # A series with no data at all before its first row must not silently
    # report zeros for buckets before that row as if they were observed --
    # the coverage block is exactly what lets a client tell the difference,
    # and this test checks the underlying data (the zeros ARE genuinely 0 in
    # the array) is at least accompanied by an accurate first_data marker.
    tmp_store.insert_usage_events([_local_row("2026-09-18T10:00:00Z", "claude-opus-5")])
    since, until = parse_window("7d", NOW)
    resp = build_grouped_history(tmp_store, PRICING, "7d", "day", "host", since, until)
    localhost_cov = next(s for s in resp["coverage"]["series"] if s["key"] == "localhost")
    assert localhost_cov["first_data"] == "2026-09-18"
    assert localhost_cov["partial"] is True
    localhost = next(s for s in resp["series"] if s["key"] == "localhost")
    pre_first_data_idx = resp["buckets"].index("2026-09-14")
    assert localhost["tokens"][pre_first_data_idx] == 0


# -- timezone day-bucketing (briefing Task 4) ---------------------------------
# "Etc/GMT+6" is a fixed UTC-6 offset (note the POSIX sign flip) -- no DST, so
# these tests are deterministic regardless of calendar date.


def test_dense_bucket_keys_day_shifts_with_timezone():
    end = datetime(2026, 9, 21, 2, 0, 0, tzinfo=UTC)  # 2am UTC on the 21st
    assert dense_bucket_keys(end, "day", 1) == ["2026-09-21"]
    # UTC-6 local time is 2026-09-20T20:00 -- still the 20th.
    assert dense_bucket_keys(end, "day", 1, "Etc/GMT+6") == ["2026-09-20"]


def test_fold_hour_rows_to_local_day_groups_by_local_calendar_day():
    rows = [
        {"bucket": "2026-09-20T23:00:00Z", "host": "h", "model": "m", "tokens": 10, "messages": 1},
        {"bucket": "2026-09-21T01:00:00Z", "host": "h", "model": "m", "tokens": 5, "messages": 1},
    ]
    utc_folded = fold_hour_rows_to_local_day(rows, "UTC", key_cols=("host", "model"))
    assert {r["bucket"] for r in utc_folded} == {"2026-09-20", "2026-09-21"}

    # Both hours (23:00 and next-day 01:00 UTC) fall on the same UTC-6
    # calendar day (17:00 and 19:00 the same evening), so they fold together.
    tz_folded = fold_hour_rows_to_local_day(rows, "Etc/GMT+6", key_cols=("host", "model"))
    assert {r["bucket"] for r in tz_folded} == {"2026-09-20"}
    assert tz_folded[0]["tokens"] == 15


def test_usage_timeline_daily_tz_shifts_day_boundary(tmp_store):
    # 2am UTC on the 20th is still evening of the 19th at UTC-6 -- the exact
    # "user in UTC-6 sees days split at 18:00 (i.e. their evening bleeds into
    # the next UTC day)" bug the briefing calls out.
    tmp_store.insert_usage_events([_local_row("2026-09-20T02:00:00Z", "claude-opus-5")])

    utc_rows = tmp_store.usage_timeline_daily_tz("2026-09-18T00:00:00Z", "UTC")
    tz_rows = tmp_store.usage_timeline_daily_tz("2026-09-18T00:00:00Z", "Etc/GMT+6")

    assert any(r["bucket"] == "2026-09-20" for r in utc_rows)
    assert not any(r["bucket"] == "2026-09-20" for r in tz_rows)
    assert any(r["bucket"] == "2026-09-19" for r in tz_rows)


def test_grouped_history_day_bucket_moves_with_timezone(tmp_store):
    tmp_store.insert_usage_events([
        _local_row("2026-09-20T02:00:00Z", "claude-opus-5", tokens_in=100, tokens_out=50),
    ])
    since, until = parse_window("7d", NOW)  # NOW = 2026-09-20T12:00:00Z

    utc_resp = build_grouped_history(tmp_store, PRICING, "7d", "day", "host", since, until, "UTC")
    tz_resp = build_grouped_history(tmp_store, PRICING, "7d", "day", "host", since, until, "Etc/GMT+6")

    localhost_utc = next(s for s in utc_resp["series"] if s["key"] == "localhost")
    localhost_tz = next(s for s in tz_resp["series"] if s["key"] == "localhost")

    i_utc_20 = utc_resp["buckets"].index("2026-09-20")
    i_tz_19 = tz_resp["buckets"].index("2026-09-19")
    i_tz_20 = tz_resp["buckets"].index("2026-09-20")

    assert localhost_utc["tokens"][i_utc_20] == 150
    assert localhost_tz["tokens"][i_tz_19] == 150
    assert localhost_tz["tokens"][i_tz_20] == 0
