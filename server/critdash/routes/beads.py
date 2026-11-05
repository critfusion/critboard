"""Bead detail, bead reply, and the bead-reply settings switch."""

from __future__ import annotations

import json
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import bead_reply as bead_reply_mod
from ..collectors.beads import BeadNotFoundError, fetch_bead_detail, validate_bead_id, validate_label
from ..collectors.beads import availability_issue as beads_availability_issue
from . import Deps
from ._common import (
    _atomic_write_json,
    _check_config_writes_allowed,
    _read_config_body,
    _read_human_labels,
    _safe_config_path,
)

logger = logging.getLogger("critdash")

# GET /api/bead/{id} cache: reopening the detail modal for the same bead
# should be cheap, so a successful (or 404) `bd show` is cached for ~30s
# keyed by id. id -> (expires_at_monotonic, detail_dict_or_None, status_code).
_BEAD_DETAIL_TTL_S = 30.0
_bead_detail_cache: dict[str, tuple[float, dict | None, int]] = {}


def _register_detail(app: FastAPI, d: Deps) -> None:
    config, _resolve_bd_bin = d.config, d.resolve_bd_bin

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
                _resolve_bd_bin(),
                config.sources.get("beads_actor", "critdash"),
                bead_id,
                beads_dir=config.expand("beads_dir"),
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


def _bead_reply_disabled_error() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "reason": "bead_reply_disabled",
            "message": "bead_reply is disabled (set bead_reply.enabled=true in config/sources.json)",
        },
    )


def _bead_reply_error(exc: bead_reply_mod.BeadReplyError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code, detail={"reason": exc.reason, "message": exc.message}
    )


