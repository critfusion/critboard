"""FastAPI app: /api/snapshot, /api/stream (SSE), history, config, healthz. Static web/ at /."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import quota
from . import settings as settings_mod
from . import update as update_mod
from .collectors import Scheduler
from .collectors.agents import AgentsCollector
from .collectors.analytics import AnalyticsCollector
from .collectors.beads import BeadNotFoundError, BeadsCollector, fetch_bead_detail, validate_bead_id
from .collectors.dispatch import DispatchCollector
from .collectors.kimi import KimiCollector
from .collectors.productivity import ProductivityCollector
from .collectors.remote import RemoteCollector
from .collectors.system import SystemCollector
from .collectors.usage import UsageCollector
from .collectors.worktrees import WorktreesCollector
from .config import Config, load_config
from .ctx import AppContext
from .history import InvalidWindowError, bucket_count, build_grouped_history, parse_window
from .pricing import compute_cost_usd
from .state import SnapshotStore
from .store import Store, zero_fill_daily, zero_fill_hourly
from .tzutil import DEFAULT_TZ, is_valid_timezone
from .version import VersionTracker

logger = logging.getLogger("critdash")
logging.basicConfig(level=os.environ.get("CRITDASH_LOG_LEVEL", "INFO"))

# GET /api/bead/{id} cache: reopening the detail modal for the same bead
# should be cheap, so a successful (or 404) `bd show` is cached for ~30s
# keyed by id. id -> (expires_at_monotonic, detail_dict_or_None, status_code).
_BEAD_DETAIL_TTL_S = 30.0
_bead_detail_cache: dict[str, tuple[float, dict | None, int]] = {}

# POST /api/config/layout|theme write server-side config from a browser
# request. Defaults bind to 127.0.0.1, but a user may bind 0.0.0.0 on a
# LAN -- see AGENTS.md briefing's security section. _MAX_CONFIG_BODY_BYTES
# caps the request body (these documents are a few KB; 1MB is generous
# headroom, not a real limit for a legitimate save) against someone on the
# same network sending an oversized body at an exposed instance.
_MAX_CONFIG_BODY_BYTES = 1_000_000


def _read_layout_timezone(config: Config) -> str:
    """The "timezone" key in config/layout.json (IANA name), defaulting to
    UTC. Read fresh on every call (not cached) so a POST /api/config/layout
    that changes it takes effect immediately, no restart needed. Any
    problem reading/parsing the file, or an unrecognized zone name, falls
    back to UTC rather than 500ing -- this is presentation-only (see
    tzutil.py); it must never break a history query."""
    try:
        with config.layout_path.open() as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_TZ
    if not isinstance(doc, dict):
        return DEFAULT_TZ
    tz = doc.get("timezone")
    if isinstance(tz, str) and is_valid_timezone(tz):
        return tz
    return DEFAULT_TZ


async def _read_config_body(request: Request) -> dict:
    """Shared body handling for the config POST endpoints: size-capped read,
    then JSON-object parse. Raises HTTPException (413/400) rather than
    returning an error value, so callers can just `body = await
    _read_config_body(request)`."""
    raw = await request.body()
    if len(raw) > _MAX_CONFIG_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request body too large")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"malformed JSON body: {exc.msg}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return body


def _check_config_writes_allowed(config: Config) -> None:
    if not config.sources.get("allow_config_writes", True):
        raise HTTPException(
            status_code=403,
            detail="config writes are disabled (allow_config_writes is false in sources.json)",
        )


def _safe_config_path(path: Path, config_dir: Path) -> Path:
    """Defense-in-depth: both config POSTs only ever pass a fixed constant
    path (config.layout_path / config.theme_path), so this can't be tripped
    today -- but it guarantees a write can never land outside config_dir even
    if that ever changes, without echoing a filesystem path back to the
    browser (see AGENTS.md briefing's security section)."""
    resolved = path.resolve()
    if not resolved.is_relative_to(config_dir.resolve()):
        raise HTTPException(status_code=400, detail="invalid config path")
    return resolved


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

    def load_pricing_fresh() -> dict:
        try:
            with config.pricing_path.open() as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return config.pricing

    def on_result(name: str, data: dict) -> None:
        for key, value in data.items():
            if key.startswith("_"):
                continue
            snap.update_path(key, value)
        # unified activity feed, refreshed after any collector that can emit events
        if name in ("beads", "worktrees", "agents", "dispatch"):
            events = [
                {
                    "t": r["ts"], "kind": r["kind"], "severity": r["severity"],
                    "text": r["text"], "ref": r["ref"],
                }
                for r in store.recent_events(50)
            ]
            snap.update_path("events", events)

    def on_health(_name: str, _health) -> None:
        snap.update_sources(scheduler.health_snapshot())

    scheduler = Scheduler(on_result=on_result, on_health=on_health)

    repo_roots = config.sources.get("repo_roots", [])
    beads_collector = BeadsCollector(
        ctx=app_ctx,
        beads_env=config.expand("beads_env"),
        bd_bin=config.expand("bd_bin") or "bd",
        actor=config.sources.get("beads_actor", "critdash"),
        store=store,
    )
    beads_collector.interval_s = config.interval("beads")
    scheduler.register(beads_collector)

    local_host = config.sources.get("host", "localhost")

    herdr_bin = config.sources.get("herdr_bin", "herdr")
    session_active_window_s = config.sources.get("session_active_window_s", 900)
    agents_collector = AgentsCollector(
        ctx=app_ctx, herdr_bin=herdr_bin, store=store, host=local_host,
        session_projects_glob=config.expand("claude_projects_dir") + "/*/*.jsonl",
        session_active_window_s=session_active_window_s,
    )
    agents_collector.interval_s = config.interval("agents")
    scheduler.register(agents_collector)

    worktrees_collector = WorktreesCollector(
        ctx=app_ctx, repo_roots=repo_roots,
        worker_pool=config.sources.get("worktree_worker_pool", 32),
        git_timeout_s=config.sources.get("worktree_git_timeout_s", 5),
        store=store,
        max_depth=config.sources.get("repo_scan_depth", 4),
        prune_dirs=set(config.sources.get("worktree_skip_dirs", [])) or None,
        host=local_host,
    )
    worktrees_collector.interval_s = config.interval("worktrees")
    scheduler.register(worktrees_collector)

    usage_collector = UsageCollector(
        ctx=app_ctx,
        projects_glob=config.expand("claude_projects_dir") + "/*/*.jsonl",
        store=store,
        pricing=config.pricing,
        host=local_host,
    )
    usage_collector.interval_s = config.interval("usage")
    scheduler.register(usage_collector)

    kimi_collector = KimiCollector(
        ctx=app_ctx, store=store, host=local_host,
        kimi_dir=config.sources.get("kimi_dir", "~/.kimi-code"),
        session_active_window_s=session_active_window_s,
    )
    kimi_collector.interval_s = config.interval("kimi")
    scheduler.register(kimi_collector)

    dispatch_collector = DispatchCollector(ctx=app_ctx, overlord_dir=config.expand("overlord_dir"))
    dispatch_collector.interval_s = config.interval("dispatch")
    scheduler.register(dispatch_collector)

    system_collector = SystemCollector(ctx=app_ctx, disk_mounts=["/", "/srv"])
    system_collector.interval_s = config.interval("system")
    scheduler.register(system_collector)

    remote_collector = RemoteCollector(
        ctx=app_ctx,
        hosts=config.sources.get("hosts", []),
        ssh_opts=config.sources.get("ssh_opts", []),
        ssh_timeout_s=config.sources.get("ssh_timeout_s", 45),
        store=store,
        local_host=local_host,
        repo_roots=repo_roots,
        # NOT config.expand() here -- a remote host's "~" must be expanded on
        # THAT host (remote_probe.py does this), never pre-expanded against
        # the local host's own home directory. See RemoteCollector._probe_args.
        claude_projects_dir=config.sources.get("claude_projects_dir", "~/.claude/projects"),
        herdr_bin=herdr_bin,
        disk_mounts=["/", "/srv"],
        worker_pool=config.sources.get("worktree_worker_pool", 32),
        git_timeout_s=config.sources.get("worktree_git_timeout_s", 5),
        max_depth=config.sources.get("repo_scan_depth", 4),
        session_active_window_s=session_active_window_s,
        kimi_dir=config.sources.get("kimi_dir", "~/.kimi-code"),
    )
    remote_collector.interval_s = config.sources.get("remote_interval_s", 120)
    scheduler.register(remote_collector)

    # ProductivityCollector must be registered before AnalyticsCollector so
    # ctx.latest_productivity has a first value as soon as possible -- not
    # load-bearing for correctness (AnalyticsCollector's productivity default
    # is an honest all-zero dict either way), just avoids one empty poll.
    productivity_collector = ProductivityCollector(
        ctx=app_ctx, store=store, host=local_host,
        worker_pool=config.sources.get("worktree_worker_pool", 32),
        excluded_repos=config.sources.get("excluded_repos"),
    )
    productivity_collector.interval_s = config.interval("productivity")
    scheduler.register(productivity_collector)

    analytics_collector = AnalyticsCollector(
        ctx=app_ctx,
        projects_glob=config.expand("claude_projects_dir") + "/*/*.jsonl",
        store=store,
        host=local_host,
    )
    analytics_collector.interval_s = config.interval("analytics")
    scheduler.register(analytics_collector)

    async def pricing_refresh_loop():
        while True:
            await asyncio.sleep(30)
            app_ctx.pricing = load_pricing_fresh()

    async def resync_loop():
        while True:
            await asyncio.sleep(60)
            snap.publish_resync()

    async def ping_loop():
        while True:
            await asyncio.sleep(15)
            snap.publish_ping()

    async def vacuum_loop():
        while True:
            await asyncio.sleep(24 * 3600)
            store.vacuum_old_events(days=30)

    async def version_loop():
        while True:
            await asyncio.sleep(5)
            if version_tracker.refresh():
                snap.set_version(version_tracker.to_dict())

    background_tasks: list[asyncio.Task] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scheduler.start()
        background_tasks.append(asyncio.create_task(pricing_refresh_loop()))
        background_tasks.append(asyncio.create_task(resync_loop()))
        background_tasks.append(asyncio.create_task(ping_loop()))
        background_tasks.append(asyncio.create_task(vacuum_loop()))
        background_tasks.append(asyncio.create_task(version_loop()))
        yield
        for t in background_tasks:
            t.cancel()
        await scheduler.stop()
        store.close()

    app = FastAPI(title="CritBoard", lifespan=lifespan)
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
            or path.startswith("/api/bead/")
        ):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def _settings_block() -> dict:
        """Not layout geometry -- everything a settings panel needs to know
        about server-side state that isn't part of config/layout.json's
        panels/grid. Computed fresh per request (not cached in `snap`) so a
        POST /api/config/layout that changes "timezone" is reflected on the
        very next /api/snapshot or /api/stream connect, no restart needed."""
        return {
            "timezone": _read_layout_timezone(config),
            "allow_config_writes": bool(config.sources.get("allow_config_writes", True)),
            "refresh_preset": config.sources.get("refresh_preset", "normal"),
        }

    def _snapshot_with_settings() -> dict:
        body = dict(snap.full_snapshot())
        body["settings"] = _settings_block()
        return body

    @app.get("/api/snapshot")
    async def get_snapshot():
        return JSONResponse(_snapshot_with_settings())

    @app.get("/api/stream")
    async def stream(request: Request):
        queue = snap.subscribe()

        async def event_gen():
            try:
                # Only the initial frame on a fresh connection carries
                # "settings" -- periodic resync frames (resync_loop, every
                # 60s) call snap.publish_resync() directly and only patch
                # collector-owned data, not config-file-derived state. A
                # client picks up a settings change on its next reconnect or
                # GET /api/snapshot, which is the same cadence a
                # POST /api/config/* response already gives it.
                yield _sse_format("snapshot", _snapshot_with_settings())
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event, data = await asyncio.wait_for(queue.get(), timeout=20.0)
                        yield _sse_format(event, data)
                    except TimeoutError:
                        continue
            finally:
                snap.unsubscribe(queue)

        return StreamingResponse(event_gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    @app.get("/api/history/usage")
    async def history_usage(window: str = "24h", bucket: str = "hour", group_by: str = "none"):
        now = datetime.now(UTC)
        try:
            since_dt, until_dt = parse_window(window, now)
        except InvalidWindowError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if bucket not in ("hour", "day"):
            raise HTTPException(
                status_code=400, detail=f"invalid bucket: {bucket!r} (expected 'hour' or 'day')"
            )

        tz_name = _read_layout_timezone(config)

        if group_by == "none":
            # Unchanged for tz_name="UTC" (the default): the exact query +
            # zero-fill this endpoint has always run, byte-compatible for
            # the existing 48h timeline widget -- only the window string it
            # accepts got richer (see history.py), and now bucket='day' also
            # honors config/layout.json's "timezone" (see
            # Store.usage_timeline_daily_tz).
            since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            count = bucket_count(since_dt, until_dt, bucket, window)
            if bucket == "day":
                daily_rows = store.usage_timeline_daily_tz(since, tz_name)
                return zero_fill_daily(daily_rows, until_dt, count, tz_name)
            return zero_fill_hourly(store.usage_timeline_hourly(since), until_dt, count)

        if group_by not in ("model", "provider", "host"):
            raise HTTPException(
                status_code=400,
                detail=f"invalid group_by: {group_by!r} (expected 'none', 'model', 'provider', or 'host')",
            )
        return JSONResponse(
            build_grouped_history(
                store, app_ctx.pricing, window, bucket, group_by, since_dt, until_dt, tz_name
            )
        )

    @app.get("/api/history/agents")
    async def history_agents(window: str = "24h"):
        try:
            since = _window_to_since(window)
        except InvalidWindowError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        rows = store.agent_status_history(since)
        return [{"t": r["ts"], "agent_id": r["agent_id"], "status": r["status"]} for r in rows]

    @app.get("/api/config/layout")
    async def get_layout():
        if not config.layout_path.exists():
            raise HTTPException(status_code=404, detail="config/layout.json not found")
        with config.layout_path.open() as f:
            return JSONResponse(json.load(f))

    @app.get("/api/config/theme")
    async def get_theme():
        if not config.theme_path.exists():
            raise HTTPException(status_code=404, detail="config/theme.json not found")
        with config.theme_path.open() as f:
            return JSONResponse(json.load(f))

    @app.post("/api/config/layout")
    async def post_layout(request: Request):
        _check_config_writes_allowed(config)
        body = await _read_config_body(request)
        errors = validate_layout(body)
        if errors:
            raise HTTPException(status_code=400, detail={"errors": errors})
        path = _safe_config_path(config.layout_path, config.config_dir)
        try:
            _atomic_write_json(path, body)
        except OSError:
            # Never echo a filesystem path back to the browser (briefing's
            # security section) -- the real path/errno only goes to the log.
            logger.exception("failed to write config/layout.json")
            raise HTTPException(status_code=500, detail="failed to write layout config") from None
        return JSONResponse(body)

    @app.post("/api/config/theme")
    async def post_theme(request: Request):
        _check_config_writes_allowed(config)
        body = await _read_config_body(request)
        errors = validate_theme(body)
        if errors:
            raise HTTPException(status_code=400, detail={"errors": errors})
        path = _safe_config_path(config.theme_path, config.config_dir)
        try:
            _atomic_write_json(path, body)
        except OSError:
            logger.exception("failed to write config/theme.json")
            raise HTTPException(status_code=500, detail="failed to write theme config") from None
        return JSONResponse(body)

    @app.get("/api/settings/suggest")
    async def get_settings_suggest():
        beads_items = (snap.snapshot.get("beads") or {}).get("items") or []
        dispatch_routes = (snap.snapshot.get("dispatch") or {}).get("routes") or []
        human_labels = settings_mod.suggest_human_labels(beads_items, dispatch_routes)
        tz_detected, tz_source = settings_mod.detect_timezone()
        return {
            "human_labels": human_labels,
            "timezone": {"detected": tz_detected, "source": tz_source},
            "quota_providers": quota.provider_availability_buckets(),
        }

    @app.get("/api/bead/{bead_id}")
    async def get_bead_detail(bead_id: str):
        if not validate_bead_id(bead_id):
            raise HTTPException(status_code=400, detail=f"invalid bead id: {bead_id!r}")

        now = time.monotonic()
        cached = _bead_detail_cache.get(bead_id)
        if cached is not None and cached[0] > now:
            _, cached_detail, cached_status = cached
            if cached_status == 404:
                raise HTTPException(status_code=404, detail=f"bead not found: {bead_id}")
            return JSONResponse(cached_detail)

        try:
            detail = await fetch_bead_detail(
                config.expand("beads_env"),
                config.expand("bd_bin") or "bd",
                config.sources.get("beads_actor", "critdash"),
                bead_id,
            )
        except BeadNotFoundError:
            _bead_detail_cache[bead_id] = (now + _BEAD_DETAIL_TTL_S, None, 404)
            raise HTTPException(status_code=404, detail=f"bead not found: {bead_id}") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - never 500 a traceback for a subprocess hiccup
            raise HTTPException(status_code=502, detail=f"bd show failed: {exc}") from exc

        _bead_detail_cache[bead_id] = (now + _BEAD_DETAIL_TTL_S, detail, 200)
        return JSONResponse(detail)

    # AI-provider "remaining quota" check (critdash/quota.py) -- manually
    # triggered only, per the hard rule in that module's docstring. GET never
    # makes a network call; POST does, at most once every
    # quota_refresh_min_interval_s (default 30s, see quota.RefreshState).
    quota_refresh_state = quota.RefreshState()

    @app.get("/api/quota")
    async def get_quota():
        cache = store.get_quota_cache()
        stale_after_s = config.sources.get("quota_stale_after_s", 3600)
        return JSONResponse(quota.build_quota_response(cache, stale_after_s))

    @app.post("/api/quota/refresh")
    async def post_quota_refresh():
        block = (snap.snapshot.get("usage") or {}).get("block") or {}
        try:
            result = await quota.do_refresh(config, store, block, quota_refresh_state)
        except quota.RateLimited as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": str(max(1, int(round(exc.retry_after_s))))},
            ) from exc
        return JSONResponse(result)

    @app.get("/api/healthz")
    async def healthz():
        return {"ok": True, "collectors": scheduler.health_snapshot()}

    @app.get("/api/version")
    async def get_version():
        return snap.snapshot.get("version", version_tracker.to_dict())

    # Self-update (briefing Task 4): GET never touches disk or restarts
    # anything, POST is gated behind allow_self_update (default false, see
    # update.py's module docstring) and is only ever invoked by a human
    # clicking a button -- nothing in this codebase calls it on its own.
    update_check_state = update_mod.CheckState()

    @app.get("/api/update/check")
    async def get_update_check():
        try:
            result = await update_mod.check_for_update(config, config.dashboard_root, update_check_state)
        except update_mod.UpdateError as exc:
            detail = {"reason": exc.reason, "message": exc.message}
            raise HTTPException(status_code=502, detail=detail) from exc
        return JSONResponse(result)

    _UPDATE_APPLY_STATUS = {
        "self_update_disabled": 403,
        "not_a_git_checkout": 409,
        "dirty_working_tree": 409,
        "no_origin_remote": 409,
        "origin_mismatch": 403,
        "fetch_failed": 502,
        "not_fast_forward": 409,
        "reinstall_failed": 500,
        "git_error": 500,
    }

    @app.post("/api/update/apply")
    async def post_update_apply():
        try:
            result = await asyncio.to_thread(update_mod.apply_update, config, config.dashboard_root)
        except update_mod.UpdateError as exc:
            status = _UPDATE_APPLY_STATUS.get(exc.reason, 400)
            detail = {"reason": exc.reason, "message": exc.message}
            raise HTTPException(status_code=status, detail=detail) from exc
        return JSONResponse(result)

    web_dir = config.dashboard_root / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


