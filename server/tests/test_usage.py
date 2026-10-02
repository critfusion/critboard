import json
from datetime import UTC, datetime, timedelta

import pytest

from critdash.collectors.usage import (
    UsageCollector,
    extract_usage_row,
    parse_jsonl_bytes,
    project_name_from_dir,
)
from critdash.store import zero_fill_daily, zero_fill_hourly


def _rate(input_, output, cw5m, cw1h, cache_read):
    return {
        "input": input_, "output": output,
        "cache_write_5m": cw5m, "cache_write_1h": cw1h, "cache_read": cache_read,
    }


PRICING = {
    "models": {
        "claude-opus-5": _rate(5.0, 25.0, 6.25, 10.0, 0.50),
        "default": _rate(3.0, 15.0, 3.75, 6.0, 0.30),
    },
    "fast_mode": {},
    "monthly_budget_usd": 2000,
}


def _simple_pricing():
    return {
        "models": {"default": _rate(3.0, 15.0, 3.75, 6.0, 0.3)},
        "fast_mode": {}, "monthly_budget_usd": 2000.0,
    }


def test_parse_jsonl_bytes_skips_malformed(fixtures_dir):
    data = (fixtures_dir / "usage_malformed.jsonl").read_bytes()
    docs = parse_jsonl_bytes(data)
    assert docs == []  # malformed line must not raise, just be skipped


def test_parse_jsonl_bytes_empty_file(fixtures_dir):
    data = (fixtures_dir / "usage_empty.jsonl").read_bytes()
    assert parse_jsonl_bytes(data) == []


def test_parse_jsonl_bytes_mixed(fixtures_dir):
    data = (fixtures_dir / "usage_mixed.jsonl").read_bytes()
    docs = parse_jsonl_bytes(data)
    # real + synthetic parse fine; the malformed line in the mix is dropped
    assert len(docs) == 2


def test_real_assistant_row_extracted(fixtures_dir):
    line = (fixtures_dir / "usage_real_assistant.jsonl").read_bytes()
    doc = json.loads(line)
    row = extract_usage_row(doc, "demo-app", "/home/user/work/demo-app", PRICING)
    assert row is not None
    assert row["model"] == "claude-opus-5"
    assert row["message_id"] == "msg_011CfAvsXNareSej2iKBixBi"
    assert row["input"] == 2
    assert row["output"] == 449
    assert row["cache_read"] == 23978
    assert row["cache_write_5m"] == 0
    assert row["cache_write_1h"] == 29923
    assert row["speed"] == "standard"
    assert row["cost_usd"] > 0
    assert row["is_sidechain"] == 0
    rate = PRICING["models"]["claude-opus-5"]
    expected = (2 / 1e6 * rate["input"]) + (449 / 1e6 * rate["output"]) + (
        23978 / 1e6 * rate["cache_read"]
    ) + (29923 / 1e6 * rate["cache_write_1h"])
    assert abs(row["cost_usd"] - expected) < 1e-9


def test_synthetic_row_skipped(fixtures_dir):
    line = (fixtures_dir / "usage_synthetic.jsonl").read_bytes()
    doc = json.loads(line)
    assert doc["message"]["model"] == "<synthetic>"
    row = extract_usage_row(doc, "demo-app", "/home/user/work/demo-app", PRICING)
    assert row is None


def test_malformed_json_line_does_not_raise(fixtures_dir):
    data = (fixtures_dir / "usage_malformed.jsonl").read_bytes()
    # the collector-level contract: parse_jsonl_bytes must swallow the error
    docs = parse_jsonl_bytes(data)
    for doc in docs:
        extract_usage_row(doc, "p", "/p", PRICING)  # should never raise


def test_non_assistant_type_skipped():
    doc = {"type": "user", "message": {"model": "claude-opus-5", "usage": {}}}
    assert extract_usage_row(doc, "p", "/p", PRICING) is None


def test_missing_usage_skipped():
    doc = {"type": "assistant", "message": {"model": "claude-opus-5", "id": "msg_1"}, "timestamp": "t"}
    assert extract_usage_row(doc, "p", "/p", PRICING) is None


def test_project_name_from_dir_simple(tmp_path, monkeypatch):
    # no filesystem match -> naive '-' -> '/' decode. The repo name here is
    # deliberately hyphen-free (unlike "demo-app" elsewhere in this file) --
    # naive decode splits on every '-', so a hyphenated name would come back
    # wrong here on purpose; test_project_name_from_dir_hyphenated_repo below
    # covers the greedy-match case that DOES preserve internal hyphens.
    name, path = project_name_from_dir("-home-user-work-democli")
    assert name == "democli"
    assert path == "/home/user/work/democli"


def test_project_name_from_dir_hyphenated_repo(tmp_path):
    # simulate a real repo dir with a hyphen in its own name, and confirm the
    # greedy longest-existing-dir match finds it instead of splitting on every '-'
    (tmp_path / "srv" / "demo" / "repos" / "example-multi-word-repo").mkdir(parents=True)
    import critdash.collectors.usage as usage_mod

    usage_mod._PROJECT_DIR_CACHE.clear()
    orig_isdir = usage_mod.os.path.isdir

    def fake_isdir(p):
        # redirect absolute-looking probes into tmp_path
        if p.startswith("/"):
            return orig_isdir(str(tmp_path) + p)
        return orig_isdir(p)

    usage_mod.os.path.isdir = fake_isdir
    try:
        name, path = project_name_from_dir("-srv-demo-repos-example-multi-word-repo")
    finally:
        usage_mod.os.path.isdir = orig_isdir
    assert name == "example-multi-word-repo"
    assert path == "/srv/demo/repos/example-multi-word-repo"