def _register_reply(app: FastAPI, d: Deps) -> None:
    config, scheduler, _resolve_bd_bin = d.config, d.scheduler, d.resolve_bd_bin

    # bead reply (bead popup: read comments, send back to an agent, or
    # close -- see bead_reply.py's module docstring for the full contract).
    # Off by default (config/sources.json "bead_reply": {"enabled": false}),
    # and every write additionally requires allow_config_writes -- same
    # reasoning as POST /api/config/layout|theme.

    @app.get("/api/bead/{bead_id}/comments")
    async def get_bead_comments(bead_id: str):
        # Defect-1-style reload (see POST /api/update/apply): a hand edit to
        # bead_reply.enabled must take effect on the very next request.
        config.reload_sources()
        cfg = bead_reply_mod.resolve_config(config.sources)
        if not cfg["enabled"]:
            raise _bead_reply_disabled_error()
        if not validate_bead_id(bead_id):
            raise HTTPException(status_code=400, detail=f"invalid bead id: {bead_id!r}")

        human_labels = _read_human_labels(config)
        actor = bead_reply_mod.resolve_actor(cfg, human_labels)
        beads_env = config.expand("beads_env")
        beads_dir = config.expand("beads_dir")
        bd_bin = _resolve_bd_bin()

        try:
            raw = await bead_reply_mod.fetch_bead_raw(bd_bin, beads_env, actor, beads_dir, bead_id)
            comments = await bead_reply_mod.fetch_bead_comments(
                bd_bin, beads_env, actor, beads_dir, bead_id
            )
        except bead_reply_mod.BeadReplyError as exc:
            raise _bead_reply_error(exc) from exc

        labels = raw.get("labels") or []
        present = [label for label in human_labels if label in labels]
        route = bead_reply_mod.choose_route(
            raw.get("created_by") or "", cfg["routes"], cfg["default_route"]
        )
        return JSONResponse(
            {
                "comments": comments,
                "status": raw.get("status"),
                "labels": labels,
                "human_labels_present": present,
                "route_preview": route,
            }
        )

    @app.post("/api/bead/{bead_id}/reply")
    async def post_bead_reply(bead_id: str, request: Request):
        _check_config_writes_allowed(config)
        # Same reload-before-write as every other config-gated POST.
        config.reload_sources()
        cfg = bead_reply_mod.resolve_config(config.sources)
        if not cfg["enabled"]:
            raise _bead_reply_disabled_error()
        if not validate_bead_id(bead_id):
            raise HTTPException(status_code=400, detail=f"invalid bead id: {bead_id!r}")

        body = await _read_config_body(request)
        action = body.get("action")
        if action not in ("send_back", "close"):
            raise HTTPException(
                status_code=400, detail="'action' must be 'send_back' or 'close'"
            )
        text = body.get("text", "")
        if not isinstance(text, str):
            raise HTTPException(status_code=400, detail="'text' must be a string")
        if len(text) > bead_reply_mod.MAX_TEXT_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"'text' too long (max {bead_reply_mod.MAX_TEXT_LEN} characters)",
            )
        text = text.strip()
        if action == "send_back" and not text:
            raise HTTPException(
                status_code=400, detail="'text' is required (non-empty) to send a bead back"
            )

        human_labels = _read_human_labels(config)
        actor = bead_reply_mod.resolve_actor(cfg, human_labels)
        beads_env = config.expand("beads_env")
        beads_dir = config.expand("beads_dir")
        bd_bin = _resolve_bd_bin()

        try:
            raw = await bead_reply_mod.fetch_bead_raw(bd_bin, beads_env, actor, beads_dir, bead_id)
        except bead_reply_mod.BeadReplyError as exc:
            raise _bead_reply_error(exc) from exc

        labels = raw.get("labels") or []
        present = [label for label in human_labels if label in labels]
        # Close is allowed on an open OR blocked human bead (a blocked bead can
        # still be answered by the human). Send-back stays open-only: relabelling
        # a blocked bead could hand an agent work it is still waiting on.
        allowed_status = ("open", "blocked") if action == "close" else ("open",)
        if raw.get("status") not in allowed_status or not present:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "not_actionable",
                    "message": (
                        "bead is not actionable: it needs a human label and status open "
                        "or blocked to close, or open to send back -- refusing to avoid "
                        "reopening or relabelling a bead an agent already took"
                    ),
                },
            )

        try:
            if action == "send_back":
                route = bead_reply_mod.choose_route(
                    raw.get("created_by") or "", cfg["routes"], cfg["default_route"]
                )
                for label in [*present, route]:
                    if not validate_label(label):
                        raise HTTPException(
                            status_code=400, detail=f"invalid label: {label!r}"
                        )
                result = await bead_reply_mod.do_send_back(
                    bd_bin, beads_env, actor, beads_dir, bead_id, text, present, route
                )
            else:
                result = await bead_reply_mod.do_close(
                    bd_bin, beads_env, actor, beads_dir, bead_id, text
                )
        except bead_reply_mod.BeadReplyError as exc:
            raise _bead_reply_error(exc) from exc

        # Immediate re-collect so the bead card reflects the write right
        # away, instead of waiting for the beads collector's own 30s tick.
        try:
            await scheduler.run_once("beads")
        except Exception:  # noqa: BLE001 - the write already succeeded; a
            # failed/inactive beads collector must not turn a successful
            # bd write into a 500. The card still updates on the next tick.
            logger.warning("bead reply: post-write beads re-collect failed", exc_info=True)

        return JSONResponse(result)


