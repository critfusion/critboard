import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from critdash import config as config_mod
from critdash import detect as detect_mod
from critdash import main as main_mod
from critdash import update as update_mod
from critdash.main import (
    _REQUIRED_COLOR_TOKENS,
    _atomic_write_json,
    _window_to_delta,
    _window_to_since,
    app,
    validate_layout,
    validate_theme,
)

# A "colors" object satisfying validate_theme's required-token set --
# arbitrary values, real tokens (see _REQUIRED_COLOR_TOKENS's derivation).
_FULL_COLORS = {token: "#000000" for token in _REQUIRED_COLOR_TOKENS}


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


def test_validate_layout_rejects_overlapping_panels():
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [
            {"id": "a", "type": "stat_row", "x": 0, "y": 0, "w": 4, "h": 4},
            {"id": "b", "type": "stat_row", "x": 2, "y": 2, "w": 4, "h": 4},
        ],
    }
    errors = validate_layout(doc)
    assert any("overlaps" in e for e in errors)


def test_validate_layout_adjacent_panels_do_not_overlap():
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [
            {"id": "a", "type": "stat_row", "x": 0, "y": 0, "w": 4, "h": 4},
            {"id": "b", "type": "stat_row", "x": 4, "y": 0, "w": 4, "h": 4},
        ],
    }
    assert validate_layout(doc) == []


def test_validate_layout_rejects_panel_past_grid_columns():
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "x": 10, "y": 0, "w": 4, "h": 4}],
    }
    errors = validate_layout(doc)
    assert any("extends past grid columns" in e for e in errors)


def test_validate_layout_rejects_non_string_title():
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "title": 123, "x": 0, "y": 0, "w": 4, "h": 4}],
    }
    errors = validate_layout(doc)
    assert any("'title' must be a string" in e for e in errors)


def test_validate_layout_rejects_bad_timezone():
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [], "timezone": "Not/AZone",
    }
    errors = validate_layout(doc)
    assert any("timezone" in e for e in errors)


def test_validate_layout_accepts_valid_timezone():
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "x": 0, "y": 0, "w": 1, "h": 1}],
        "timezone": "America/Chicago",
    }
    assert validate_layout(doc) == []


def test_validate_layout_rejects_empty_panels():
    # Same data-loss shape as the theme bug: an empty list still satisfies
    # `isinstance(panels, list)`, so this used to pass with zero errors and
    # would silently save a layout with no panels at all.
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [],
    }
    errors = validate_layout(doc)
    assert errors
    assert any("panels" in e for e in errors)


def test_validate_layout_rejects_grid_missing_columns():
    doc = {
        "version": 1, "grid": {"row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "x": 0, "y": 0, "w": 1, "h": 1}],
    }
    errors = validate_layout(doc)
    assert any("columns" in e for e in errors)


def test_validate_layout_rejects_panel_missing_any_required_field():
    for missing in ("id", "type", "x", "y", "w", "h"):
        panel = {"id": "a", "type": "stat_row", "x": 0, "y": 0, "w": 1, "h": 1}
        del panel[missing]
        doc = {
            "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
            "panels": [panel],
        }
        errors = validate_layout(doc)
        assert any(missing in e for e in errors), f"missing {missing!r} was not rejected: {errors}"


def test_validate_layout_preserves_unknown_top_level_keys():
    # Forward compatibility -- an unrecognized top-level key must not itself
    # be a validation error (only the specific shapes that would brick
    # rendering are rejected).
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "x": 0, "y": 0, "w": 1, "h": 1}],
        "some_future_key": {"anything": "goes"},
    }
    assert validate_layout(doc) == []


def test_validate_theme_valid():
    doc = {
        "name": "t", "colors": dict(_FULL_COLORS), "fonts": {"mono": "monospace"},
        "reload_banner": {"enabled": True}, "_presets": {"light": {"colors": {}}},
    }
    assert validate_theme(doc) == []


