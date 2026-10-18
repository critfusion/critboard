"""Context module: the small answer an agent reads at session start.

Pure functions, no I/O and no clock of their own (the caller passes `now`).
Every section names its `source`. A fact the data does not state is the
string "unknown"; a section that could not be read says why instead of
looking empty. Only BookStack-curated records and bead assignment fields go
out (SPEC.md, "Context data boundary"). Page bodies never go out.

The serialized body stays within `max_bytes`: lists are dropped, lowest
priority first, and each drop is named in `degraded`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from . import workflow
from .infrastructure import UNKNOWN

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
DEFAULT_MAX_BYTES = 16384
MIN_MAX_BYTES, MAX_MAX_BYTES = 8192, 262144
MAX_LIST = 20          # per list: standards, runbooks, ownership items, availability rows
MAX_FIELD_BYTES = 200
# Lowest priority first.
DROP_ORDER = ("runbooks", "standards", "ownership", "availability")
AVAILABILITY_STATES = ("observed", "stale", "unknown", "available", "unavailable", "degraded", "limited")
DEFAULT_CACHE_TTL_S, MAX_CACHE_TTL_S = 60, 3600
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Defence in depth, not the boundary: the allowlist projections below are the
# boundary. A key with one of these names never leaves, even if a projection
# starts to pass it through.
FORBIDDEN_KEYS = frozenset({
    "session", "sessions", "agents", "transcript", "transcripts", "memory", "cost", "spend", "token",
    "token_file", "auth", "auth_path", "values", "detail", "local_path", "cwd", "host", "hosts",
    "cost_estimate_today_usd", "cost_basis", "authorization", "secret", "api_key", "apikey", "password",
    "passwd", "credential", "credentials", "session_id", "sessionid", "token_path", "file_path", "filepath",
    "memory_path", "transcript_path", "home",
})


def book_slugs(cfg: dict) -> tuple[str, str]:
    def one(key: str, default: str) -> str:
        v = cfg.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else default
    return one("projects_book_slug", "projects"), one("runbooks_book_slug", "runbooks")


def max_bytes(cfg: dict) -> int:
    v = cfg.get("max_bytes", DEFAULT_MAX_BYTES)
    if isinstance(v, bool) or not isinstance(v, int):
        return DEFAULT_MAX_BYTES
    return min(max(v, MIN_MAX_BYTES), MAX_MAX_BYTES)


def _text(value, limit: int) -> str:
    """Text cut to `limit` BYTES of UTF-8 at a character boundary. A lone
    surrogate becomes "?" and a control character a space, so the JSON
    escape of a field never exceeds twice its bytes."""
    if not isinstance(value, str):
        return ""
    text = _CONTROL.sub(" ", value.encode("utf-8", "replace").decode("utf-8")).strip()
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore").strip()


def _s(value, limit: int = MAX_FIELD_BYTES) -> str:
    return _text(value, limit) or UNKNOWN


def _opt(value, limit: int = MAX_FIELD_BYTES) -> str | None:
    return _text(value, limit) or None


def scrub(obj):
    """Copy of `obj` without any dict key in FORBIDDEN_KEYS, at any depth."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if str(k).lower() not in FORBIDDEN_KEYS}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def render(body: dict) -> bytes:
    """The exact bytes the endpoint sends, and the bytes the cap measures."""
    text = json.dumps(scrub(body), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return text.encode("utf-8", "replace")  # a surrogate that got through is "?", never a 500


# ---------------- wiki-backed sections ----------------

def _wiki_state(result) -> tuple[dict | None, str | None]:
    """('ok', payload) -> (payload, None); ('error', code) -> (None, state).
    state is `not_configured` or `unavailable`."""
    kind, value = result
    if kind == "ok":
        return value, None
    return None, "not_configured" if value == "not_configured" else "unavailable"


def _failed(source: str, state: str, code: str) -> dict:
    return {"source": source, "state": state, "error": code}


def _page_ref(r: dict) -> dict:
    return {"id": r.get("id"), "title": _s(r.get("title")), "url": _s(r.get("url")),
            "updated_at": _s(r.get("updated_at"))}


def entrypoint(project: str, result, book_slug: str) -> dict:
    source = f"bookstack:{book_slug}"
    payload, state = _wiki_state(result)
    if payload is None:
        return _failed(source, state, result[1])
    records = [r for r in payload.get("records") or [] if isinstance(r, dict)]
    # Tag match first, then a slug that starts with "<project>-".
    hit = next((r for r in records if project in [p.lower() for p in r.get("projects") or []]), None)
    match = "tag" if hit else None
    if hit is None:
        hit = next((r for r in records
                    if isinstance(r.get("slug"), str) and r["slug"].startswith(project + "-")), None)
        match = "slug_prefix" if hit else None
    if hit is None:
        # A book cut at the page cap may hold the page: that is not "no match".
        return {"source": source, "state": "incomplete" if payload.get("truncated") else "no_match"}
    src = hit.get("source") or {}
    canon = hit.get("canonical") or {}
    provenance = {
        "repo": _s(src.get("repo")), "path": _s(src.get("path")), "git_sha": _s(src.get("git_sha")),
        "status": _s(hit.get("status")),
    }
    for key in ("path_note", "git_sha_note"):
        if _opt(src.get(key)):
            provenance[key] = _opt(src.get(key))
    return {
        "source": source, "state": "ok", "match": match, **_page_ref(hit), "provenance": provenance,
        "canonical_repo": _s(canon.get("repo")), "canonical_location": _s(canon.get("location")),
        "dev_prod_boundary": _s(canon.get("boundary")),
    }


def standards(result, book_slug: str) -> dict:
    source = f"bookstack:{book_slug}"
    payload, state = _wiki_state(result)
    if payload is None:
        return _failed(source, state, result[1])
    records = [r for r in payload.get("records") or [] if isinstance(r, dict)]
    items = [{**_page_ref(r), "status": _s(r.get("status"))} for r in records[:MAX_LIST]]
    return {"source": source, "state": "ok", "items": items,
            "truncated": len(records) > MAX_LIST or bool(payload.get("truncated"))}


def runbooks(project: str, result, book_slug: str) -> dict:
    source = f"bookstack:{book_slug}"
    payload, state = _wiki_state(result)
    if payload is None:
        return _failed(source, state, result[1])
    hits = [r for r in payload.get("records") or [] if isinstance(r, dict)
            and (project in [p.lower() for p in r.get("projects") or []]
                 or "all" in [s.lower() for s in r.get("scopes") or []])]
    cut = bool(payload.get("truncated"))
    out = {"source": source, "state": "ok", "items": [_page_ref(r) for r in hits[:MAX_LIST]],
           "truncated": len(hits) > MAX_LIST or cut}
    if not hits and not cut:
        out["runbooks_note"] = "untagged"
    return out


# ---------------- bead-backed and quota-backed sections ----------------

def _ownership_item(a: dict) -> dict:
    return {
        "id": _s(a.get("id")), "title": _s(a.get("title")), "assignee": _s(a.get("assignee")),
        "coder": _s(a.get("coder")), "reviewers": [_s(n) for n in a.get("reviewers") or []],
        "approvers": [_s(n) for n in a.get("approvers") or []], "review_gate": _s(a.get("review_gate")),
    }


def ownership(project: str, snapshot: dict) -> dict:
    beads = (snapshot.get("beads") or {}).get("items")
    mine = [b for b in beads if isinstance(b, dict) and project in (b.get("labels") or [])] \
        if isinstance(beads, list) else []
    # No agents are passed: the sessions never enter this section.
    items = workflow.assignments(mine, [])
    return {"source": "beads", "state": "ok", "items": [_ownership_item(a) for a in items[:MAX_LIST]],
            "total": len(items), "truncated": len(items) > MAX_LIST}


def beads_stale(snapshot: dict) -> bool:
    src = (snapshot.get("sources") or {}).get("beads")
    return not (isinstance(src, dict) and src.get("ok") is True and src.get("stale") is False)


def availability(quota_response: dict, wf_cfg: dict, now) -> dict:
    """Freshness only: provider, basis, state and the three timestamps."""
    ttl = workflow.availability_ttl_s(wf_cfg)
    rows = workflow.availability(quota_response, wf_cfg.get("owner_reported"), ttl, now)["rows"]
    keep = ("provider", "basis", "state", "observed_at", "expires_at", "expired")
    out = []
    for r in rows[:MAX_LIST]:
        row = {k: r.get(k) for k in keep}
        row["provider"] = _s(row["provider"], 64)
        # Owner-reported state is free text: only a fixed set passes.
        row["state"] = row["state"] if row["state"] in AVAILABILITY_STATES else UNKNOWN
        out.append(row)
    return {"source": "quota", "ttl_s": ttl, "rows": out, "truncated": len(rows) > MAX_LIST}


# ---------------- assembly and the size cap ----------------

def _page_revisions(sections: dict) -> list[dict]:
    seen: dict = {}
    entry = sections["entrypoint"]
    refs = [entry] if entry.get("state") == "ok" else []
    for name in ("standards", "runbooks"):
        refs += sections[name].get("items") or []
    for r in refs:
        if isinstance(r.get("id"), int) and r["id"] not in seen:
            seen[r["id"]] = {"id": r["id"], "updated_at": r.get("updated_at", UNKNOWN)}
    return list(seen.values())


def _degraded(sections: dict, stale_beads: bool, cut: dict) -> list[str]:
    out: list[str] = []
    for name in ("entrypoint", "standards", "runbooks"):
        state = sections[name].get("state")
        reason = {"not_configured": "knowledge_not_configured",
                  "unavailable": "knowledge_unavailable"}.get(state)
        if reason and reason not in out:
            out.append(reason)
    if sections["entrypoint"].get("state") == "no_match":
        out.append("no_entrypoint")
    for name in ("projects", "runbooks"):
        if cut.get(name):
            out.append(f"{name}_truncated")
    if stale_beads:
        out.append("beads_stale")
    return out


def build(project: str, *, projects, runbook_result, policy, books: tuple[str, str, str], snapshot: dict,
          quota_response: dict, wf_cfg: dict, commit: str | None, now: datetime, limit: int) -> dict:
    """The /api/context body. `projects`, `runbook_result` and `policy` are
    ('ok', book payload) or ('error', KnowledgeError code). `books` is
    (projects, runbooks, policy) book slugs."""
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    sections = {
        "entrypoint": entrypoint(project, projects, books[0]),
        "standards": standards(policy, books[2]),
        "runbooks": runbooks(project, runbook_result, books[1]),
        "ownership": ownership(project, snapshot),
        "availability": availability(quota_response, wf_cfg, now),
    }
    cut = {name: kind == "ok" and bool(v.get("truncated"))
           for name, (kind, v) in (("projects", projects), ("runbooks", runbook_result))}
    base_degraded = _degraded(sections, beads_stale(snapshot), cut)

    def assemble(dropped: list[str]) -> dict:
        shown = dict(sections)
        for name in dropped:
            shown[name] = {"source": sections[name]["source"], "state": "dropped",
                           "rows" if name == "availability" else "items": []}
        return {
            "ok": True, "project": project, **shown,
            "revision": {"critboard": _s(commit, 64), "pages": _page_revisions(shown), "fetched_at": now_iso},
            "degraded": base_degraded + [f"dropped:{n}" for n in dropped],
        }

    dropped: list[str] = []
    body = assemble(dropped)
    for name in DROP_ORDER:
        if len(render(body)) <= limit:
            break
        dropped.append(name)
        body = assemble(dropped)
    if len(render(body)) > limit:
        # Last resort, so the cap always holds: the entrypoint cannot fit.
        body = {
            "ok": True, "project": project, "state": "over_cap",
            "revision": {"critboard": _s(commit, 64), "pages": [], "fetched_at": now_iso},
            "degraded": base_degraded + [f"dropped:{n}" for n in DROP_ORDER] + ["entrypoint_dropped", "over_cap"],
        }
    return body


class TtlCache:
    """In-process cache of successful BookStack book reads. Entries are
    [expires_at, value]; the caller passes the clock."""

    def __init__(self) -> None:
        self.entries: dict = {}

    def get(self, key, now: float):
        entry = self.entries.get(key)
        if entry is None:
            return None
        if now >= entry[0]:
            del self.entries[key]
            return None
        return entry[1]

    def put(self, key, value, now: float, ttl_s: float) -> None:
        if ttl_s > 0:
            self.entries[key] = [now + ttl_s, value]


def cache_ttl_s(cfg: dict) -> int:
    v = cfg.get("cache_ttl_s", DEFAULT_CACHE_TTL_S)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return DEFAULT_CACHE_TTL_S
    return min(v, MAX_CACHE_TTL_S)