def _register_settings(app: FastAPI, d: Deps) -> None:
    config, _resolve_bd_bin, beads_dir = d.config, d.resolve_bd_bin, d.beads_dir

    # Narrow settings-panel endpoint for bead_reply.enabled -- same pattern
    # as /api/settings/updates above: the browser can only ever flip the one
    # allow-listed boolean key, never routes/actor/default_route. Those stay
    # hand-edit-JSON-only (this is a public repo -- a fork's route labels
    # and actor name are site-specific, unlike a plain on/off switch) but
    # are reported back here read-only so the settings panel can show what
    # the popup will actually do.
    _BEAD_REPLY_SETTINGS_KEYS = {"enabled"}

    def _bead_reply_settings_view() -> dict:
        cfg = bead_reply_mod.resolve_config(config.sources)
        # Same availability check _apply_enablement used to decide whether
        # to register the beads collector at all (not the scheduler's
        # health_snapshot -- under a plain TestClient, and for a few
        # seconds after a real startup, a healthy-but-never-polled
        # collector still reports ok=False there, which would misreport a
        # perfectly configured host as "beads not configured").
        beads_issue = beads_availability_issue(_resolve_bd_bin(), beads_dir)
        return {
            "enabled": cfg["enabled"],
            # No specific bead's human labels apply here -- resolve_actor's
            # first-human-label branch never fires, so this reports exactly
            # "configured actor, else the fixed fallback".
            "actor": bead_reply_mod.resolve_actor(cfg, []),
            "default_route": cfg["default_route"],
            "beads_configured": beads_issue is None,
        }

    @app.get("/api/settings/bead-reply")
    async def get_settings_bead_reply():
        # Defect-1-style reload (see GET /api/settings/updates): a hand
        # edit to bead_reply must show up here immediately, since this is
        # what the settings panel reads to decide what to display.
        config.reload_sources()
        return JSONResponse(_bead_reply_settings_view())

    @app.post("/api/settings/bead-reply")
    async def post_settings_bead_reply(request: Request):
        _check_config_writes_allowed(config)
        body = await _read_config_body(request)

        unknown = sorted(set(body) - _BEAD_REPLY_SETTINGS_KEYS)
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"unknown key(s) (not settable here): {', '.join(unknown)}"
            )

        errors = []
        if "enabled" in body and not isinstance(body["enabled"], bool):
            errors.append("'enabled' must be a boolean")
        if errors:
            raise HTTPException(status_code=400, detail={"errors": errors})

        try:
            with config.sources_path.open() as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            doc = dict(config.sources)

        # Nested merge -- bead_reply is an object that also holds actor/
        # routes/default_route. `doc["bead_reply"] = {"enabled": ...}` would
        # silently wipe a hand-configured routes list; instead copy whatever
        # is already there (or start from {} if the block is absent/not a
        # dict) and only touch "enabled".
        existing = doc.get("bead_reply")
        merged = dict(existing) if isinstance(existing, dict) else {}
        if "enabled" in body:
            merged["enabled"] = body["enabled"]
        doc["bead_reply"] = merged

        path = _safe_config_path(config.sources_path, config.config_dir)
        try:
            _atomic_write_json(path, doc)
        except OSError:
            logger.exception("failed to write config/sources.json")
            raise HTTPException(status_code=500, detail="failed to write bead reply settings") from None

        # Keep the running process in sync immediately, same reasoning as
        # POST /api/settings/updates -- GET /api/bead/{id}/comments, POST
        # /api/bead/{id}/reply, and the snapshot's settings.bead_reply_enabled
        # (see _settings_block above) all call bead_reply_mod.resolve_config
        # fresh on every request, so mutating config.sources in place here
        # means the very next request picks up the change, no restart. Same
        # existing-value-preserving merge as the on-disk write above.
        in_memory = config.sources.get("bead_reply")
        in_memory = dict(in_memory) if isinstance(in_memory, dict) else {}
        if "enabled" in body:
            in_memory["enabled"] = body["enabled"]
        config.sources["bead_reply"] = in_memory

        # Keep the running process in sync immediately, same reasoning as
        # POST /api/settings/updates -- GET /api/bead/{id}/comments, POST
        # /api/bead/{id}/reply, and the snapshot's settings.bead_reply_enabled
        # (see _settings_block above) all call bead_reply_mod.resolve_config
        # fresh on every request, so mutating config.sources in place here
        # means the very next request picks up the change, no restart. Same
        # existing-value-preserving merge as the on-disk write above.

        return JSONResponse(_bead_reply_settings_view())


def register(app: FastAPI, d: Deps) -> None:
    _register_detail(app, d)
    _register_reply(app, d)
    _register_settings(app, d)
