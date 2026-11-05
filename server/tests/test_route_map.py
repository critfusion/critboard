"""Pin the HTTP surface of build_app().

These tests guard the split of build_app() into route modules: the same
paths, methods, handler names, query/path parameters, middleware and
static mount must come out, in the same order. Handler behavior is pinned
by the endpoint tests in the other test files.
"""

from fastapi.routing import APIRoute
from starlette.routing import Mount

from critdash.main import app

# (path, methods, handler name), in registration order.
EXPECTED_API_ROUTES = [
    ("/api/snapshot", {"GET"}, "get_snapshot"),
    ("/api/stream", {"GET"}, "stream"),
    ("/api/history/usage", {"GET"}, "history_usage"),
    ("/api/history/agents", {"GET"}, "history_agents"),
    ("/api/config/layout", {"GET"}, "get_layout"),
    ("/config/layout.json", {"GET"}, "get_layout"),
    ("/api/config/theme", {"GET"}, "get_theme"),
    ("/config/theme.json", {"GET"}, "get_theme"),
    ("/api/config/layout", {"POST"}, "post_layout"),
    ("/api/config/theme", {"POST"}, "post_theme"),
    ("/api/settings/suggest", {"GET"}, "get_settings_suggest"),
    ("/api/bead/{bead_id}", {"GET"}, "get_bead_detail"),
    ("/api/bead/{bead_id}/comments", {"GET"}, "get_bead_comments"),
    ("/api/bead/{bead_id}/reply", {"POST"}, "post_bead_reply"),
    ("/api/settings/bead-reply", {"GET"}, "get_settings_bead_reply"),
    ("/api/settings/bead-reply", {"POST"}, "post_settings_bead_reply"),
    ("/api/quota", {"GET"}, "get_quota"),
    ("/api/quota/refresh", {"POST"}, "post_quota_refresh"),
    ("/api/healthz", {"GET"}, "healthz"),
    ("/api/version", {"GET"}, "get_version"),
    ("/api/update/check", {"GET"}, "get_update_check"),
    ("/api/update/apply", {"POST"}, "post_update_apply"),
    ("/api/settings/updates", {"GET"}, "get_settings_updates"),
    ("/api/settings/updates", {"POST"}, "post_settings_updates"),
    ("/api/modules", {"GET"}, "get_modules"),
    ("/api/knowledge/status", {"GET"}, "knowledge_status"),
    ("/api/knowledge/search", {"GET"}, "knowledge_search"),
    ("/api/knowledge/page/{page_id}", {"GET"}, "knowledge_page"),
    ("/api/infrastructure", {"GET"}, "infrastructure"),
    ("/api/workflow", {"GET"}, "workflow_view"),
    ("/api/context", {"GET"}, "context_view"),
]

# Declared query/path parameters per (method, path): (name, location, required, default).
EXPECTED_PARAMS = {
    ("get", "/api/history/usage"): [
        ("window", "query", False, "24h"),
        ("bucket", "query", False, "hour"),
        ("group_by", "query", False, "none"),
    ],
    ("get", "/api/history/agents"): [("window", "query", False, "24h")],
    ("get", "/api/bead/{bead_id}"): [("bead_id", "path", True, None)],
    ("get", "/api/bead/{bead_id}/comments"): [("bead_id", "path", True, None)],
    ("post", "/api/bead/{bead_id}/reply"): [("bead_id", "path", True, None)],
    ("get", "/api/knowledge/search"): [("q", "query", False, "")],
    ("get", "/api/knowledge/page/{page_id}"): [("page_id", "path", True, None)],
    ("get", "/api/context"): [("project", "query", False, ""), ("also", "query", False, "")],
}


def test_api_routes_are_pinned_in_order():
    got = [(r.path, set(r.methods), r.name) for r in app.routes if isinstance(r, APIRoute)]
    assert got == EXPECTED_API_ROUTES


def test_route_parameters_are_pinned():
    paths = app.openapi()["paths"]
    got = {}
    for path, ops in paths.items():
        for method, op in ops.items():
            params = op.get("parameters") or []
            if params:
                got[(method, path)] = [
                    (p["name"], p["in"], p.get("required", False), p["schema"].get("default"))
                    for p in params
                ]
    assert got == EXPECTED_PARAMS


def test_search_query_keeps_max_length():
    op = app.openapi()["paths"]["/api/knowledge/search"]["get"]
    (q,) = op["parameters"]
    assert q["schema"]["maxLength"] == 200


def test_static_mount_is_last():
    last = app.routes[-1]
    assert isinstance(last, Mount)
    assert last.path == "" and last.name == "web"


def test_middleware_stack_is_pinned():
    names = [m.cls.__name__ for m in app.user_middleware]
    assert names == ["BaseHTTPMiddleware", "CORSMiddleware"]


def test_app_state_hooks_exist():
    for attr in ("snap", "store", "knowledge", "served", "context_cache"):
        assert hasattr(app.state, attr), attr


def test_collector_registration_order_is_pinned(tmp_path, monkeypatch):
    """Every collector forced on: the scheduler sees them in this order.
    No lifespan runs here, so no collector starts."""
    import json

    from critdash import config as config_mod
    from critdash import main as main_mod

    names = ["beads", "dispatch", "remote"]
    sources = dict(config_mod.DEFAULT_SOURCES)
    sources["db_path"] = str(tmp_path / "test.db")
    sources["collectors"] = {n: {"enabled": True} for n in names}
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "sources.json").write_text(json.dumps(sources))
    (config_dir / "sources.example.json").write_text(json.dumps(config_mod.DEFAULT_SOURCES))
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)

    built = main_mod.build_app()
    order = list(built.state.snap.full_snapshot()["sources"])
    assert order == [
        "beads", "agents", "worktrees", "usage", "kimi", "provider_usage",
        "dispatch", "system", "remote", "productivity", "analytics",
    ]
    built.state.store.close()
