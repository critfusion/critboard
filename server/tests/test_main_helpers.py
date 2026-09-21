import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from critdash.main import _atomic_write_json, _window_to_delta, _window_to_since, app, validate_layout


def test_validate_layout_valid():
    doc = {
        "version": 1, "title": "t",
        "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "fleet", "type": "agent_grid", "title": "FLEET", "x": 0, "y": 0, "w": 6, "h": 4}],
    }
    assert validate_layout(doc) == []


def test_validate_layout_missing_fields():
    errors = validate_layout({"panels": []})
    assert any("version" in e for e in errors)
    assert any("grid" in e for e in errors)


def test_validate_layout_duplicate_panel_ids():
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [
            {"id": "a", "type": "stat_row", "x": 0, "y": 0, "w": 1, "h": 1},
            {"id": "a", "type": "stat_row", "x": 1, "y": 0, "w": 1, "h": 1},
        ],
    }
    errors = validate_layout(doc)
    assert any("duplicate panel id" in e for e in errors)


def test_validate_layout_not_a_dict():
    assert validate_layout([1, 2, 3]) == ["layout must be a JSON object"]


def test_atomic_write_creates_backup(tmp_path):
    target = tmp_path / "layout.json"
    target.write_text('{"version": 1}')
    _atomic_write_json(target, {"version": 2})
    assert json.loads(target.read_text()) == {"version": 2}
    bak = target.with_suffix(".json.bak")
    assert json.loads(bak.read_text()) == {"version": 1}


def test_atomic_write_no_partial_file_on_success(tmp_path):
    target = tmp_path / "layout.json"
    _atomic_write_json(target, {"a": 1})
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_window_to_since_returns_iso():
    since = _window_to_since("24h")
    assert since.endswith("Z")
    assert "T" in since


def test_window_to_delta_known_windows():
    assert _window_to_delta("24h") == timedelta(hours=24)
    assert _window_to_delta("7d") == timedelta(days=7)
    assert _window_to_delta("30d") == timedelta(days=30)
    assert _window_to_delta("90d") == timedelta(days=90)


def test_window_to_delta_arbitrary_n_forms():
    assert _window_to_delta("3d") == timedelta(days=3)
    assert _window_to_delta("6h") == timedelta(hours=6)
    assert _window_to_delta("120h") == timedelta(hours=120)


def test_window_to_delta_rejects_garbage():
    # Bug fix: an unparseable window must raise, not silently fall back to a
    # default (a caller asking for a typo'd window used to get 24h of data
    # back with no indication anything was wrong).
    for bad in ("bogus", "", "5x", "-5d", "0d", "0h", "7 days"):
        with pytest.raises(ValueError):
            _window_to_delta(bad)


# -- /api/history/usage zero-fill (Fix 1) ------------------------------------
# Real app, real (possibly sparse or empty) store -- the zero-fill contract
# must hold regardless of what data happens to be present.


def _contiguous(points, step):
    ts = [datetime.fromisoformat(p["t"].replace("Z", "+00:00")) for p in points]
    assert ts == sorted(ts)
    assert len(set(ts)) == len(ts)
    for a, b in zip(ts, ts[1:], strict=False):
        assert (b - a) == step


def test_history_usage_24h_hour_returns_24_contiguous_buckets():
    client = TestClient(app)
    resp = client.get("/api/history/usage", params={"window": "24h", "bucket": "hour"})
    assert resp.status_code == 200
    points = resp.json()
    assert len(points) == 24
    _contiguous(points, timedelta(hours=1))
    for p in points:
        for key in ("t", "input", "output", "cache_read", "cache_write", "cost_usd", "messages"):
            assert key in p


def test_history_usage_7d_day_returns_7_contiguous_buckets():
    client = TestClient(app)
    resp = client.get("/api/history/usage", params={"window": "7d", "bucket": "day"})
    assert resp.status_code == 200
    points = resp.json()
    assert len(points) == 7
    _contiguous(points, timedelta(days=1))


def test_history_usage_30d_day_returns_30_contiguous_buckets():
    client = TestClient(app)
    resp = client.get("/api/history/usage", params={"window": "30d", "bucket": "day"})
    assert resp.status_code == 200
    assert len(resp.json()) == 30


def test_history_usage_90d_day_returns_90_contiguous_buckets():
    # Bug fix: window=90d used to be unrecognised and silently fall back to
    # a 24h default (1 bucket back), instead of the 90 a caller asked for.
    client = TestClient(app)
    resp = client.get("/api/history/usage", params={"window": "90d", "bucket": "day"})
    assert resp.status_code == 200
    points = resp.json()
    assert len(points) == 90
    _contiguous(points, timedelta(days=1))


def test_history_usage_arbitrary_n_day_window():
    client = TestClient(app)
    resp = client.get("/api/history/usage", params={"window": "3d", "bucket": "day"})
    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_history_usage_arbitrary_n_hour_window():
    client = TestClient(app)
    resp = client.get("/api/history/usage", params={"window": "6h", "bucket": "hour"})
    assert resp.status_code == 200
    assert len(resp.json()) == 6


def test_history_usage_today_window_hour_bucket():
    client = TestClient(app)
    resp = client.get("/api/history/usage", params={"window": "today", "bucket": "hour"})
    assert resp.status_code == 200
    points = resp.json()
    # At least the current (partial) hour, at most 24 (midnight to now).
    assert 1 <= len(points) <= 24
    _contiguous(points, timedelta(hours=1))
    now = datetime.now(UTC)
    last_t = datetime.fromisoformat(points[-1]["t"].replace("Z", "+00:00"))
    assert last_t.hour == now.hour
    first_t = datetime.fromisoformat(points[0]["t"].replace("Z", "+00:00"))
    assert first_t.hour == 0


def test_history_usage_today_window_day_bucket_is_one_bucket():
    client = TestClient(app)
    resp = client.get("/api/history/usage", params={"window": "today", "bucket": "day"})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_history_usage_unparseable_window_returns_400():
    client = TestClient(app)
    resp = client.get("/api/history/usage", params={"window": "bogus", "bucket": "hour"})
    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]


def test_history_usage_invalid_bucket_returns_400():
    client = TestClient(app)
    resp = client.get("/api/history/usage", params={"window": "7d", "bucket": "fortnight"})
    assert resp.status_code == 400


def test_history_usage_invalid_group_by_returns_400():
    client = TestClient(app)
    resp = client.get(
        "/api/history/usage", params={"window": "7d", "bucket": "day", "group_by": "nonsense"}
    )
    assert resp.status_code == 400


def test_history_usage_group_by_none_is_still_a_bare_list():
    # Byte-compatibility: group_by=none (the default) must keep returning the
    # flat list-of-buckets shape the existing 48h timeline widget consumes,
    # not the {"buckets":...,"series":...} shape group_by=model/provider/host
    # return.
    client = TestClient(app)
    resp = client.get(
        "/api/history/usage", params={"window": "24h", "bucket": "hour", "group_by": "none"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 24


def test_history_agents_unparseable_window_returns_400():
    client = TestClient(app)
    resp = client.get("/api/history/agents", params={"window": "bogus"})
    assert resp.status_code == 400