def test_dedupe_by_message_id(tmp_store):
    ts = "2026-09-18T12:00:00Z"
    row = dict(
        message_id="msg_dupe", ts=ts, session_id="s1", project="p", project_path="/p",
        model="claude-opus-5", input=10, output=10, cache_read=0, cache_write_5m=0,
        cache_write_1h=0, speed=None, is_sidechain=0, web_searches=0, cost_usd=0.001,
    )
    inserted1 = tmp_store.insert_usage_events([row, row])  # same id twice in one batch
    inserted2 = tmp_store.insert_usage_events([row])  # again in a later batch
    assert inserted1 == 1
    assert inserted2 == 0
    totals = tmp_store.usage_totals()
    assert totals["messages"] == 1


# Note: the collector-level "5h rate-limit block" reconstruction (formerly
# store.current_usage_block_start()/BLOCK_DURATION, and the quota.py card row
# it fed) was removed entirely -- it was a local ccusage-style reconstruction
# across every provider/host mixed together, presented as if it were the
# account's real rate-limit window, which it cannot be.


# -- usage.budget -------------------------------------------------------


def _row_now(message_id: str, cost_usd: float) -> dict:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dict(
        message_id=message_id, ts=ts, session_id="s1", project="p", project_path="/p",
        model="claude-opus-5", input=1, output=1, cache_read=0, cache_write_5m=0,
        cache_write_1h=0, speed=None, is_sidechain=0, web_searches=0, cost_usd=cost_usd,
    )


def test_budget_math(tmp_store):
    tmp_store.insert_usage_events([_row_now("b1", 12.5), _row_now("b2", 8.05)])
    collector = UsageCollector(store=tmp_store, pricing={
        "models": {"default": _rate(3.0, 15.0, 3.75, 6.0, 0.3)},
        "fast_mode": {}, "monthly_budget_usd": 2000.0,
    })
    data = collector._compute_rollups()
    budget = data["budget"]
    assert budget["monthly_usd"] == 2000.0
    assert budget["spent_mtd_usd"] == pytest.approx(20.55, abs=1e-6)
    assert budget["pct"] == round(20.55 / 2000.0, 4)  # budget.pct is rounded to 4dp
    # budget.projected_month_usd is spent-to-date plus the rest of the month
    # at the current rate -- a different, independent computation from
    # burn.projected_month_usd (a naive full-month extrapolation).
    assert budget["projected_month_usd"] >= budget["spent_mtd_usd"]


# -- usage.budget.projected_month_usd (month-end projection fix) --------


def _rollups_with_mtd_and_recent_burn(tmp_store, mtd_cost_usd: float, recent_cost_usd: float,
                                       monthly_budget_usd: float = 2000.0):
    """Insert `mtd_cost_usd` spread across the start of the current UTC month
    (old enough to be outside the 24h burn window) plus `recent_cost_usd`
    within the last hour (inside the 24h burn window), then compute rollups."""
    now = datetime.now(UTC)
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    old_ts = max(month_start, now - timedelta(hours=30))
    rows = []
    if mtd_cost_usd:
        rows.append(dict(
            message_id="mtd1", ts=old_ts.strftime("%Y-%m-%dT%H:%M:%SZ"), session_id="s1",
            project="p", project_path="/p", model="claude-opus-5", input=1, output=1,
            cache_read=0, cache_write_5m=0, cache_write_1h=0, speed=None, is_sidechain=0,
            web_searches=0, cost_usd=mtd_cost_usd,
        ))
    if recent_cost_usd:
        recent_ts = now - timedelta(minutes=5)
        rows.append(dict(
            message_id="recent1", ts=recent_ts.strftime("%Y-%m-%dT%H:%M:%SZ"), session_id="s1",
            project="p", project_path="/p", model="claude-opus-5", input=1, output=1,
            cache_read=0, cache_write_5m=0, cache_write_1h=0, speed=None, is_sidechain=0,
            web_searches=0, cost_usd=recent_cost_usd,
        ))
    if rows:
        tmp_store.insert_usage_events(rows)
    collector = UsageCollector(store=tmp_store, pricing={
        "models": {"default": _rate(3.0, 15.0, 3.75, 6.0, 0.3)},
        "fast_mode": {}, "monthly_budget_usd": monthly_budget_usd,
    })
    return collector._compute_rollups()


def test_budget_projection_always_gte_spent_mtd(tmp_store):
    # normal mid-month case: some MTD spend, some recent burn
    data = _rollups_with_mtd_and_recent_burn(tmp_store, mtd_cost_usd=50.0, recent_cost_usd=2.0)
    budget = data["budget"]
    assert budget["projected_month_usd"] >= budget["spent_mtd_usd"]


def test_budget_projection_converges_to_spent_when_month_effectively_over(tmp_store, monkeypatch):
    # freeze "now" to the last few seconds of the month so hours_remaining ~ 0
    import critdash.collectors.usage as usage_mod

    fake_now = datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    monkeypatch.setattr(usage_mod, "datetime", _FixedDatetime)

    row = dict(
        message_id="eom1", ts="2026-09-30T20:00:00Z", session_id="s1", project="p",
        project_path="/p", model="claude-opus-5", input=1, output=1, cache_read=0,
        cache_write_5m=0, cache_write_1h=0, speed=None, is_sidechain=0, web_searches=0,
        cost_usd=100.0,
    )
    tmp_store.insert_usage_events([row])
    collector = UsageCollector(store=tmp_store, pricing={
        "models": {"default": _rate(3.0, 15.0, 3.75, 6.0, 0.3)},
        "fast_mode": {}, "monthly_budget_usd": 2000.0,
    })
    data = collector._compute_rollups()
    budget = data["budget"]
    # ~1 second left in the month: projection must be within rounding of spend
    assert budget["projected_month_usd"] == pytest.approx(budget["spent_mtd_usd"], abs=0.01)