def test_validate_theme_not_a_dict():
    assert validate_theme([1, 2]) == ["theme must be a JSON object"]


def test_validate_theme_rejects_non_dict_colors():
    errors = validate_theme({"colors": "not-an-object"})
    assert any("'colors' must be an object" in e for e in errors)


def test_validate_theme_rejects_non_string_color_value():
    errors = validate_theme({"colors": {"bg": 12345}})
    assert any("colors.bg must be a string" in e for e in errors)


def test_validate_theme_rejects_non_bool_reload_banner_enabled():
    errors = validate_theme({"reload_banner": {"enabled": "yes"}})
    assert any("reload_banner.enabled must be a boolean" in e for e in errors)


def test_validate_theme_preserves_unknown_top_level_keys():
    doc = {"some_future_key": 42, "colors": dict(_FULL_COLORS)}
    assert validate_theme(doc) == []


# -- Bug fix: an empty document (or an empty 'colors' object) used to pass
# validate_theme with zero errors -- every check was gated behind
# `if <field> is not None`, so a field that was simply absent validated
# itself. POST /api/config/theme with {} therefore returned 200 and
# silently overwrote config/theme.json with {}, losing every colour token
# and both presets while the page kept rendering (style.css's own :root
# block still has a hardcoded fallback per token). These tests fail against
# the old code and pass after the fix.


def test_validate_theme_rejects_empty_document():
    errors = validate_theme({})
    assert errors  # at least one error -- 'colors' is required
    assert any("colors" in e for e in errors)


def test_validate_theme_rejects_empty_colors():
    errors = validate_theme({"colors": {}})
    assert errors
    assert any("colors" in e for e in errors)


def test_validate_theme_rejects_missing_required_color_tokens():
    # Present, non-empty, all string values -- but missing tokens the
    # frontend actually reads (e.g. status-crit, severity-warn, every
    # priority-N). Must still be rejected, not just type-checked.
    errors = validate_theme({"colors": {"bg": "#000000"}})
    assert errors


def test_validate_theme_real_config_theme_json_round_trips_with_zero_errors():
    # Read the real, shipped config/theme.json from disk -- not a copy kept
    # in this test file -- so this can never drift from what's actually on
    # disk. If someone adds a new --color-* token to the frontend without
    # updating config/theme.json (or vice versa), this is the test that
    # catches it.
    from critdash.config import DASHBOARD_ROOT

    real_theme_path = DASHBOARD_ROOT / "config" / "theme.json"
    with real_theme_path.open() as f:
        real_theme = json.load(f)
    assert validate_theme(real_theme) == []


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


# -- isolated app for config-writing endpoints --------------------------------
# These tests write real files, so they must NEVER run against the module-
# level `app` singleton (which points at the real config/layout.json and
# config/theme.json -- this repo's actual settings). Each test here builds
# its own app pointed at a throwaway tmp_path config dir instead.


def _write_isolated_config(tmp_path, extra_sources=None):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    sources = dict(config_mod.DEFAULT_SOURCES)
    sources["db_path"] = str(tmp_path / "test.db")
    if extra_sources:
        sources.update(extra_sources)
    (config_dir / "sources.json").write_text(json.dumps(sources))
    (config_dir / "sources.example.json").write_text(json.dumps(config_mod.DEFAULT_SOURCES))
    layout = {
        "version": 1, "title": "t",
        "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "title": "A", "x": 0, "y": 0, "w": 4, "h": 4}],
    }
    theme = {"name": "t", "colors": {"bg": "#000000"}}
    (config_dir / "layout.json").write_text(json.dumps(layout))
    (config_dir / "theme.json").write_text(json.dumps(theme))
    return config_dir


def _build_isolated_client(tmp_path, monkeypatch, extra_sources=None):
    config_dir = _write_isolated_config(tmp_path, extra_sources)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    return TestClient(main_mod.build_app())


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    return _build_isolated_client(tmp_path, monkeypatch)


