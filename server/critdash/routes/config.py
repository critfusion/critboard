"""Layout and theme documents, and settings-panel suggestions."""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import quota
from .. import settings as settings_mod
from ..config_schema import validate_layout, validate_theme
from . import Deps
from ._common import _atomic_write_json, _check_config_writes_allowed, _read_config_body, _safe_config_path

logger = logging.getLogger("critdash")


def register(app: FastAPI, d: Deps) -> None:
    config, snap = d.config, d.snap

    # The fixture harness (web/dev.html, index.html?fixture=1) fetches
    # config/layout.json and config/theme.json as static paths. web/config is
    # a symlink to ../config, which the StaticFiles mount below will not
    # follow, so these two paths are answered here instead. Only these two:
    # mounting config/ whole would also serve sources.json (hosts, paths).
    @app.get("/config/layout.json")
    @app.get("/api/config/layout")
    async def get_layout():
        if not config.layout_path.exists():
            raise HTTPException(status_code=404, detail="config/layout.json not found")
        with config.layout_path.open() as f:
            return JSONResponse(json.load(f))

    @app.get("/config/theme.json")
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
        worktrees = snap.snapshot.get("worktrees") or []
        human_labels = settings_mod.suggest_human_labels(beads_items, dispatch_routes, worktrees)
        tz_detected, tz_source = settings_mod.detect_timezone()
        return {
            "human_labels": human_labels,
            "timezone": {"detected": tz_detected, "source": tz_source},
            "quota_providers": quota.provider_availability_buckets(),
        }