def _sse_format(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _window_to_delta(window: str) -> timedelta:
    """Kept for callers/tests that want a plain delta. Raises
    InvalidWindowError (a ValueError) for an unparseable window instead of
    silently substituting a default -- see history.parse_window."""
    since, until = parse_window(window, datetime.now(UTC))
    return until - since


def _window_to_since(window: str) -> str:
    since, _until = parse_window(window, datetime.now(UTC))
    return since.strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_layout(doc: dict) -> list[str]:
    """Rejects anything that would make the dashboard unrenderable. Unknown
    top-level keys (e.g. a future "timezone" or settings-panel addition) are
    NOT rejected -- forward compatibility -- only the specific shapes below
    that would break rendering: a panel missing required geometry/identity
    fields, two panels overlapping, a panel extending past the configured
    grid width, a non-string title/timezone, or (see the 'panels' check
    below) an empty panel list that would silently wipe every panel."""
    errors = []
    if not isinstance(doc, dict):
        return ["layout must be a JSON object"]
    if "version" not in doc:
        errors.append("missing 'version'")

    grid = doc.get("grid")
    columns = None
    if "grid" not in doc or not isinstance(grid, dict):
        errors.append("missing or invalid 'grid'")
    else:
        for key in ("columns", "row_height", "gap"):
            if key not in grid:
                errors.append(f"grid missing '{key}'")
        if isinstance(grid.get("columns"), int) and not isinstance(grid.get("columns"), bool):
            if grid["columns"] > 0:
                columns = grid["columns"]
            else:
                errors.append("grid.columns must be a positive integer")

    tz = doc.get("timezone")
    if tz is not None:
        if not isinstance(tz, str):
            errors.append("'timezone' must be a string")
        elif not is_valid_timezone(tz):
            errors.append(f"'timezone' is not a recognized IANA timezone name: {tz!r}")

    panels = doc.get("panels")
    if not isinstance(panels, list):
        errors.append("missing or invalid 'panels' (must be a list)")
    elif not panels:
        # Same data-loss shape as validate_theme's empty-colors bug: an empty
        # list IS a list, so `isinstance(panels, list)` alone lets a request
        # that (accidentally or otherwise) drops every panel overwrite
        # layout.json with a dashboard that has none, with no error. A saved
        # layout must keep at least one panel.
        errors.append("'panels' must not be empty")
    else:
        seen_ids = set()
        rects: list[tuple[int, str, float, float, float, float]] = []
        for idx, panel in enumerate(panels):
            if not isinstance(panel, dict):
                errors.append(f"panels[{idx}] must be an object")
                continue
            for key in ("id", "type", "x", "y", "w", "h"):
                if key not in panel:
                    errors.append(f"panels[{idx}] missing '{key}'")
            pid = panel.get("id")
            if pid in seen_ids:
                errors.append(f"duplicate panel id '{pid}'")
            seen_ids.add(pid)

            title = panel.get("title")
            if title is not None and not isinstance(title, str):
                errors.append(f"panels[{idx}] (id={pid!r}) 'title' must be a string")

            x, y, w, h = panel.get("x"), panel.get("y"), panel.get("w"), panel.get("h")
            geometry_ok = all(
                isinstance(v, int | float) and not isinstance(v, bool) for v in (x, y, w, h)
            )
            if geometry_ok:
                if columns is not None and x + w > columns:
                    errors.append(
                        f"panels[{idx}] (id={pid!r}) extends past grid columns "
                        f"({x}+{w} > {columns})"
                    )
                rects.append((idx, pid, x, y, w, h))

        for i in range(len(rects)):
            idx_a, id_a, xa, ya, wa, ha = rects[i]
            for j in range(i + 1, len(rects)):
                idx_b, id_b, xb, yb, wb, hb = rects[j]
                overlaps = xa < xb + wb and xb < xa + wa and ya < yb + hb and yb < ya + ha
                if overlaps:
                    errors.append(
                        f"panels[{idx_a}] (id={id_a!r}) overlaps panels[{idx_b}] (id={id_b!r})"
                    )
    return errors


#  Every `--color-<token>` custom property the frontend actually reads --
#  either directly via `var(--color-<token>)` in web/css/style.css, or
#  JS-constructed (utils.js statusColorVar/severityColorVar/priorityColorVar,
#  interpolated into an inline `style` attribute) in web/js/*.js. Derived
#  with:
#    grep -ohE -- '--color-[a-zA-Z0-9_-]+' web/js/*.js web/css/style.css \
#      | sort -u
#  This is exactly the 28-key set config/theme.json's "colors" object ships
#  with today -- if the frontend starts reading a new token, add it here too,
#  and if a token stops being read anywhere, it can come out.
_REQUIRED_COLOR_TOKENS = (
    "accent-amber", "accent-amber-dim", "accent-cyan", "accent-cyan-dim",
    "agent-glow", "bg", "bg-elevated", "bg-panel", "bg-panel-hover",
    "border", "border-bright", "grid-line",
    "priority-0", "priority-1", "priority-2", "priority-3",
    "severity-crit", "severity-info", "severity-warn",
    "status-crit", "status-done", "status-idle", "status-ok",
    "status-warn", "status-working",
    "text", "text-dim", "text-faint",
)


def validate_theme(doc: dict) -> list[str]:
    """Same "cannot brick the dashboard" contract as validate_layout, scaled
    to theme.json's shape: unknown top-level keys are allowed (forward
    compatibility), but a known section with the wrong type -- one that the
    frontend would fail to apply -- is rejected.

    `colors` is REQUIRED (bug fix): every check used to be gated behind
    `if <field> is not None`, so a document that omitted a field validated
    it, requiring nothing. `POST /api/config/theme` with `{}` therefore
    passed with zero errors and silently overwrote theme.json, wiping every
    colour token and both presets -- while the page kept rendering, because
    style.css's own `:root` block still has a hardcoded value for each
    token, so the loss wasn't visible. `colors` must now be present, be a
    non-empty object, and contain every token in _REQUIRED_COLOR_TOKENS (the
    set the frontend actually consumes -- nothing invented, nothing that
    isn't read somewhere in web/). `fonts`/`radius`/`density`/`motion`/
    `reload_banner`/`_presets` stay optional and forward-compatible, exactly
    as before: type-checked only when present."""
    errors = []
    if not isinstance(doc, dict):
        return ["theme must be a JSON object"]

    colors = doc.get("colors")
    if colors is None:
        errors.append("'colors' is required")
    elif not isinstance(colors, dict):
        errors.append("'colors' must be an object")
    elif not colors:
        errors.append("'colors' must not be empty")
    else:
        for key, value in colors.items():
            if not isinstance(value, str):
                errors.append(f"colors.{key} must be a string")
        missing = [t for t in _REQUIRED_COLOR_TOKENS if t not in colors]
        if missing:
            errors.append("colors missing required token(s): " + ", ".join(missing))

    fonts = doc.get("fonts")
    if fonts is not None:
        if not isinstance(fonts, dict):
            errors.append("'fonts' must be an object")
        else:
            for key, value in fonts.items():
                if not isinstance(value, str):
                    errors.append(f"fonts.{key} must be a string")

    reload_banner = doc.get("reload_banner")
    if reload_banner is not None:
        if not isinstance(reload_banner, dict):
            errors.append("'reload_banner' must be an object")
        elif "enabled" in reload_banner and not isinstance(reload_banner["enabled"], bool):
            errors.append("reload_banner.enabled must be a boolean")

    presets = doc.get("_presets")
    if presets is not None and not isinstance(presets, dict):
        errors.append("'_presets' must be an object")

    return errors


def _atomic_write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_bytes(path.read_bytes())
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


app = build_app()
