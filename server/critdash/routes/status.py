"""Health and version."""

from __future__ import annotations

from fastapi import FastAPI

from . import Deps


def register(app: FastAPI, d: Deps) -> None:
    snap, scheduler = d.snap, d.scheduler
    _inactive_health, version_tracker = d.inactive_health, d.version_tracker

    @app.get("/api/healthz")
    async def healthz():
        return {"ok": True, "collectors": {**_inactive_health, **scheduler.health_snapshot()}}

    @app.get("/api/version")
    async def get_version():
        return snap.snapshot.get("version", version_tracker.to_dict())
