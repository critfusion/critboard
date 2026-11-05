"""Usage and agent-status history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ..history import InvalidWindowError, bucket_count, build_grouped_history, parse_window
from ..store import zero_fill_daily, zero_fill_hourly
from . import Deps
from ._common import _read_layout_timezone


def register(app: FastAPI, d: Deps) -> None:
    config, store, app_ctx = d.config, d.store, d.app_ctx

    @app.get("/api/history/usage")
    async def history_usage(window: str = "24h", bucket: str = "hour", group_by: str = "none"):
        now = datetime.now(UTC)
        tz_name = _read_layout_timezone(config)
        try:
            since_dt, until_dt = parse_window(window, now, tz_name)
        except InvalidWindowError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if bucket not in ("hour", "day"):
            raise HTTPException(
                status_code=400, detail=f"invalid bucket: {bucket!r} (expected 'hour' or 'day')"
            )

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
            since = _window_to_since(window, _read_layout_timezone(config))
        except InvalidWindowError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        rows = store.agent_status_history(since)
        return [{"t": r["ts"], "agent_id": r["agent_id"], "status": r["status"]} for r in rows]


def _window_to_delta(window: str) -> timedelta:
    """Kept for callers/tests that want a plain delta. Raises
    InvalidWindowError (a ValueError) for an unparseable window instead of
    silently substituting a default -- see history.parse_window."""
    since, until = parse_window(window, datetime.now(UTC))
    return until - since


def _window_to_since(window: str, tz_name: str = "UTC") -> str:
    since, _until = parse_window(window, datetime.now(UTC), tz_name)
    return since.strftime("%Y-%m-%dT%H:%M:%SZ")
