"""Background loops started by the app lifespan."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from . import update as update_mod
from .config import Config
from .ctx import AppContext
from .state import SnapshotStore
from .store import Store
from .version import VersionTracker
from .wiring import Wiring

logger = logging.getLogger("critdash")


def background_loops(
    config: Config,
    store: Store,
    app_ctx: AppContext,
    snap: SnapshotStore,
    wiring: Wiring,
    version_tracker: VersionTracker,
) -> list[Callable[[], Coroutine[Any, Any, None]]]:
    """The loop functions, in the order the lifespan starts them."""
    scheduler = wiring.scheduler
    _inactive_health = wiring.inactive_health
    _redetect_specs = wiring.redetect_specs

    def load_pricing_fresh() -> dict:
        try:
            with config.pricing_path.open() as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return config.pricing

    collector_redetect_interval_s = config.sources.get("collector_redetect_interval_s", 60)

    async def collector_redetect_loop():
        """Bug 2's "re-detect periodically... so installing bd later makes
        the panel come alive without editing config": re-run every inactive
        collector's auto-detection on a slow interval and, the moment its
        dependency shows up, register and start it live -- no restart. Only
        touches collectors that are inactive AND not held off by an explicit
        `enabled: false` override (_apply_enablement never adds those to
        _redetect_specs in the first place)."""
        while True:
            await asyncio.sleep(collector_redetect_interval_s)
            activated = False
            for spec in _redetect_specs:
                name = spec["name"]
                if name not in _inactive_health:
                    continue  # already activated on an earlier tick
                issue = spec["detect"]()
                if issue is not None:
                    continue
                scheduler.register(spec["build"]())
                scheduler.start_one(name)
                del _inactive_health[name]
                activated = True
                logger.info("collector %s: dependency now available, activated", name)
            if activated:
                snap.update_sources({**_inactive_health, **scheduler.health_snapshot()})

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

    # Periodic background update check (briefing Task 2). Runs a tick
    # immediately on startup (so /api/snapshot has real values without
    # waiting a full interval), then re-reads update_check_interval_s from
    # config.sources fresh on every iteration -- so a POST
    # /api/settings/updates change (which mutates config.sources in place,
    # see post_settings_updates) takes effect on the very next tick, no
    # restart needed. auto_apply is read from the same tick's result;
    # apply_update() keeps every existing safety check (including
    # allow_self_update, still required) -- see update.py's module
    # docstring.
    async def update_check_loop():
        while True:
            try:
                result = await update_mod.periodic_update_check(config, config.dashboard_root, store)
                snap.update_path("update", result)
                if result.get("update_available") and result.get("auto_apply"):
                    try:
                        await asyncio.to_thread(update_mod.apply_update, config, config.dashboard_root)
                    except update_mod.UpdateError as exc:
                        logger.warning("periodic auto-apply refused: %s: %s", exc.reason, exc.message)
            except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
                logger.exception("periodic update check failed")
            interval = float(
                config.sources.get("update_check_interval_s", update_mod.DEFAULT_UPDATE_CHECK_INTERVAL_S)
            )
            interval = max(interval, update_mod.MIN_UPDATE_CHECK_INTERVAL_S)
            await asyncio.sleep(interval)

    return [
        pricing_refresh_loop,
        resync_loop,
        ping_loop,
        vacuum_loop,
        version_loop,
        collector_redetect_loop,
        update_check_loop,
    ]
