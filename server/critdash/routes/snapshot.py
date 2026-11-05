"""Snapshot, live stream, and the settings block both carry."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import bead_reply as bead_reply_mod
from . import Deps
from ._common import _read_layout_timezone


def register(app: FastAPI, d: Deps) -> None:
    config, snap = d.config, d.snap

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
            # bead_reply (bead popup reply/send-back/close, off by default --
            # see bead_reply.py): the frontend needs this one flag to decide
            # whether to fetch comments / show the reply form at all, so it
            # doesn't hit a 403 on every bead popup open on an install that
            # never enabled the feature.
            "bead_reply_enabled": bool(
                bead_reply_mod.resolve_config(config.sources)["enabled"]
            ),
        }

    def _snapshot_with_settings() -> dict:
        body = dict(snap.full_snapshot())
        body["settings"] = _settings_block()
        return body

    # state.py doesn't know about config-derived settings, so hand it the
    # callable: every "snapshot" SSE event the store publishes -- the
    # initial /api/stream frame (still built via _snapshot_with_settings
    # above) AND every periodic resync_loop frame (snap.publish_resync(),
    # every 60s) -- then carries "settings" too. See BUG 1 in the fix
    # history: resync frames used to replace the whole snapshot WITHOUT
    # settings, so the frontend's reply box (and timezone) silently vanished
    # about a minute after the page loaded.
    snap.set_settings_provider(_settings_block)

    @app.get("/api/snapshot")
    async def get_snapshot():
        return JSONResponse(_snapshot_with_settings())

    @app.get("/api/stream")
    async def stream(request: Request):
        queue = snap.subscribe()

        async def event_gen():
            try:
                # Every "snapshot" event -- this initial frame AND every
                # periodic resync frame (resync_loop, every 60s, via
                # snap.publish_resync()) -- replaces state.data wholesale on
                # the frontend, so both must carry "settings". This frame is
                # built directly via _snapshot_with_settings(); resync frames
                # get "settings" from the provider registered above with
                # snap.set_settings_provider(_settings_block).
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


def _sse_format(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