def test_budget_projection_zero_burn_equals_spent_mtd(tmp_store):
    # MTD spend happened >24h ago (outside the burn window) -> zero recent
    # burn rate -> nothing to extrapolate, projection collapses to actual spend
    data = _rollups_with_mtd_and_recent_burn(tmp_store, mtd_cost_usd=50.0, recent_cost_usd=0.0)
    budget = data["budget"]
    assert budget["spent_mtd_usd"] == pytest.approx(50.0, abs=1e-6)
    assert budget["projected_month_usd"] == pytest.approx(50.0, abs=1e-6)


def test_budget_projection_exceeds_mtd_for_high_spend_low_recent_burn(tmp_store):
    # this is the old-bug scenario: heavy spend earlier in the month, quiet
    # recently -> naive burn.projected_month_usd can fall below spent_mtd_usd,
    # but budget.projected_month_usd must not.
    data = _rollups_with_mtd_and_recent_burn(tmp_store, mtd_cost_usd=4156.57, recent_cost_usd=0.01)
    budget = data["budget"]
    assert budget["projected_month_usd"] >= budget["spent_mtd_usd"]
    # reproduces the reported bug: naive burn projection was below MTD actual
    assert data["burn"]["projected_month_usd"] < budget["spent_mtd_usd"]


def test_budget_pct_zero_when_monthly_budget_is_zero(tmp_store):
    tmp_store.insert_usage_events([_row_now("b1", 5.0)])
    collector = UsageCollector(store=tmp_store, pricing={
        "models": {"default": _rate(3.0, 15.0, 3.75, 6.0, 0.3)},
        "fast_mode": {}, "monthly_budget_usd": 0,
    })
    data = collector._compute_rollups()
    assert data["budget"]["pct"] == 0.0  # must not raise ZeroDivisionError


# -- historical re-costing (store.recost_all) -----------------------------


def test_recost_all_rewrites_cost_from_stored_token_columns(tmp_store):
    # simulate a row ingested under a stale/wrong pricing table
    row = dict(
        message_id="stale1", ts="2026-09-18T00:00:00Z", session_id="s1", project="p",
        project_path="/p", model="claude-opus-5", input=1_000_000, output=1_000_000,
        cache_read=0, cache_write_5m=0, cache_write_1h=0, speed=None,
        is_sidechain=0, web_searches=0, cost_usd=999.0,
    )
    tmp_store.insert_usage_events([row])

    def cost_fn(model, inp, out, cache_read, cw5m, cw1h, speed):
        assert model == "claude-opus-5"
        return inp / 1_000_000 * 5.0 + out / 1_000_000 * 25.0  # corrected opus rate

    updated = tmp_store.recost_all(cost_fn)
    assert updated == 1
    totals = tmp_store.usage_totals()
    assert round(totals["cost_usd"], 2) == 30.0  # not the stale 999.0


# -- store.zero_fill_hourly / zero_fill_daily (pure function unit tests) ----


def test_zero_fill_hourly_dense_length_and_boundaries():
    end = datetime(2026, 9, 18, 15, 30, tzinfo=UTC)  # mid-hour; must floor to :00
    out = zero_fill_hourly([], end, 5)
    assert [p["t"] for p in out] == [
        "2026-09-18T11:00:00Z", "2026-09-18T12:00:00Z", "2026-09-18T13:00:00Z",
        "2026-09-18T14:00:00Z", "2026-09-18T15:00:00Z",
    ]
    for p in out:
        assert (p["input"], p["output"], p["cache_read"], p["cache_write"], p["cost_usd"], p["messages"]) == \
            (0, 0, 0, 0, 0.0, 0)


def test_zero_fill_hourly_preserves_real_rows_and_fills_gaps():
    end = datetime(2026, 9, 18, 3, 0, tzinfo=UTC)
    rows = [
        {"bucket": "2026-09-18T01:00:00Z", "input": 5, "output": 7, "cache_read": 1,
         "cache_write": 2, "cost_usd": 0.5, "messages": 3},
    ]
    out = zero_fill_hourly(rows, end, 4)  # 00:00, 01:00, 02:00, 03:00
    assert [p["t"] for p in out] == [
        "2026-09-18T00:00:00Z", "2026-09-18T01:00:00Z", "2026-09-18T02:00:00Z", "2026-09-18T03:00:00Z",
    ]
    assert out[0]["input"] == 0  # filled gap
    assert out[1] == {"t": "2026-09-18T01:00:00Z", "input": 5, "output": 7, "cache_read": 1,
                       "cache_write": 2, "cost_usd": 0.5, "messages": 3}
    assert out[2]["input"] == 0  # filled gap


def test_zero_fill_daily_dense_length_and_boundaries():
    end = datetime(2026, 9, 18, 23, 0, tzinfo=UTC)
    out = zero_fill_daily([], end, 7)
    assert [p["t"] for p in out] == [
        "2026-09-12", "2026-09-13", "2026-09-14", "2026-09-15",
        "2026-09-16", "2026-09-17", "2026-09-18",
    ]


def test_zero_fill_empty_dataset_never_returns_empty_list():
    assert len(zero_fill_hourly([], datetime.now(UTC), 24)) == 24
    assert len(zero_fill_daily([], datetime.now(UTC), 7)) == 7