@pytest.fixture
def isolated_app_writes_disabled(tmp_path, monkeypatch):
    return _build_isolated_client(tmp_path, monkeypatch, extra_sources={"allow_config_writes": False})


def test_post_layout_valid_returns_saved_document(isolated_app):
    doc = {
        "version": 2, "title": "New",
        "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "title": "A", "x": 0, "y": 0, "w": 4, "h": 4}],
    }
    resp = isolated_app.post("/api/config/layout", json=doc)
    assert resp.status_code == 200
    assert resp.json() == doc
    assert isolated_app.get("/api/config/layout").json() == doc


def test_post_layout_rejects_overlapping_panels_with_400(isolated_app):
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [
            {"id": "a", "type": "stat_row", "x": 0, "y": 0, "w": 4, "h": 4},
            {"id": "b", "type": "stat_row", "x": 2, "y": 2, "w": 4, "h": 4},
        ],
    }
    resp = isolated_app.post("/api/config/layout", json=doc)
    assert resp.status_code == 400
    assert any("overlaps" in e for e in resp.json()["detail"]["errors"])


def test_post_layout_rejects_out_of_grid_panel_with_400(isolated_app):
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "x": 10, "y": 0, "w": 4, "h": 4}],
    }
    resp = isolated_app.post("/api/config/layout", json=doc)
    assert resp.status_code == 400
    assert any("extends past grid columns" in e for e in resp.json()["detail"]["errors"])


def test_post_layout_rejects_missing_fields_with_400(isolated_app):
    resp = isolated_app.post("/api/config/layout", json={"panels": []})
    assert resp.status_code == 400
    assert "errors" in resp.json()["detail"]


def test_post_layout_rejects_non_string_title_with_400(isolated_app):
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "title": 5, "x": 0, "y": 0, "w": 1, "h": 1}],
    }
    resp = isolated_app.post("/api/config/layout", json=doc)
    assert resp.status_code == 400
    assert any("'title' must be a string" in e for e in resp.json()["detail"]["errors"])


