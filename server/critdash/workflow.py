"""Workflow module: written policy next to what the agents are doing.

Pure functions, no I/O and no clock of their own (the caller passes `now`).
Four sections are built separately and never merged into one verdict:

  policy        BookStack pages: the INTENDED workflow.
  observed      live sessions, with role / runner / model / provider / account
                / host as separate fields.
  assignments   in-progress beads and the review gate each one names.
  availability  provider quota readings, each with observed_at, expires_at
                and a basis.

Rule: a field the data does not state is the string "unknown". A missing
reviewer is `review_gate: "missing"`, never a pass. An expired reading is
`stale`, its numbers dropped. A cost figure is an estimate and says so.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .infrastructure import MAX_TEXT_CHARS, UNKNOWN

DEFAULT_TTL_S = 1800
MIN_TTL_S, MAX_TTL_S = 60, 86400
MAX_OWNER_REPORTS = 50
ROLES = ("orchestrator", "coder", "reviewer", "worker")
BASES = ("provider-observed", "owner-reported", "unknown")
# Family prefix of a model id -> provider. Only what the id itself states.
_MODEL_PROVIDERS = {"claude": "anthropic", "gpt": "openai", "grok": "xai", "kimi": "moonshot",
                    "gemini": "google"}
_BEAD_LABEL_FIELDS = {"coder": "coder", "reviewer": "reviewers", "approver": "approvers",
                      "reviewed": "reviewed_refs"}
_ROLE_RE = re.compile(r"^\s*role:\s*(" + "|".join(ROLES) + r")\b", re.I)


def policy_book_slug(cfg: dict) -> str:
    v = cfg.get("policy_book_slug")
    return v.strip() if isinstance(v, str) and v.strip() else "workflow-policy-projections"


def availability_ttl_s(cfg: dict) -> int:
    v = cfg.get("availability_ttl_s", DEFAULT_TTL_S)
    if isinstance(v, bool) or not isinstance(v, int) or v < MIN_TTL_S:
        return DEFAULT_TTL_S
    return min(v, MAX_TTL_S)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value).strip()[:limit] if isinstance(value, str | int | float) and not isinstance(value, bool) else ""
    return text or UNKNOWN


def _when(text) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


# ---------------- policy ----------------

def project_policy(book_payload: dict) -> dict:
    """Infrastructure-style book read -> the intended-policy section. Each
    page keeps its provenance and declared status; none is upgraded."""
    pages = []
    for r in book_payload.get("records") or []:
        src = r.get("source") or {}
        pages.append({
            "id": r.get("id"), "title": r.get("title") or UNKNOWN, "url": r.get("url"),
            "status": r.get("status") or UNKNOWN, "status_reason": r.get("status_reason"),
            "status_conflict": r.get("status_conflict"),
            "source": {"repo": src.get("repo", UNKNOWN), "repo_href": src.get("repo_href"),
                       "path": src.get("path", UNKNOWN), "git_sha": src.get("git_sha", UNKNOWN)},
            "last_verified": r.get("last_verified", UNKNOWN),
            "updated_at": r.get("updated_at", UNKNOWN),
        })
    return {"state": "ok", "label": "intended policy", "book": book_payload.get("book"),
            "pages": pages, "total": book_payload.get("total", len(pages)),
            "truncated": bool(book_payload.get("truncated")),
            "stale_after_days": book_payload.get("stale_after_days"), "error": None}


def policy_unavailable(code: str, message: str) -> dict:
    """The policy half could not be read. The observed half is unaffected."""
    return {"state": code, "label": "intended policy", "book": None, "pages": [], "total": 0,
            "truncated": False, "stale_after_days": None,
            "error": {"code": code, "message": message}}


# ---------------- observed ----------------

def _role(agent: dict) -> str:
    """A role only from an explicit `role:<name>` marker at the start of the
    session title. A role word elsewhere in a title is not a statement."""
    m = _ROLE_RE.match(str(agent.get("title") or ""))
    return m.group(1).lower() if m else UNKNOWN


def _provider(model) -> str:
    if not isinstance(model, str) or not model.strip():
        return UNKNOWN
    model = model.strip().lower()
    if "/" in model:
        return _text(model.split("/", 1)[0], 64)
    for prefix, provider in _MODEL_PROVIDERS.items():
        if model.startswith(prefix):
            return provider
    return UNKNOWN


def observed_sessions(agents) -> list[dict]:
    """Collector agents -> sessions with separate role/runner/model/provider/
    account/host. `account` and `coordinated` have no collector behind them
    yet, so they are always "unknown" (SPEC.md lists them for Phase 3)."""
    out = []
    for a in agents if isinstance(agents, list) else []:
        if not isinstance(a, dict):
            continue
        cost = a.get("cost_today_usd")
        out.append({
            "id": _text(a.get("id")),
            "role": _role(a),
            "runner": _text(a.get("kind"), 64),
            "model": _text(a.get("model"), 100),
            "provider": _provider(a.get("model")),
            "account": UNKNOWN,
            "host": _text(a.get("host"), 100),
            "coordinated": UNKNOWN,
            "status": _text(a.get("status"), 32),
            "repo": _text(a.get("repo"), 100),
            "bead": a.get("bead") if isinstance(a.get("bead"), str) else None,
            "subagents_active": a.get("subagents_active") if isinstance(a.get("subagents_active"), int) else None,
            "last_activity": a.get("last_activity") if isinstance(a.get("last_activity"), str) else None,
            # Token cost priced from a local table. Not billing, not spend,
            # not remaining quota.
            "cost_estimate_today_usd": cost if isinstance(cost, int | float) and not isinstance(cost, bool) else None,
            "cost_basis": "estimate",
        })
    return out


# ---------------- assignments ----------------

def _bead_people(labels) -> dict:
    out: dict = {"coder": None, "reviewers": [], "approvers": [], "reviewed_refs": []}
    for label in labels if isinstance(labels, list) else []:
        name, sep, value = str(label).partition(":")
        field = _BEAD_LABEL_FIELDS.get(name.strip().lower())
        value = value.strip()[:200]
        if not field or not sep or not value:
            continue
        if field == "coder":
            out["coder"] = out["coder"] or value
        else:
            for part in (v.strip() for v in value.split(",")):
                if part and part not in out[field]:
                    out[field].append(part)
    return out


def _needs_review(labels) -> bool:
    """A review label that is not one of the people labels above."""
    return any("review" in str(lb).lower() and str(lb).partition(":")[0].strip().lower() not in _BEAD_LABEL_FIELDS
               for lb in labels if isinstance(lb, str)) if isinstance(labels, list) else False


def assignments(beads, agents) -> list[dict]:
    """In-progress beads with the people the bead names. The reviewer is only
    ever read from a `reviewer:<name>` label. It is never taken from the
    assignee. `named` means the labels name a reviewer and an approver, not
    that the review happened."""
    by_bead: dict[str, list[str]] = {}
    for a in agents if isinstance(agents, list) else []:
        if isinstance(a, dict) and isinstance(a.get("bead"), str) and a.get("id"):
            by_bead.setdefault(a["bead"], []).append(str(a["id"]))
    out = []
    for b in beads if isinstance(beads, list) else []:
        if not isinstance(b, dict) or b.get("status") != "in_progress":
            continue
        labels = b.get("labels")
        people = _bead_people(labels)
        assignee = b.get("assignee") if isinstance(b.get("assignee"), str) and b.get("assignee") else None
        coder = people["coder"] or assignee
        needs = _needs_review(labels)
        # Both names count as the coder: the `coder:` label and the assignee.
        coders = {n.lower() for n in (people["coder"], assignee) if n}
        missing: list[str] = []
        if needs:
            if not coders:
                missing.append("coder_unknown")
            if not people["reviewers"]:
                missing.append("reviewer")
            elif any(r.lower() in coders for r in people["reviewers"]):
                missing.append("independent_reviewer")
            if not people["approvers"]:
                missing.append("approver")
            elif any(r.lower() in coders for r in people["approvers"]):
                missing.append("independent_approver")
        gate = "no_review_marked" if not needs else "missing" if missing else "named"
        out.append({
            "id": _text(b.get("id")), "title": _text(b.get("title")),
            "assignee": assignee or UNKNOWN,
            "coder": coder or UNKNOWN,
            "coder_basis": "label" if people["coder"] else "assignee" if assignee else "none",
            "reviewers": people["reviewers"], "approvers": people["approvers"],
            "reviewed_refs": people["reviewed_refs"],
            "needs_review": needs, "review_gate": gate, "gate_missing": missing,
            "sessions": by_bead.get(b.get("id"), []),
        })
    out.sort(key=lambda r: (r["review_gate"] != "missing", r["id"]))
    return out


# ---------------- availability ----------------

def _row(provider: str, basis: str, observed: datetime | None, ttl_s: int, now: datetime,
         state: str, values: dict | None, detail=None) -> dict:
    """One reading. Past expires_at the state becomes "stale" and the numbers
    are dropped: an old number would read as a current one."""
    expires = observed + timedelta(seconds=ttl_s) if observed else None
    expired = bool(expires and now >= expires)
    if expired:
        state, values = "stale", None
    return {
        "provider": provider, "basis": basis, "state": state, "values": values,
        "observed_at": _iso(observed) if observed else None,
        "expires_at": _iso(expires) if expires else None,
        "expired": expired, "detail": detail,
    }


def _quota_row(entry: dict, ttl_s: int, now: datetime) -> dict:
    provider = _text(entry.get("provider"), 64)
    observed = _when(entry.get("checked_at"))
    if observed is not None and observed > now + timedelta(minutes=5):
        observed = None  # a reading from the future is not a reading
    if observed is None or entry.get("ok") is not True or entry.get("source") != "provider_api":
        # Never checked, check failed, or no provider endpoint exists: nothing
        # is known about availability. The failure text is kept as detail.
        detail = entry.get("error") if isinstance(entry.get("error"), str) else None
        return _row(provider, "unknown", observed, ttl_s, now, "unknown", None, detail)
    values = {k: entry.get(k) for k in ("remaining", "limit", "used", "unit", "period", "resets_at")}
    return _row(provider, "provider-observed", observed, ttl_s, now, "observed", values)


def _owner_rows(reports, ttl_s: int, now: datetime) -> list[dict]:
    """`modules.workflow.owner_reported`: [{provider, state, observed_at,
    note}] typed in by a person. Listed beside provider readings, not
    merged with them, and expired the same way."""
    out = []
    for r in (reports if isinstance(reports, list) else [])[:MAX_OWNER_REPORTS]:
        if not isinstance(r, dict) or not isinstance(r.get("provider"), str):
            continue
        observed = _when(r.get("observed_at"))
        if observed is None or observed > now + timedelta(minutes=5):
            out.append(_row(_text(r["provider"], 64), "unknown", None, ttl_s, now, "unknown", None,
                            "owner report has no readable observed_at"))
            continue
        out.append(_row(_text(r["provider"], 64), "owner-reported", observed, ttl_s, now,
                        _text(r.get("state"), 64), None, _text(r.get("note"), 200) if r.get("note") else None))
    return out


def availability(quota_response: dict, owner_reported, ttl_s: int, now: datetime) -> dict:
    rows = [_quota_row(e, ttl_s, now) for e in quota_response.get("providers") or [] if isinstance(e, dict)]
    rows += _owner_rows(owner_reported, ttl_s, now)
    return {"ttl_s": ttl_s, "rows": rows,
            "note": "Readings expire after ttl_s seconds. An expired reading is stale and its numbers are not shown."}


def build(snapshot: dict, quota_response: dict, cfg: dict, policy: dict, now: datetime) -> dict:
    """The /api/workflow body (without the envelope the endpoint adds)."""
    agents = snapshot.get("agents")
    beads = (snapshot.get("beads") or {}).get("items")
    sources = snapshot.get("sources") if isinstance(snapshot.get("sources"), dict) else {}
    health = {k: {"ok": bool((sources.get(k) or {}).get("ok")), "stale": bool((sources.get(k) or {}).get("stale", True))}
              for k in ("agents", "beads")}
    return {
        "policy": policy,
        "observed": {"label": "observed runtime", "snapshot_at": snapshot.get("generated_at"),
                     "sources": health, "sessions": observed_sessions(agents),
                     "cost_note": "cost_estimate_today_usd is an estimate from local token counts. It is not spend and not remaining quota."},
        "assignments": {"label": "assignments", "items": assignments(beads, agents)},
        "availability": availability(quota_response, cfg.get("owner_reported"), availability_ttl_s(cfg), now),
    }