# -- usage.timeline zero-fill (Fix 1: sparse hours must not compress away) --


def test_timeline_sparse_dataset_is_fully_dense_48h(tmp_store):
    now = datetime.now(UTC)
    # only 3 hours have data, scattered across the 48h window -- the fleet
    # was genuinely idle in between (per SPEC this must render as flat zero,
    # not be compressed into a 3-point line).
    for i, hours_ago in enumerate([47, 24, 0]):
        ts = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp_store.insert_usage_events([dict(
            message_id=f"sparse{i}", ts=ts, session_id="s1", project="p", project_path="/p",
            model="claude-opus-5", input=10, output=10, cache_read=0, cache_write_5m=0,
            cache_write_1h=0, speed=None, is_sidechain=0, web_searches=0, cost_usd=0.01,
        )])
    collector = UsageCollector(store=tmp_store, pricing={
        "models": {"default": _rate(3.0, 15.0, 3.75, 6.0, 0.3)},
        "fast_mode": {}, "monthly_budget_usd": 2000.0,
    })
    timeline = collector._compute_rollups()["timeline"]
    assert len(timeline) == 48
    # contiguous, no gaps or duplicates: each bucket exactly 1h after the last
    ts_list = [datetime.fromisoformat(p["t"].replace("Z", "+00:00")) for p in timeline]
    assert ts_list == sorted(ts_list)
    assert len(set(ts_list)) == 48
    for a, b in zip(ts_list, ts_list[1:], strict=False):
        assert (b - a) == timedelta(hours=1)
    # every bucket carries the full zero-row shape even when it has no data
    nonzero_buckets = [p for p in timeline if p["input"] or p["output"] or p["cost_usd"]]
    assert len(nonzero_buckets) == 3
    for p in timeline:
        for key in ("input", "output", "cache_read", "cache_write", "cost_usd", "messages"):
            assert key in p


def test_timeline_empty_dataset_yields_dense_zero_buckets_not_empty_list(tmp_store):
    collector = UsageCollector(store=tmp_store, pricing={
        "models": {"default": _rate(3.0, 15.0, 3.75, 6.0, 0.3)},
        "fast_mode": {}, "monthly_budget_usd": 2000.0,
    })
    timeline = collector._compute_rollups()["timeline"]
    assert len(timeline) == 48
    for p in timeline:
        assert p["input"] == 0
        assert p["output"] == 0
        assert p["cache_read"] == 0
        assert p["cache_write"] == 0
        assert p["cost_usd"] == 0
        assert p["messages"] == 0


def test_timeline_no_store_still_yields_48_zero_buckets():
    # defensive default path (no persistence layer at all) must uphold the
    # same "always 48 contiguous buckets" contract as the normal path
    collector = UsageCollector(store=None, pricing={
        "models": {"default": _rate(3.0, 15.0, 3.75, 6.0, 0.3)},
        "fast_mode": {}, "monthly_budget_usd": 2000.0,
    })
    timeline = collector._compute_rollups()["timeline"]
    assert len(timeline) == 48


def test_timeline_ends_at_current_hour(tmp_store):
    collector = UsageCollector(store=tmp_store, pricing={
        "models": {"default": _rate(3.0, 15.0, 3.75, 6.0, 0.3)},
        "fast_mode": {}, "monthly_budget_usd": 2000.0,
    })
    timeline = collector._compute_rollups()["timeline"]
    last = datetime.fromisoformat(timeline[-1]["t"].replace("Z", "+00:00"))
    current_hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    assert last == current_hour


def test_recost_all_passes_stored_speed_through(tmp_store):
    row = dict(
        message_id="fastrow", ts="2026-09-18T00:00:00Z", session_id="s1", project="p",
        project_path="/p", model="claude-opus-5", input=1_000_000, output=0,
        cache_read=0, cache_write_5m=0, cache_write_1h=0, speed="fast",
        is_sidechain=0, web_searches=0, cost_usd=0.0,
    )
    tmp_store.insert_usage_events([row])
    seen_speeds = []

    def cost_fn(model, inp, out, cache_read, cw5m, cw1h, speed):
        seen_speeds.append(speed)
        return 1.0

    tmp_store.recost_all(cost_fn)
    assert seen_speeds == ["fast"]


# -- multi-host: host column, composite dedupe, remote buckets, union rollup -


def _local_row(message_id, host="localhost", model="claude-opus-5", output=10, cost=1.0, ts=None):
    return dict(
        message_id=message_id, ts=ts or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        session_id="s1", project="p", project_path="/p", model=model, input=0, output=output,
        cache_read=0, cache_write_5m=0, cache_write_1h=0, speed=None, is_sidechain=0,
        web_searches=0, cost_usd=cost, host=host,
    )


def test_host_column_defaults_to_localhost_for_untagged_rows(tmp_store):
    # a caller that doesn't set "host" (every pre-existing call site in this
    # test file) must land as 'localhost', not raise or leave it null.
    row = dict(
        message_id="untagged", ts="2026-09-18T00:00:00Z", session_id="s1", project="p",
        project_path="/p", model="claude-opus-5", input=1, output=1, cache_read=0,
        cache_write_5m=0, cache_write_1h=0, speed=None, is_sidechain=0, web_searches=0, cost_usd=0.1,
    )
    tmp_store.insert_usage_events([row])
    assert tmp_store.usage_totals(host="localhost")["messages"] == 1
    assert tmp_store.usage_totals(host="host-b")["messages"] == 0