def test_post_layout_rejects_oversized_body(isolated_app):
    panels = [
        {"id": f"p{i}", "type": "stat_row", "x": 0, "y": i, "w": 1, "h": 1, "junk": "x" * 5000}
        for i in range(500)
    ]
    doc = {"version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14}, "panels": panels}
    resp = isolated_app.post("/api/config/layout", json=doc)
    assert resp.status_code == 413


def test_post_theme_valid_returns_saved_document(isolated_app):
    doc = {"name": "t2", "colors": dict(_FULL_COLORS)}
    resp = isolated_app.post("/api/config/theme", json=doc)
    assert resp.status_code == 200
    assert resp.json() == doc
    assert isolated_app.get("/api/config/theme").json() == doc


def test_post_theme_rejects_non_dict_colors_with_400(isolated_app):
    resp = isolated_app.post("/api/config/theme", json={"colors": "nope"})
    assert resp.status_code == 400
    assert any("'colors' must be an object" in e for e in resp.json()["detail"]["errors"])


def test_post_theme_rejects_non_string_color_value_with_400(isolated_app):
    resp = isolated_app.post("/api/config/theme", json={"colors": {"bg": 42}})
    assert resp.status_code == 400
    assert any("colors.bg must be a string" in e for e in resp.json()["detail"]["errors"])


def test_post_theme_rejects_non_bool_reload_banner_with_400(isolated_app):
    resp = isolated_app.post("/api/config/theme", json={"reload_banner": {"enabled": "yes"}})
    assert resp.status_code == 400


def test_post_config_writes_disabled_returns_403(isolated_app_writes_disabled):
    resp = isolated_app_writes_disabled.post("/api/config/layout", json={"version": 1})
    assert resp.status_code == 403
    assert "allow_config_writes" in resp.json()["detail"]
    resp2 = isolated_app_writes_disabled.post("/api/config/theme", json={"colors": {}})
    assert resp2.status_code == 403


# -- End-to-end: POST with an empty body must 400, and must NOT touch the
# on-disk file. This is the actual reported bug for /api/config/theme
# (POST {} used to return 200 and overwrite theme.json with {}) plus the
# same assertion for /api/config/layout, which already behaved correctly.
# Uses its own isolated tmp_path config dir (not the `isolated_app` fixture)
# so the test can read the file back directly and compare bytes.


def test_post_theme_empty_body_returns_400_and_file_unchanged(tmp_path, monkeypatch):
    config_dir = _write_isolated_config(tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    theme_path = config_dir / "theme.json"
    before = theme_path.read_bytes()

    client = TestClient(main_mod.build_app())
    resp = client.post("/api/config/theme", json={})

    assert resp.status_code == 400
    assert "errors" in resp.json()["detail"]
    assert theme_path.read_bytes() == before


def test_post_layout_empty_body_returns_400_and_file_unchanged(tmp_path, monkeypatch):
    config_dir = _write_isolated_config(tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    layout_path = config_dir / "layout.json"
    before = layout_path.read_bytes()

    client = TestClient(main_mod.build_app())
    resp = client.post("/api/config/layout", json={})

    assert resp.status_code == 400
    assert "errors" in resp.json()["detail"]
    assert layout_path.read_bytes() == before


def test_safe_config_path_rejects_path_outside_config_dir(tmp_path):
    from fastapi import HTTPException

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    outside = tmp_path / "outside" / "evil.json"
    with pytest.raises(HTTPException):
        main_mod._safe_config_path(outside, config_dir)


def test_safe_config_path_allows_path_inside_config_dir(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    inside = config_dir / "layout.json"
    assert main_mod._safe_config_path(inside, config_dir) == inside.resolve()


def test_settings_suggest_shape_on_empty_snapshot(isolated_app):
    resp = isolated_app.get("/api/settings/suggest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["human_labels"]["detected"] == []
    assert "no label found" in body["human_labels"]["reason"]
    assert isinstance(body["timezone"]["detected"], str) and body["timezone"]["detected"]
    assert body["timezone"]["source"] in ("env", "system", "default")
    qp = body["quota_providers"]
    assert set(qp) == {"never_available", "needs_credential", "working"}


def test_snapshot_includes_settings_block_with_default_timezone(isolated_app):
    resp = isolated_app.get("/api/snapshot")
    assert resp.status_code == 200
    settings = resp.json()["settings"]
    assert settings["timezone"] == "UTC"
    assert settings["allow_config_writes"] is True
    assert settings["refresh_preset"] == "normal"


def test_snapshot_settings_timezone_reflects_saved_layout(isolated_app):
    doc = {
        "version": 1, "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "x": 0, "y": 0, "w": 1, "h": 1}],
        "timezone": "America/Chicago",
    }
    post_resp = isolated_app.post("/api/config/layout", json=doc)
    assert post_resp.status_code == 200
    snap_resp = isolated_app.get("/api/snapshot")
    assert snap_resp.json()["settings"]["timezone"] == "America/Chicago"


# -- GET/POST /api/settings/updates (briefing Task 3) --------------------------
# Lifespan (and therefore update_check_loop, scheduler.start(), every other
# background task) never runs under a plain TestClient.get()/.post() call --
# only `with TestClient(...) as c:` triggers it (verified: no test in this
# suite uses that form for the FastAPI app) -- so these tests never make a
# real network call, and /api/snapshot's "update" key stays the static
# empty_snapshot() default unless a test sets it explicitly.


def test_get_settings_updates_defaults(isolated_app):
    resp = isolated_app.get("/api/settings/updates")
    assert resp.status_code == 200
    body = resp.json()
    assert body["check_enabled"] is True
    assert body["check_interval_s"] == 900
    assert body["auto_apply"] is False
    assert body["repo"] == "critfusion/critboard"
    assert body["branch"] == "main"
    assert body["update_available"] is False
    assert body["current"] is None
    assert body["latest"] is None


def test_post_settings_updates_valid_persists_and_reflected_on_get(isolated_app, tmp_path):
    resp = isolated_app.post(
        "/api/settings/updates",
        json={"check_enabled": False, "check_interval_s": 1200, "auto_apply": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["check_enabled"] is False
    assert body["check_interval_s"] == 1200
    assert body["auto_apply"] is True

    get_resp = isolated_app.get("/api/settings/updates")
    assert get_resp.json()["check_enabled"] is False
    assert get_resp.json()["check_interval_s"] == 1200
    assert get_resp.json()["auto_apply"] is True


def test_post_settings_updates_persists_to_sources_json_on_disk(tmp_path, monkeypatch):
    config_dir = _write_isolated_config(tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(main_mod.build_app())

    resp = client.post("/api/settings/updates", json={"auto_apply": True})
    assert resp.status_code == 200

    with (config_dir / "sources.json").open() as f:
        on_disk = json.load(f)
    assert on_disk["update_auto_apply"] is True
    # every other key untouched -- this endpoint must never clobber the rest
    # of sources.json (ssh hosts, filesystem paths, etc.)
    assert on_disk["repo_roots"] == config_mod.DEFAULT_SOURCES["repo_roots"]
    assert on_disk["bd_bin"] == config_mod.DEFAULT_SOURCES["bd_bin"]


def test_post_settings_updates_rejects_unknown_key(isolated_app):
    resp = isolated_app.post("/api/settings/updates", json={"repo_roots": ["/evil"]})
    assert resp.status_code == 400
    assert "repo_roots" in resp.json()["detail"]


def test_post_settings_updates_rejects_multiple_unknown_keys_names_both(isolated_app):
    resp = isolated_app.post("/api/settings/updates", json={"ssh_opts": [], "beads_env": "/x"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "ssh_opts" in detail
    assert "beads_env" in detail


def test_post_settings_updates_clamps_interval_floor(isolated_app):
    resp = isolated_app.post("/api/settings/updates", json={"check_interval_s": 10})
    assert resp.status_code == 200
    assert resp.json()["check_interval_s"] == 300  # floor, not the requested 10


def test_post_settings_updates_rejects_bad_types(isolated_app):
    for body in (
        {"check_enabled": "yes"},
        {"auto_apply": "yes"},
        {"check_interval_s": "900"},
        {"check_interval_s": True},  # bool is not an int here, even though bool subclasses int
    ):
        resp = isolated_app.post("/api/settings/updates", json=body)
        assert resp.status_code == 400, body


def test_post_settings_updates_writes_disabled_returns_403(isolated_app_writes_disabled):
    resp = isolated_app_writes_disabled.post("/api/settings/updates", json={"check_enabled": False})
    assert resp.status_code == 403


def test_post_settings_updates_empty_body_is_a_no_op_200(isolated_app):
    before = isolated_app.get("/api/settings/updates").json()
    resp = isolated_app.post("/api/settings/updates", json={})
    assert resp.status_code == 200
    assert resp.json() == before


# -- defect 2 (macOS install report): allow_self_update as a 4th settable key --


def test_get_settings_updates_reports_allow_self_update_default_false(isolated_app):
    resp = isolated_app.get("/api/settings/updates")
    assert resp.json()["allow_self_update"] is False


def test_post_settings_updates_accepts_allow_self_update(isolated_app):
    resp = isolated_app.post("/api/settings/updates", json={"allow_self_update": True})
    assert resp.status_code == 200
    assert resp.json()["allow_self_update"] is True

    get_resp = isolated_app.get("/api/settings/updates")
    assert get_resp.json()["allow_self_update"] is True


def test_post_settings_updates_allow_self_update_persists_to_sources_json(tmp_path, monkeypatch):
    config_dir = _write_isolated_config(tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(main_mod.build_app())

    resp = client.post("/api/settings/updates", json={"allow_self_update": True})
    assert resp.status_code == 200

    with (config_dir / "sources.json").open() as f:
        on_disk = json.load(f)
    assert on_disk["allow_self_update"] is True


def test_post_settings_updates_rejects_non_bool_allow_self_update(isolated_app):
    resp = isolated_app.post("/api/settings/updates", json={"allow_self_update": "yes"})
    assert resp.status_code == 400
    assert any("allow_self_update" in e for e in resp.json()["detail"]["errors"])


# -- defect 1 (macOS install report): a hand edit to config/sources.json ------
# must be picked up by the running server, no restart, via the two call
# sites main.py wires config.reload_sources() into: GET
# /api/settings/updates and POST /api/update/apply. Config.reload_sources()
# itself is unit-tested directly in test_config.py -- these prove the HTTP
# wiring specifically.


def test_get_settings_updates_reflects_hand_edit_without_restart(tmp_path, monkeypatch):
    config_dir = _write_isolated_config(tmp_path, extra_sources={"allow_self_update": False})
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(main_mod.build_app())

    before = client.get("/api/settings/updates").json()
    assert before["allow_self_update"] is False
    assert before["check_interval_s"] == 900

    # A hand edit to the file on disk -- never through the app's own POST --
    # exactly what a user editing config/sources.json in a text editor does.
    on_disk = json.loads((config_dir / "sources.json").read_text())
    on_disk["allow_self_update"] = True
    on_disk["update_check_interval_s"] = 1234
    (config_dir / "sources.json").write_text(json.dumps(on_disk))

    after = client.get("/api/settings/updates").json()
    assert after["allow_self_update"] is True
    assert after["check_interval_s"] == 1234


def test_post_update_apply_reloads_sources_before_allow_self_update_gate(tmp_path, monkeypatch):
    """The exact reported consequence: the user hand-flipped allow_self_update
    false -> true and clicked "Update now", and the running process still
    saw false. Proves reload_sources() runs BEFORE update.apply_update() is
    even called, by making a stand-in apply_update read config.sources back
    and report what it saw -- never touches git or the real dashboard_root."""
    config_dir = _write_isolated_config(tmp_path, extra_sources={"allow_self_update": False})
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(main_mod.build_app())

    on_disk = json.loads((config_dir / "sources.json").read_text())
    on_disk["allow_self_update"] = True
    (config_dir / "sources.json").write_text(json.dumps(on_disk))

    seen = {}

    def fake_apply_update(config, dashboard_root):
        seen["allow_self_update"] = config.sources.get("allow_self_update")
        return {
            "applied": True, "commit": "deadbeef",
            "reinstalled": False, "restart_requested": False, "applied_at": "now",
        }

    monkeypatch.setattr(main_mod.update_mod, "apply_update", fake_apply_update)

    resp = client.post("/api/update/apply")

    assert resp.status_code == 200
    assert seen["allow_self_update"] is True


# -- defect 4 (macOS install report): _update_settings_view's repo field ------
# must use update.py's own repo resolution, not a naive `.get(...) or ""`.


def test_get_settings_updates_repo_reports_default_when_key_absent(tmp_path, monkeypatch):
    """sources.json has no update_repo key at all (the exact measured bug:
    an old/minimal config that predates this key) -- the view must report
    the effective repo that check/apply would actually use
    (DEFAULT_UPDATE_REPO), not "" (which reads as unconfigured). Deliberately
    NOT using the isolated_app fixture: _write_isolated_config seeds the
    full DEFAULT_SOURCES dict, which already has update_repo set explicitly
    -- this test needs the key genuinely absent."""
    config_dir = _write_isolated_config(tmp_path)
    on_disk = json.loads((config_dir / "sources.json").read_text())
    del on_disk["update_repo"]
    (config_dir / "sources.json").write_text(json.dumps(on_disk))
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(main_mod.build_app())

    resp = client.get("/api/settings/updates")
    assert "update_repo" not in json.loads((config_dir / "sources.json").read_text())
    assert resp.json()["repo"] == update_mod.DEFAULT_UPDATE_REPO


def test_get_settings_updates_repo_reports_empty_when_explicitly_disabled(tmp_path, monkeypatch):
    config_dir = _write_isolated_config(tmp_path, extra_sources={"update_repo": ""})
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(main_mod.build_app())

    resp = client.get("/api/settings/updates")
    assert resp.json()["repo"] == ""


def test_get_settings_updates_repo_reports_configured_value(tmp_path, monkeypatch):
    config_dir = _write_isolated_config(tmp_path, extra_sources={"update_repo": "someone/fork"})
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(main_mod.build_app())

    resp = client.get("/api/settings/updates")
    assert resp.json()["repo"] == "someone/fork"


# -- per-collector enablement (Bug 2) ------------------------------------------
# Every assertion here reads /api/healthz (or /api/snapshot's "sources" key)
# immediately after build_app() returns, BEFORE the scheduler's background
# loop has run a single cycle -- deliberately: this never lets a test
# actually shell out to `bd`/probe the filesystem via a running collector, and
# it is exactly what tells an auto-disabled ("not scheduled at all", the
# synthetic _inactive_health entry: optional=True, a real reason_code) collector
# apart from one that IS registered but simply hasn't completed its first run
# yet (the scheduler's default SourceHealth(): optional=False, reason_code=
# None, error=None).

_NO_DEPS_SOURCES = {
    "bd_bin": "/nonexistent/no-such-bd-binary",
    "beads_env": "/nonexistent/no-such-beads-env",
    "overlord_dir": "/nonexistent/no-such-overlord",
    "hosts": [{"name": "localhost", "mode": "local", "enabled": True}],
}


def _no_binaries_anywhere(monkeypatch):
    """Neutralize critdash.detect's fallback candidate scan (Task: "detect
    where tools actually live" -- see detect.py/main.py's _resolve_bd_bin).
    Without this, a dev/CI host that genuinely HAS `bd`/`herdr` installed
    (e.g. at ~/.local/bin, this repo's own dev host) would have the
    fallback find them regardless of what nonsense path a test configures,
    since that fallback is deliberately host-filesystem-aware. Tests that
    want to simulate "this tool is not installed anywhere on this machine"
    call this first."""
    monkeypatch.setattr(detect_mod, "BINARY_CANDIDATE_DIRS", [])
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: None)


def test_healthz_beads_auto_disabled_when_bd_and_env_absent(tmp_path, monkeypatch):
    _no_binaries_anywhere(monkeypatch)
    client = _build_isolated_client(tmp_path, monkeypatch, extra_sources=_NO_DEPS_SOURCES)
    collectors = client.get("/api/healthz").json()["collectors"]
    beads = collectors["beads"]
    assert beads["ok"] is False
    assert beads["optional"] is True
    assert beads["reason_code"] == "dependency_missing"
    assert "no-such-bd-binary" in beads["detail"]


def test_healthz_beads_finds_bd_via_fallback_when_configured_path_is_wrong(tmp_path, monkeypatch):
    """The actual reported bug, end to end: sources.json has a Debian-style
    bd_bin that doesn't exist on this machine, but a real `bd` sits at a
    candidate directory (simulating e.g. Apple Silicon Homebrew's
    /opt/homebrew/bin). The dashboard must find it and mark beads active --
    NOT report "not configured" the way it did for the Mac owner."""
    fake_bin_dir = tmp_path / "opt-homebrew-bin"
    fake_bin_dir.mkdir()
    real_bd = fake_bin_dir / "bd"
    real_bd.write_text("#!/bin/sh\necho '{}'\n")
    real_bd.chmod(0o755)
    monkeypatch.setattr(detect_mod, "BINARY_CANDIDATE_DIRS", [str(fake_bin_dir)])
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: None)

    env_path = tmp_path / "beads-env"
    env_path.write_text("")
    extra = {
        "bd_bin": "/nonexistent/debian-style/bin/bd",
        "beads_env": str(env_path),
        "hosts": [{"name": "localhost", "mode": "local", "enabled": True}],
    }
    client = _build_isolated_client(tmp_path, monkeypatch, extra_sources=extra)
    beads = client.get("/api/healthz").json()["collectors"]["beads"]
    # A real (if not-yet-run) scheduler entry, not the synthetic inactive
    # one -- see the module note above _NO_DEPS_SOURCES.
    assert beads["optional"] is False
    assert beads["reason_code"] is None


def test_healthz_dispatch_auto_disabled_when_overlord_dir_absent(tmp_path, monkeypatch):
    client = _build_isolated_client(tmp_path, monkeypatch, extra_sources=_NO_DEPS_SOURCES)
    dispatch = client.get("/api/healthz").json()["collectors"]["dispatch"]
    assert dispatch["ok"] is False
    assert dispatch["optional"] is True
    assert dispatch["reason_code"] == "config_missing"
    assert "no-such-overlord" in dispatch["detail"]


def test_healthz_remote_auto_disabled_when_no_ssh_host_configured(tmp_path, monkeypatch):
    client = _build_isolated_client(tmp_path, monkeypatch, extra_sources=_NO_DEPS_SOURCES)
    remote = client.get("/api/healthz").json()["collectors"]["remote"]
    assert remote["ok"] is False
    assert remote["optional"] is True
    assert remote["reason_code"] == "config_missing"


def test_healthz_inactive_collectors_are_not_scheduled_but_visible_in_sources(tmp_path, monkeypatch):
    """Still appears in `sources` with optional: true, per Bug 2 -- it must
    not vanish from the API just because it isn't scheduled."""
    _no_binaries_anywhere(monkeypatch)
    client = _build_isolated_client(tmp_path, monkeypatch, extra_sources=_NO_DEPS_SOURCES)
    sources = client.get("/api/snapshot").json()["sources"]
    for name in ("beads", "dispatch", "remote"):
        assert sources[name]["optional"] is True
        assert sources[name]["ok"] is False


def test_healthz_explicit_enable_true_registers_beads_despite_missing_dependency(tmp_path, monkeypatch):
    """An explicit override wins over auto-detection in both directions --
    this is the "force on" direction: bd/env are still absent, but the
    collector must be registered with the scheduler (not the synthetic
    inactive entry) because of the override."""
    extra = dict(_NO_DEPS_SOURCES, collectors={"beads": {"enabled": True}})
    client = _build_isolated_client(tmp_path, monkeypatch, extra_sources=extra)
    beads = client.get("/api/healthz").json()["collectors"]["beads"]
    # A real (if not-yet-run) scheduler entry, not the synthetic inactive
    # one: optional False, no reason_code/error yet.
    assert beads["optional"] is False
    assert beads["reason_code"] is None
    assert beads["error"] is None


def test_healthz_explicit_enable_false_disables_beads_despite_dependency_present(tmp_path, monkeypatch):
    """The "force off" direction: bd_bin/beads_env both resolve fine (using
    this interpreter and a real file as harmless stand-ins), but an explicit
    `enabled: false` must still keep it out of the scheduler."""
    env_path = tmp_path / "fake-beads-env"
    env_path.write_text("")
    extra = {
        "bd_bin": sys.executable,
        "beads_env": str(env_path),
        "collectors": {"beads": {"enabled": False}},
    }
    client = _build_isolated_client(tmp_path, monkeypatch, extra_sources=extra)
    beads = client.get("/api/healthz").json()["collectors"]["beads"]
    assert beads["ok"] is False
    assert beads["optional"] is True
    assert beads["reason_code"] == "config_missing"
    assert "collectors.beads.enabled=false" in beads["detail"]
