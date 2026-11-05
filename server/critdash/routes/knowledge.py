"""Opt-in modules: knowledge, infrastructure, workflow, and context."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from .. import context as context_mod
from .. import quota, workflow
from .. import update as update_mod
from ..infrastructure import stale_after_days
from ..knowledge import BookStackAdapter, KnowledgeError, read_token
from ..knowledge import now_iso as knowledge_now_iso
from ..modules import enabled_modules, module_config
from . import Deps

_KNOWLEDGE_HTTP = {
    "not_found": 404, "not_configured": 503, "token_file_missing": 503,
    "token_file_permissions": 503, "token_file_invalid": 503, "book_not_found": 503,
}


def _knowledge_allowed_hosts(src: dict) -> set[str]:
    """Loopback, the bind host (unless a wildcard), the configured host
    and host-list names, plus modules.knowledge.allowed_hosts (names
    only, no port). A wildcard bind (0.0.0.0) cannot know which names
    reach it: list those in allowed_hosts."""
    names = {"localhost", "127.0.0.1", "::1", str(src.get("host") or "")}
    bind = str(src.get("bind_host") or "")
    if bind not in ("0.0.0.0", "::", ""):
        names.add(bind)
    names.update(str(h.get("name") or "") for h in src.get("hosts") or [] if isinstance(h, dict))
    extra = (module_config(src, "knowledge") or {}).get("allowed_hosts")
    if isinstance(extra, list):
        names.update(str(x) for x in extra)
    return {n.strip("[]").lower() for n in names if n}


def _knowledge_refused(knowledge: BookStackAdapter, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message, "http_status": 403},
         "status": knowledge.status()},
        status_code=403, headers={"Cache-Control": "no-store"},
    )


def _knowledge_error(knowledge: BookStackAdapter, err: KnowledgeError) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": {"code": err.code, "message": err.message, "http_status": err.http_status},
            "status": knowledge.status(),
        },
        status_code=_KNOWLEDGE_HTTP.get(err.code, 502),
        headers={"Cache-Control": "no-store"},
    )


def _knowledge_ok(knowledge: BookStackAdapter, data: dict) -> JSONResponse:
    return JSONResponse(
        {"ok": True, **data, "fetched_at": knowledge_now_iso(), "status": knowledge.status()},
        headers={"Cache-Control": "no-store"},
    )


def _make_knowledge_gate(config, knowledge: BookStackAdapter) -> Callable[..., JSONResponse | None]:
    def _knowledge_gate(request: Request, module: str = "knowledge") -> JSONResponse | None:
        """None = allowed; else the 403 body in the normal ok:false shape.
        `module` is the module whose routes these are (Infrastructure shares
        the Knowledge allowlist but has its own on/off switch)."""
        config.reload_sources()
        if module_config(config.sources, module) is None:
            raise HTTPException(status_code=404, detail="Not Found")
        # CORS above allows any origin, so a page on another site could read
        # these responses. Browsers send Origin on cross-site requests: refuse
        # any whose Origin host differs from the Host the request came to.
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return _knowledge_refused(knowledge, "origin_not_allowed", "cross-origin request refused")
        # DNS rebinding: an attacker's name that resolves to this machine
        # passes the Origin==Host check above (both are the attacker's name).
        # Refuse any Host that is not one of ours. Knowledge routes only.
        host = (urlsplit("//" + (request.headers.get("host") or "")).hostname or "").lower()
        if host not in _knowledge_allowed_hosts(config.sources):
            return _knowledge_refused(knowledge, "host_not_allowed", "host not allowed")
        return None

    return _knowledge_gate


def register(app: FastAPI, d: Deps) -> None:
    config = d.config

    # ---- opt-in modules (see modules.py; off unless sources.json enables) ----
    knowledge = BookStackAdapter(config.sources)
    app.state.knowledge = knowledge  # exposed for tests: inject a fake transport

    @app.get("/api/modules")
    async def get_modules():
        config.reload_sources()
        return JSONResponse({"enabled": enabled_modules(config.sources)})

    _knowledge_gate = _make_knowledge_gate(config, knowledge)
    served = context_mod.ServedMarker()
    app.state.served = served  # exposed for tests

    _register_knowledge(app, knowledge, served, _knowledge_gate)
    _register_infrastructure(app, knowledge, _knowledge_gate)
    _register_workflow(app, d, knowledge, _knowledge_gate)
    _register_context(app, d, knowledge, served, _knowledge_gate)


def _register_knowledge(
    app: FastAPI,
    knowledge: BookStackAdapter,
    served: context_mod.ServedMarker,
    _knowledge_gate: Callable[..., JSONResponse | None],
) -> None:
    @app.get("/api/knowledge/status")
    async def knowledge_status(request: Request):
        if (refused := _knowledge_gate(request)) is not None:
            return refused
        try:
            await knowledge.probe()
        except KnowledgeError as err:
            return _knowledge_error(knowledge, err)
        return _knowledge_ok(knowledge, {})

    @app.get("/api/knowledge/search")
    async def knowledge_search(request: Request, q: str = Query("", max_length=200)):
        if (refused := _knowledge_gate(request)) is not None:
            return refused
        try:
            data = await knowledge.search(q)
            for r in data["results"]:
                r["served"] = served.status(r["id"], r.get("updated_at"))
            return _knowledge_ok(knowledge, data)
        except KnowledgeError as err:
            return _knowledge_error(knowledge, err)

    @app.get("/api/knowledge/page/{page_id}")
    async def knowledge_page(request: Request, page_id: int):
        if (refused := _knowledge_gate(request)) is not None:
            return refused
        if page_id < 1:
            raise HTTPException(status_code=404, detail="Not Found")
        try:
            data = await knowledge.page(page_id)
            data["served"] = served.status(page_id, data.get("updated_at"))
            return _knowledge_ok(knowledge, data)
        except KnowledgeError as err:
            return _knowledge_error(knowledge, err)


def _register_infrastructure(
    app: FastAPI,
    knowledge: BookStackAdapter,
    _knowledge_gate: Callable[..., JSONResponse | None],
) -> None:
    @app.get("/api/infrastructure")
    async def infrastructure(request: Request):
        if (refused := _knowledge_gate(request, "infrastructure")) is not None:
            return refused
        try:
            return _knowledge_ok(knowledge, await knowledge.infrastructure())
        except KnowledgeError as err:
            return _knowledge_error(knowledge, err)


def _register_workflow(
    app: FastAPI,
    d: Deps,
    knowledge: BookStackAdapter,
    _knowledge_gate: Callable[..., JSONResponse | None],
) -> None:
    config, store, snap = d.config, d.store, d.snap

    @app.get("/api/workflow")
    async def workflow_view(request: Request):
        if (refused := _knowledge_gate(request, "workflow")) is not None:
            return refused
        now = datetime.now(UTC)
        cfg = module_config(config.sources, "workflow") or {}
        # The policy half may fail (wiki off, down, no such book) while the
        # observed half still answers: each failure stays in its own section.
        try:
            policy = workflow.project_policy(await knowledge.workflow_policy(now))
        except KnowledgeError as err:
            policy = workflow.policy_unavailable(err.code, err.message)
        stale_after_s = config.sources.get("quota_stale_after_s", 3600)
        body = workflow.build(
            snap.snapshot,
            quota.build_quota_response(store.get_quota_cache(), stale_after_s),
            cfg,
            policy,
            now,
        )
        return JSONResponse(
            {"ok": True, **body, "fetched_at": knowledge_now_iso(), "status": knowledge.status()},
            headers={"Cache-Control": "no-store"},
        )


def _register_context(
    app: FastAPI,
    d: Deps,
    knowledge: BookStackAdapter,
    served: context_mod.ServedMarker,
    _knowledge_gate: Callable[..., JSONResponse | None],
) -> None:
    config, store, snap = d.config, d.store, d.snap

    context_cache = context_mod.TtlCache()
    app.state.context_cache = context_cache  # exposed for tests: age or inspect entries

    @app.get("/api/context")
    async def context_view(request: Request, project: str = Query(""), also: str = Query("")):
        """Session-start context for one project. Same gate as /api/workflow.
        See SPEC.md, "Context endpoint"."""
        if (refused := _knowledge_gate(request, "context")) is not None:
            return refused
        if not context_mod.SLUG_RE.fullmatch(project):
            return JSONResponse(
                {"ok": False, "error": {"code": "invalid_project",
                                        "message": "project must match ^[a-z0-9][a-z0-9._-]{0,63}$"}},
                status_code=400, headers={"Cache-Control": "no-store"})
        # A repeated parameter is refused: FastAPI would keep only the last value.
        repeated = len(request.query_params.getlist("also")) > 1
        aliases = None if repeated else context_mod.parse_also(also)
        if aliases is None:
            return JSONResponse(
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_also",
                        "message": "also: at most 5 comma-separated labels, each a valid project slug",
                    },
                },
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        now = datetime.now(UTC)
        cfg = module_config(config.sources, "context") or {}
        wf_cfg = module_config(config.sources, "workflow") or {}
        books = (*context_mod.book_slugs(cfg), workflow.policy_book_slug(wf_cfg))
        stale_days = stale_after_days(cfg)

        ttl = context_mod.cache_ttl_s(cfg)

        async def read(slug: str):
            # Only a successful read is cached; an error is read again next time.
            # Local checks first, as on a cold read: a cached read must not outlive the
            # module, its wiki, a usable token file or the token it was read with.
            try:
                base_url, _ = knowledge._settings()
                token = read_token(knowledge._token_path())
            except KnowledgeError as err:
                return "error", err.code
            if ttl == 0:
                context_cache.entries.clear()  # caching was switched off: drop old snapshots
                key = None
            else:
                # Entries are bound to the credential by a hash of the token and its path.
                # The token text is never kept.
                path = os.path.realpath(os.path.expanduser(knowledge._token_path()))
                fp = hashlib.sha256(f"{path}\0{token}".encode()).hexdigest()
                context_cache.bind((base_url, fp))  # a new wiki or credential: drop the old entries
                key = (base_url, fp, slug, stale_days)
                if (hit := context_cache.get(key, time.monotonic(), max_age=ttl)) is not None:
                    return "ok", hit
            try:
                payload = await knowledge.book(slug, now, stale_days)
            except KnowledgeError as err:
                return "error", err.code
            if key is not None:
                context_cache.put(key, payload, time.monotonic(), ttl)
            return "ok", payload

        projects, runbook_result, policy = await asyncio.gather(*(read(b) for b in books))
        commit = (snap.snapshot.get("update") or {}).get("current") \
            or (await asyncio.to_thread(update_mod.git_identity, config.dashboard_root))["commit"]
        stale_after_s = config.sources.get("quota_stale_after_s", 3600)
        body = context_mod.build(
            project,
            also=aliases,
            projects=projects,
            runbook_result=runbook_result,
            policy=policy,
            books=books,
            snapshot=snap.snapshot,
            quota_response=quota.build_quota_response(store.get_quota_cache(), stale_after_s),
            wf_cfg=wf_cfg,
            commit=commit,
            now=now,
            limit=context_mod.max_bytes(cfg),
        )
        served.record(body["revision"]["pages"], project, body["revision"]["fetched_at"])
        return Response(context_mod.render(body), media_type="application/json",
                        headers={"Cache-Control": "no-store"})
