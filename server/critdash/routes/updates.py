"""Self-update check and apply, and the update settings."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import update as update_mod
from . import Deps
from ._common import _atomic_write_json, _check_config_writes_allowed, _read_config_body, _safe_config_path

logger = logging.getLogger("critdash")


def _register_check_and_apply(app: FastAPI, d: Deps) -> None:
    config, store, snap = d.config, d.store, d.snap

    # Self-update (briefing Task 4): GET never touches disk or restarts
    # anything, POST is gated behind allow_self_update (default false, see
    # update.py's module docstring) and is only ever invoked by a human
    # clicking a button -- nothing in this codebase calls it on its own.
    update_check_state = update_mod.CheckState()

    def _sync_update_state_from_manual_check(result: dict) -> None:
        """One source of truth (briefing fix C): GET /api/update/check used
        to keep its own cache entirely separate from the periodic check's
        persisted state that /api/snapshot's `update` object (and so the
        banner) reads -- a manual check right after a push would correctly
        say "up to date" while the banner, still driven by the last
        periodic tick, kept showing "update available" for up to
        `update_check_interval_s`. Write this check's result into both the
        live snapshot (so the banner updates on the very next render, no
        restart/tick needed) and the periodic check's persisted cache (so a
        restart doesn't lose it either). Skipped entirely when no
        update_repo is configured -- there is nothing to reconcile."""
        if result.get("repo_configured") is False:
            return
        prior_stored = store.get_update_check_state() or {}
        # Keep the persisted ETag only if this check confirms `latest`
        # hasn't moved since it was captured -- it's still valid then, and
        # keeping it lets the next periodic tick get a free 304. If `latest`
        # DID move, that ETag is for the old sha and would be wrong to keep.
        etag = prior_stored.get("etag") if prior_stored.get("latest") == result.get("latest") else None
        merged = {
            "repo": update_mod.resolve_repo(config) or None,
            "branch": config.sources.get("update_branch") or update_mod.DEFAULT_UPDATE_BRANCH,
            "current": result.get("current"),
            "latest": result.get("latest"),
            "behind": result.get("behind"),
            "relation": result.get("relation", "unknown"),
            "update_available": result.get("update_available", False),
            "checked_at": result.get("checked_at"),
            "last_error": None,
            "enabled": bool(config.sources.get("update_check_enabled", True)),
            "auto_apply": bool(config.sources.get("update_auto_apply", False)),
        }
        snap.update_path("update", merged)
        store.set_update_check_state({
            "etag": etag, "latest": merged["latest"], "behind": merged["behind"],
            "relation": merged["relation"], "current": merged["current"],
            "checked_at": merged["checked_at"], "last_error": None,
        })

    @app.get("/api/update/check")
    async def get_update_check():
        try:
            result = await update_mod.check_for_update(config, config.dashboard_root, update_check_state)
        except update_mod.UpdateError as exc:
            detail = {"reason": exc.reason, "message": exc.message}
            raise HTTPException(status_code=502, detail=detail) from exc
        _sync_update_state_from_manual_check(result)
        return JSONResponse(result)

    _UPDATE_APPLY_STATUS = {
        "self_update_disabled": 403,
        "update_repo_not_configured": 409,
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
        # Defect 1 (macOS install report): a hand edit to config/sources.json
        # (e.g. flipping "allow_self_update" to true by hand) must take
        # effect without a restart -- reload BEFORE apply_update() evaluates
        # its allow_self_update gate, or the running process would still see
        # the stale in-memory value and refuse the very edit that was meant
        # to unblock it.
        config.reload_sources()
        try:
            result = await asyncio.to_thread(update_mod.apply_update, config, config.dashboard_root)
        except update_mod.UpdateError as exc:
            status = _UPDATE_APPLY_STATUS.get(exc.reason, 400)
            detail = {"reason": exc.reason, "message": exc.message}
            raise HTTPException(status_code=status, detail=detail) from exc
        return JSONResponse(result)


def _register_settings(app: FastAPI, d: Deps) -> None:
    config, snap = d.config, d.snap

    # Narrow settings-panel endpoint for update preferences (briefing Task
    # 3): unlike POST /api/config/layout|theme, the browser can never write
    # arbitrary sources.json content here -- only these four keys, and only
    # these four, ever change. sources.json also holds ssh hosts and
    # filesystem paths, which must stay off-limits to a browser POST.
    # allow_self_update (defect 2, macOS install report) is the master
    # switch: it used to be hand-edit-JSON-only, which is how the user got
    # stuck refusing their own "Update now" click.
    _UPDATE_SETTINGS_KEYS = {"check_enabled", "check_interval_s", "auto_apply", "allow_self_update"}

    def _update_settings_view() -> dict:
        u = snap.snapshot.get("update") or {}
        return {
            "check_enabled": bool(config.sources.get("update_check_enabled", True)),
            "check_interval_s": int(
                config.sources.get("update_check_interval_s", update_mod.DEFAULT_UPDATE_CHECK_INTERVAL_S)
            ),
            "auto_apply": bool(config.sources.get("update_auto_apply", False)),
            "allow_self_update": bool(config.sources.get("allow_self_update", False)),
            # Defect 4: report the EFFECTIVE repo via the same resolution
            # update.py's check/apply paths actually use (DEFAULT_UPDATE_REPO
            # when the key is absent, "" only when explicitly disabled) --
            # `config.sources.get("update_repo") or ""` used to report ""
            # (looks unconfigured) even when the default was about to be used.
            "repo": update_mod.resolve_repo(config),
            "branch": config.sources.get("update_branch") or update_mod.DEFAULT_UPDATE_BRANCH,
            "current": u.get("current"),
            "latest": u.get("latest"),
            "behind": u.get("behind"),
            "update_available": bool(u.get("update_available", False)),
            "checked_at": u.get("checked_at"),
            "last_error": u.get("last_error"),
        }

    @app.get("/api/settings/updates")
    async def get_settings_updates():
        # Defect 1: same reload as POST /api/update/apply -- a hand edit to
        # config/sources.json (e.g. flipping allow_self_update) must show up
        # here immediately, since this is what the settings panel reads to
        # decide what to display/send.
        config.reload_sources()
        return JSONResponse(_update_settings_view())

    @app.post("/api/settings/updates")
    async def post_settings_updates(request: Request):
        _check_config_writes_allowed(config)
        body = await _read_config_body(request)

        unknown = sorted(set(body) - _UPDATE_SETTINGS_KEYS)
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"unknown key(s) (not settable here): {', '.join(unknown)}"
            )

        errors = []
        if "check_enabled" in body and not isinstance(body["check_enabled"], bool):
            errors.append("'check_enabled' must be a boolean")
        if "auto_apply" in body and not isinstance(body["auto_apply"], bool):
            errors.append("'auto_apply' must be a boolean")
        if "allow_self_update" in body and not isinstance(body["allow_self_update"], bool):
            errors.append("'allow_self_update' must be a boolean")
        if "check_interval_s" in body:
            v = body["check_interval_s"]
            if not isinstance(v, int) or isinstance(v, bool):
                errors.append("'check_interval_s' must be an integer")
        if errors:
            raise HTTPException(status_code=400, detail={"errors": errors})

        updates: dict[str, Any] = {}
        if "check_enabled" in body:
            updates["update_check_enabled"] = body["check_enabled"]
        if "auto_apply" in body:
            updates["update_auto_apply"] = body["auto_apply"]
        if "allow_self_update" in body:
            updates["allow_self_update"] = body["allow_self_update"]
        if "check_interval_s" in body:
            # Floor, not ceiling -- nobody can configure a rate-limit
            # violation from the settings UI (see update.py's
            # MIN_UPDATE_CHECK_INTERVAL_S).
            updates["update_check_interval_s"] = max(
                int(body["check_interval_s"]), update_mod.MIN_UPDATE_CHECK_INTERVAL_S
            )

        try:
            with config.sources_path.open() as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            doc = dict(config.sources)
        doc.update(updates)
        path = _safe_config_path(config.sources_path, config.config_dir)
        try:
            _atomic_write_json(path, doc)
        except OSError:
            logger.exception("failed to write config/sources.json")
            raise HTTPException(status_code=500, detail="failed to write update settings") from None

        # Keep the running process in sync immediately -- update_check_loop
        # re-reads config.sources.get(...) fresh every tick, so mutating
        # this in-memory dict (not just the on-disk file) means the next
        # tick picks up the change with no restart. POST /api/config/layout
        # and /api/config/theme never need this: neither writes sources.json.
        config.sources.update(updates)

        return JSONResponse(_update_settings_view())


def register(app: FastAPI, d: Deps) -> None:
    _register_check_and_apply(app, d)
    _register_settings(app, d)