def test_dedupe_key_is_host_plus_message_id_not_message_id_alone(tmp_store):
    # two DISTINCT hosts legitimately sharing a message_id must both be kept
    # -- this is the SPEC requirement that dedupe becomes (host, message_id).
    inserted_a = tmp_store.insert_usage_events([_local_row("shared-id", host="localhost")])
    inserted_b = tmp_store.insert_usage_events([_local_row("shared-id", host="host-b")])
    assert inserted_a == 1
    assert inserted_b == 1
    assert tmp_store.usage_totals()["messages"] == 2
    # but the SAME host repeating the SAME message_id is still deduped
    inserted_c = tmp_store.insert_usage_events([_local_row("shared-id", host="localhost")])
    assert inserted_c == 0


def test_host_column_migration_on_pre_existing_db(tmp_path):
    import sqlite3

    from critdash.store import Store

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT UNIQUE NOT NULL,
            ts TEXT NOT NULL, session_id TEXT, project TEXT, project_path TEXT, model TEXT,
            input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
            cache_read INTEGER NOT NULL DEFAULT 0, cache_write_5m INTEGER NOT NULL DEFAULT 0,
            cache_write_1h INTEGER NOT NULL DEFAULT 0, is_sidechain INTEGER NOT NULL DEFAULT 0,
            web_searches INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0.0
        )
    """)  # a schema from before both the `speed` and `host` migrations existed
    conn.execute(
        "INSERT INTO usage_events (message_id, ts, cost_usd) VALUES ('old1', '2026-09-01T00:00:00Z', 2.5)"
    )
    conn.commit()
    conn.close()

    store = Store(db_path)
    try:
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(usage_events)")}
        assert "host" in cols
        assert "speed" in cols
        row = store._conn.execute("SELECT host FROM usage_events WHERE message_id = 'old1'").fetchone()
        assert row["host"] == "localhost"  # pre-existing rows backfilled to the local default
        assert store.usage_totals(host="localhost")["cost_usd"] == 2.5
    finally:
        store.close()


def test_remote_usage_grouped_rejects_unknown_group_column(tmp_store):
    with pytest.raises(ValueError):
        tmp_store.remote_usage_grouped(("bogus",))


def test_upsert_remote_usage_buckets_replaces_not_accumulates(tmp_store):
    bucket = dict(host="host-b", hour="2026-09-18T12", model="claude-opus-5", project="p",
                   input=10, output=20, cache_read=0, cache_write_5m=0, cache_write_1h=0, messages=1)
    tmp_store.upsert_remote_usage_buckets([bucket])
    updated = {**bucket, "output": 999, "messages": 5}
    tmp_store.upsert_remote_usage_buckets([updated])
    rows = tmp_store.remote_usage_grouped(("host",), host="host-b")
    assert rows[0]["output"] == 999  # replaced, not 20 + 999
    assert rows[0]["messages"] == 5


def test_by_host_rollup_includes_host_with_zero_today_usage(tmp_store):
    """Bug 1 regression at the rollup layer: a host with usage entirely in
    the past (nothing in the current hour) has a legitimate tokens_today of
    0, but must still appear in usage.by_host for windows that cover its
    history ("7d"/"30d"/"all") -- zero-today must never be conflated with
    zero-ever."""
    old_hour = "2026-06-01T09"
    tmp_store.upsert_remote_usage_buckets([dict(
        host="host-c", hour=old_hour, model="claude-sonnet-4-6", project="sample-project",
        input=5, output=100, cache_read=0, cache_write_5m=0, cache_write_1h=0, messages=3,
    )])

    pricing = {
        "models": {"default": _rate(3.0, 15.0, 3.75, 6.0, 0.30)},
        "fast_mode": {}, "monthly_budget_usd": 2000,
    }
    collector = UsageCollector(store=tmp_store, pricing=pricing, host="localhost")
    rollups = collector._compute_rollups()

    by_host_today = {r["host"]: r for r in rollups["by_host"] if r["window"] == "today"}
    assert "host-c" not in by_host_today  # correct: no activity today

    by_host_all = {r["host"]: r for r in rollups["by_host"] if r["window"] == "all"}
    assert by_host_all["host-c"]["total"] == 105  # 5 + 100, present in the all-time window


def test_union_rollup_combines_local_and_remote_without_double_counting(tmp_store):
    now = datetime.now(UTC)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    hour = now.strftime("%Y-%m-%dT%H")

    tmp_store.insert_usage_events([_local_row("local1", host="localhost", model="claude-opus-5",
                                               output=100, cost=1.0, ts=ts)])
    tmp_store.upsert_remote_usage_buckets([dict(
        host="host-b", hour=hour, model="claude-opus-5", project="p",
        input=0, output=200, cache_read=0, cache_write_5m=0, cache_write_1h=0, messages=1,
    )])

    pricing = {
        "models": {
            "claude-opus-5": _rate(5.0, 25.0, 6.25, 10.0, 0.50),
            "default": _rate(3.0, 15.0, 3.75, 6.0, 0.30),
        },
        "fast_mode": {}, "monthly_budget_usd": 2000,
    }
    collector = UsageCollector(store=tmp_store, pricing=pricing, host="localhost")
    rollups = collector._compute_rollups()

    today_totals = rollups["totals"]["today"]
    assert today_totals["output"] == 300  # 100 local + 200 remote, not doubled
    assert today_totals["messages"] == 2

    by_model_today = {r["model"]: r for r in rollups["by_model"] if r["window"] == "today"}
    assert by_model_today["claude-opus-5"]["output"] == 300
    expected_remote_cost = 200 / 1e6 * 25.0  # remote costed centrally at claude-opus-5's output rate
    assert abs(by_model_today["claude-opus-5"]["cost_usd"] - (1.0 + expected_remote_cost)) < 1e-9

    by_host_today = {r["host"]: r for r in rollups["by_host"] if r["window"] == "today"}
    assert by_host_today["localhost"]["total"] == 100
    assert by_host_today["host-b"]["total"] == 200
    assert abs(by_host_today["host-b"]["cost_usd"] - expected_remote_cost) < 1e-9

    timeline_row = next(r for r in rollups["timeline"] if r["t"][:13] == hour)
    assert timeline_row["output"] == 300


# -- usage.by_provider (Kimi, briefing 2026-09-18) ----------------------------


def test_by_provider_kimi_cost_is_null_not_zero(tmp_store):
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp_store.insert_kimi_turn_events([
        {"host": "localhost", "session_id": "session_k1", "ts": ts, "ts_ms": 1, "model": "kimi-for-coding",
         "tokens": 208, "project": "testrepo"},
    ])
    collector = UsageCollector(store=tmp_store, pricing=_simple_pricing())
    rollups = collector._compute_rollups()
    by_provider_all = {r["window"]: r for r in rollups["by_provider"] if r["provider"] == "kimi"}["all"]
    assert by_provider_all["tokens"] == 208
    assert by_provider_all["messages"] == 1
    # None (null), not 0.0 -- Kimi bills by subscription quota, not per
    # token; 0.0 would claim "free", which is a different, false claim.
    assert by_provider_all["cost_usd"] is None
    assert type(by_provider_all["cost_usd"]) is not float


def test_by_provider_claude_row_matches_existing_totals_exactly(tmp_store):
    tmp_store.insert_usage_events([_row_now("c1", 12.5)])
    tmp_store.insert_kimi_turn_events([
        {"host": "localhost", "session_id": "session_k1",
         "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "ts_ms": 1,
         "model": "kimi-for-coding", "tokens": 999, "project": "testrepo"},
    ])
    collector = UsageCollector(store=tmp_store, pricing=_simple_pricing())
    rollups = collector._compute_rollups()
    claude_all = {r["window"]: r for r in rollups["by_provider"] if r["provider"] == "claude"}["all"]
    totals_all = rollups["totals"]["all"]
    assert claude_all["tokens"] == totals_all["total"]
    assert claude_all["messages"] == totals_all["messages"]
    assert claude_all["cost_usd"] == totals_all["cost_usd"]


def test_by_provider_present_for_every_window(tmp_store):
    collector = UsageCollector(store=tmp_store, pricing=_simple_pricing())
    rollups = collector._compute_rollups()
    windows = {r["window"] for r in rollups["by_provider"]}
    assert windows == {"today", "7d", "30d", "all"}
    providers = {r["provider"] for r in rollups["by_provider"]}
    assert providers == {"claude", "kimi"}


# -- regression: adding Kimi data must not change a single Claude figure -----


def test_kimi_data_present_does_not_change_claude_totals(tmp_store):
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp_store.insert_usage_events([_row_now("c1", 12.5), _row_now("c2", 8.05)])
    collector = UsageCollector(store=tmp_store, pricing=_simple_pricing())
    before = collector._compute_rollups()

    tmp_store.insert_kimi_turn_events([
        {"host": "localhost", "session_id": "session_k1", "ts": ts, "ts_ms": 1,
         "model": "kimi-for-coding", "tokens": 500000, "project": "testrepo"},
    ])
    tmp_store.insert_kimi_error_events([
        {"host": "localhost", "session_id": "session_k1", "ts": ts, "ts_ms": 1,
         "kind": "quota_exceeded", "code": "provider.auth_error", "example": "403 ..."},
    ])
    after = collector._compute_rollups()

    for window in ("today", "7d", "30d", "all"):
        assert before["totals"][window] == after["totals"][window], window
    assert before["by_model"] == after["by_model"]
    assert before["by_project"] == after["by_project"]
    assert before["by_agent"] == after["by_agent"]
    assert before["by_host"] == after["by_host"]
    assert before["burn"] == after["burn"]
    assert before["budget"] == after["budget"]


# -- subagent transcripts, local-midnight "today" (q733.12) -------------------
# Synthetic fixtures; every "now" is injected, nothing reads the wall clock.

import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402

PARENT = "11111111-aaaa-bbbb-cccc-000000000001"
PROJ_DIR = "-home-user-work-demo"


def _assistant_line(mid, ts, session=PARENT, agent=None, sidechain=False, output=10):
    doc = {
        "type": "assistant", "timestamp": ts, "sessionId": session, "isSidechain": sidechain,
        "message": {"id": mid, "model": "claude-opus-5", "usage": {
            "input_tokens": 100, "output_tokens": output,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }},
    }
    if agent:
        doc["agentId"] = agent
    return json.dumps(doc)


def _make_tree(tmp_path, main_lines=(), subagents=None):
    """<root>/<proj>/<PARENT>.jsonl plus <root>/<proj>/<PARENT>/subagents/agent-<id>.jsonl"""
    proj = tmp_path / PROJ_DIR
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{PARENT}.jsonl").write_text("\n".join(main_lines) + ("\n" if main_lines else ""))
    for agent_id, lines in (subagents or {}).items():
        d = proj / PARENT / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n")
    return str(tmp_path / "*" / "*.jsonl")


def _usage_collector(tmp_store, glob_, ctx=None, now=None):
    c = UsageCollector(ctx=ctx, projects_glob=glob_, store=tmp_store, pricing=_simple_pricing())
    if now is not None:
        c._now = lambda: now
    return c


def _rows(store, where="1=1"):
    return store._conn.execute(f"SELECT * FROM usage_events WHERE {where} ORDER BY message_id").fetchall()  # noqa: S608


@pytest.mark.asyncio
async def test_subagent_file_ingested_under_parent_session_with_sidechain_flag(tmp_path, tmp_store):
    glob_ = _make_tree(
        tmp_path,
        main_lines=[_assistant_line("m-main", "2026-10-01T10:00:00.000Z")],
        subagents={"aaa111": [
            _assistant_line("m-sub1", "2026-10-01T10:01:00.000Z", agent="aaa111", sidechain=True),
            _assistant_line("m-sub2", "2026-10-01T10:02:00.000Z", agent="aaa111", sidechain=True, output=20),
        ]},
    )
    result = await _usage_collector(tmp_store, glob_).collect()
    assert result["_inserted"] == 3
    sub = _rows(tmp_store, "is_sidechain = 1")
    assert [r["message_id"] for r in sub] == ["m-sub1", "m-sub2"]
    assert {r["session_id"] for r in sub} == {PARENT}
    assert {r["agent_id"] for r in sub} == {"aaa111"}
    assert {r["project"] for r in sub} == {"demo"}  # not "subagents" / the session dir
    assert sub[1]["cost_usd"] == pytest.approx((100 * 3.0 + 20 * 15.0) / 1_000_000)
    main = _rows(tmp_store, "is_sidechain = 0")
    assert [r["message_id"] for r in main] == ["m-main"]
    assert main[0]["agent_id"] is None


@pytest.mark.asyncio
async def test_subagent_agent_id_falls_back_to_file_name(tmp_path, tmp_store):
    glob_ = _make_tree(tmp_path, subagents={"bbb222": [
        _assistant_line("m-sub1", "2026-10-01T10:01:00.000Z", sidechain=False),  # no agentId, no flag
    ]})
    await _usage_collector(tmp_store, glob_).collect()
    row = _rows(tmp_store)[0]
    assert row["agent_id"] == "bbb222"
    assert row["is_sidechain"] == 1


@pytest.mark.asyncio
async def test_subagent_rescan_dedupes_and_picks_up_appended_lines(tmp_path, tmp_store):
    lines = [_assistant_line("m-sub1", "2026-10-01T10:01:00.000Z", agent="aaa111", sidechain=True)]
    glob_ = _make_tree(tmp_path, subagents={"aaa111": lines + lines})  # same message id twice
    c = _usage_collector(tmp_store, glob_)
    assert (await c.collect())["_inserted"] == 1
    assert (await c.collect())["_inserted"] == 0  # offset cached, nothing re-read
    f = tmp_path / PROJ_DIR / PARENT / "subagents" / "agent-aaa111.jsonl"
    with f.open("a") as fh:
        fh.write(_assistant_line("m-sub2", "2026-10-01T10:05:00.000Z", agent="aaa111", sidechain=True) + "\n")
    assert (await c.collect())["_inserted"] == 1
    # a fresh collector over the same store (restart): the stored offset means no re-read
    assert (await _usage_collector(tmp_store, glob_).collect())["_inserted"] == 0
    assert len(_rows(tmp_store)) == 2


@pytest.mark.asyncio
async def test_first_run_backfills_every_subagent_file(tmp_path, tmp_store):
    subs = {
        f"agent{i}": [
            _assistant_line(f"m-{i}-{j}", f"2026-09-2{j}T10:00:00.000Z", agent=f"agent{i}", sidechain=True)
            for j in range(3)
        ]
        for i in range(5)
    }
    glob_ = _make_tree(tmp_path, subagents=subs)
    result = await _usage_collector(tmp_store, glob_).collect()
    assert result["_inserted"] == 15
    assert len({r["agent_id"] for r in _rows(tmp_store)}) == 5


@pytest.mark.asyncio
async def test_collect_ingests_off_the_event_loop_thread(tmp_path, tmp_store):
    glob_ = _make_tree(tmp_path, main_lines=[_assistant_line("m1", "2026-10-01T10:00:00.000Z")])
    c = _usage_collector(tmp_store, glob_)
    seen = []
    real = c._ingest
    c._ingest = lambda: (seen.append(threading.current_thread()), real())[1]
    await c.collect()
    assert seen and seen[0] is not threading.main_thread()


def _ctx(tmp_path, tz=None):
    layout = tmp_path / "layout.json"
    layout.write_text(json.dumps({"timezone": tz} if tz is not None else {}))
    return SimpleNamespace(
        config=SimpleNamespace(layout_path=layout), pricing=_simple_pricing(), usage_by_session={},
    )


@pytest.mark.asyncio
async def test_subagents_active_counts_distinct_subagents_in_last_5_minutes(tmp_path, tmp_store):
    now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    main = [_assistant_line("m-main", "2026-10-01T11:59:00.000Z")]
    glob_ = _make_tree(tmp_path, main_lines=main, subagents={
        "aaa111": [  # three messages, one subagent
            _assistant_line(f"a{i}", f"2026-10-01T11:5{i}:30.000Z", agent="aaa111", sidechain=True)
            for i in (6, 7, 8)
        ],
        "bbb222": [_assistant_line("b1", "2026-10-01T11:58:00.000Z", agent="bbb222", sidechain=True)],
        "ccc333": [_assistant_line("c1", "2026-10-01T11:40:00.000Z", agent="ccc333", sidechain=True)],  # idle
    })
    ctx = _ctx(tmp_path)
    await _usage_collector(tmp_store, glob_, ctx=ctx, now=now).collect()
    assert ctx.usage_by_session[PARENT]["subagents_active"] == 2


@pytest.mark.asyncio
async def test_subagent_cost_counts_in_totals_and_session_cost(tmp_path, tmp_store):
    now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    glob_ = _make_tree(
        tmp_path,
        main_lines=[_assistant_line("m-main", "2026-10-01T11:00:00.000Z")],
        subagents={"aaa111": [
            _assistant_line("a1", "2026-10-01T11:30:00.000Z", agent="aaa111", sidechain=True),
        ]},
    )
    ctx = _ctx(tmp_path)
    result = await _usage_collector(tmp_store, glob_, ctx=ctx, now=now).collect()
    assert result["usage"]["totals"]["today"]["messages"] == 2
    assert ctx.usage_by_session[PARENT]["msg_count_today"] == 2


def test_store_migrates_agent_id_column(tmp_path):
    import sqlite3

    from critdash.store import Store

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE usage_events (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL UNIQUE,"
        " ts TEXT NOT NULL, session_id TEXT, project TEXT, project_path TEXT, model TEXT,"
        " input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,"
        " cache_read INTEGER NOT NULL DEFAULT 0, cache_write_5m INTEGER NOT NULL DEFAULT 0,"
        " cache_write_1h INTEGER NOT NULL DEFAULT 0, speed TEXT, is_sidechain INTEGER NOT NULL DEFAULT 0,"
        " web_searches INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0.0)"
    )
    conn.execute("INSERT INTO usage_events (message_id, ts) VALUES ('old', '2026-09-01T00:00:00Z')")
    conn.commit()
    conn.close()
    s = Store(db)
    cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(usage_events)")}
    assert "agent_id" in cols
    assert s.insert_usage_events([_row_now("new", 1.0)]) == 1
    s.close()


# -- "today" starts at local midnight in the layout timezone --------------------

EVENING_NY = datetime(2026, 10, 2, 0, 47, 0, tzinfo=UTC)  # 20:47 EDT on Oct 1


def test_today_window_is_local_midnight_when_utc_is_already_tomorrow(tmp_path, tmp_store):
    c = _usage_collector(tmp_store, "x", ctx=_ctx(tmp_path, "America/New_York"), now=EVENING_NY)
    assert c._windows()["today"] == "2026-10-01T04:00:00Z"


@pytest.mark.parametrize("now,expected", [
    # US spring-forward 2026-03-08: midnight is still EST (UTC-5), the day is 23h long
    (datetime(2026, 3, 8, 18, 0, tzinfo=UTC), "2026-03-08T05:00:00Z"),
    # the day after: EDT (UTC-4)
    (datetime(2026, 3, 9, 18, 0, tzinfo=UTC), "2026-03-09T04:00:00Z"),
    # US fall-back 2026-11-01: midnight is still EDT (UTC-4), the day is 25h long
    (datetime(2026, 11, 1, 18, 0, tzinfo=UTC), "2026-11-01T04:00:00Z"),
    # 03:00 UTC on Nov 2 is still Nov 1 local (EST): same local day
    (datetime(2026, 11, 2, 3, 0, tzinfo=UTC), "2026-11-01T04:00:00Z"),
])
def test_today_window_dst_transition_days(tmp_path, tmp_store, now, expected):
    c = _usage_collector(tmp_store, "x", ctx=_ctx(tmp_path, "America/New_York"), now=now)
    assert c._windows()["today"] == expected


@pytest.mark.parametrize("tz", ["Not/AZone", "", None, 5])
def test_today_window_invalid_or_missing_timezone_falls_back_to_utc(tmp_path, tmp_store, tz):
    c = _usage_collector(tmp_store, "x", ctx=_ctx(tmp_path, tz), now=EVENING_NY)
    assert c._windows()["today"] == "2026-10-02T00:00:00Z"


def test_today_window_without_ctx_is_utc(tmp_store):
    c = _usage_collector(tmp_store, "x", now=EVENING_NY)
    assert c._windows()["today"] == "2026-10-02T00:00:00Z"


def test_unreadable_layout_falls_back_to_utc(tmp_path, tmp_store):
    ctx = SimpleNamespace(config=SimpleNamespace(layout_path=tmp_path / "missing.json"), pricing={})
    c = _usage_collector(tmp_store, "x", ctx=ctx, now=EVENING_NY)
    assert c._windows()["today"] == "2026-10-02T00:00:00Z"


@pytest.mark.asyncio
async def test_evening_usage_counts_today_in_local_zone_everywhere(tmp_path, tmp_store):
    # 22:00Z Oct 1 = 18:00 EDT Oct 1; "now" is 00:47Z Oct 2 = 20:47 EDT Oct 1
    glob_ = _make_tree(tmp_path, main_lines=[
        _assistant_line("m-early", "2026-10-01T03:00:00.000Z"),  # 23:00 EDT Sep 30: not today
        _assistant_line("m-eve", "2026-10-01T22:00:00.000Z"),
    ])
    ctx = _ctx(tmp_path, "America/New_York")
    result = await _usage_collector(tmp_store, glob_, ctx=ctx, now=EVENING_NY).collect()
    usage = result["usage"]
    assert usage["totals"]["today"]["messages"] == 1
    assert usage["cache_hit_ratio_today"] == 0.0
    assert ctx.usage_by_session[PARENT]["msg_count_today"] == 1
    assert ctx.usage_by_session[PARENT]["tokens_today"]["input"] == 100
    # UTC config: nothing before Oct 2 00:00Z counts
    ctx_utc = _ctx(tmp_path, "UTC")
    result = await _usage_collector(tmp_store, glob_, ctx=ctx_utc, now=EVENING_NY).collect()
    assert result["usage"]["totals"]["today"]["messages"] == 0
    assert ctx_utc.usage_by_session[PARENT]["msg_count_today"] == 0
