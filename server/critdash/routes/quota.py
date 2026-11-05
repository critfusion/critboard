"""Provider quota: cached read and manual refresh."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .. import quota
from . import Deps


def register(app: FastAPI, d: Deps) -> None:
    config, store = d.config, d.store

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
        try:
            result = await quota.do_refresh(config, store, quota_refresh_state)
        except quota.RateLimited as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": str(max(1, int(round(exc.retry_after_s))))},
            ) from exc
        return JSONResponse(result)
