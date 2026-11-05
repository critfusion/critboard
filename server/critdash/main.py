"""FastAPI app: /api/snapshot, /api/stream (SSE), history, config, healthz. Static web/ at /.

build_app() wires the collectors (wiring.py), the background loops
(background.py) and the route modules (routes/), in that order.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .background import background_loops
from .config import load_config
from .config_schema import _REQUIRED_COLOR_TOKENS, validate_layout, validate_theme
from .ctx import AppContext
from .pricing import compute_cost_usd
from .routes import Deps
from .routes import beads as beads_routes
from .routes import config as config_routes
from .routes import history as history_routes
from .routes import knowledge as knowledge_routes
from .routes import quota as quota_routes
from .routes import snapshot as snapshot_routes
from .routes import status as status_routes
from .routes import updates as updates_routes
from .routes._common import _atomic_write_json, _safe_config_path
from .routes.history import _window_to_delta, _window_to_since
from .state import SnapshotStore
from .store import Store
from .version import VersionTracker
from .wiring import wire_collectors

# Re-exported: tests and older callers import these from critdash.main.
__all__ = [
    "_REQUIRED_COLOR_TOKENS",
    "_atomic_write_json",
    "_safe_config_path",
    "_window_to_delta",
    "_window_to_since",
    "app",
    "build_app",
    "validate_layout",
    "validate_theme",
]

logger = logging.getLogger("critdash")
logging.basicConfig(level=os.environ.get("CRITDASH_LOG_LEVEL", "INFO"))

# Route modules, in registration order. tests/test_route_map.py pins it.
_ROUTE_MODULES = (
    snapshot_routes,
    history_routes,
    config_routes,
    beads_routes,
    quota_routes,
    status_routes,
    updates_routes,
    knowledge_routes,
)


def build_app() -> FastAPI:
    config = load_config()
    store = Store(config.db_path)
    app_ctx = AppContext(config=config, store=store, pricing=dict(config.pricing))
    snap = SnapshotStore(host=config.sources.get("host", "localhost"))

    # Task 1: re-cost every historical usage_events row against the current
    # (corrected) pricing.json rates, using the stored per-token columns --
    # otherwise the all-time/7d/30d totals stay wrong forever even though
    # newly-ingested rows are billed correctly. Rows ingested before the
    # `speed` column existed have speed=NULL (billed at the standard rate);
    # their original fast-mode status was never persisted.
    def _recost(model, inp, out, cache_read, cw5m, cw1h, speed):
        return compute_cost_usd(app_ctx.pricing, model, inp, out, cache_read, cw5m, cw1h, speed=speed)

    _recosted_rows = store.recost_all(_recost)
    logger.info("recosted %d historical usage_events rows against config/pricing.json", _recosted_rows)

    version_tracker = VersionTracker(config.dashboard_root)
    snap.set_version(version_tracker.to_dict(), publish=False)

    wiring = wire_collectors(config, store, app_ctx, snap)
    scheduler = wiring.scheduler
    loops = background_loops(config, store, app_ctx, snap, wiring, version_tracker)

    background_tasks: list[asyncio.Task] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scheduler.start()
        for loop in loops:
            background_tasks.append(asyncio.create_task(loop()))
        yield
        for t in background_tasks:
            t.cancel()
        await scheduler.stop()
        store.close()

    app = FastAPI(title="CritBoard", lifespan=lifespan)
    app.state.snap = snap  # exposed for tests: driving snap.publish_resync() directly
    app.state.store = store  # exposed for tests: inspecting persisted update-check state etc.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:9999", "http://127.0.0.1:9999", "*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def no_cache_for_reloadables(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if (
            path == "/"
            or path.endswith((".html", ".js", ".css"))
            or path.startswith("/api/config/")
            or path in ("/config/layout.json", "/config/theme.json")
            or path.startswith("/api/bead/")
        ):
            response.headers["Cache-Control"] = "no-cache"
        return response

    deps = Deps(
        config=config,
        store=store,
        app_ctx=app_ctx,
        snap=snap,
        scheduler=scheduler,
        inactive_health=wiring.inactive_health,
        resolve_bd_bin=wiring.resolve_bd_bin,
        beads_dir=wiring.beads_dir,
        version_tracker=version_tracker,
    )
    for routes in _ROUTE_MODULES:
        routes.register(app, deps)

    web_dir = config.dashboard_root / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


app = build_app()
