"""Infrastructure module: turn BookStack pages into infrastructure records.

Pure functions, no I/O and no clock of their own (the caller passes `now`).
The wiki page is the source of truth; CritBoard keeps no copy.

A page carries a leading "Provenance" Markdown table (`| Field | Value |`)
and BookStack tags. See SPEC.md, "Infrastructure record contract".

Rule: a field that is missing, empty or unreadable is the explicit value
"unknown". Nothing here fills a gap with a guess, and a record is never
reported as `current` unless a readable Live check date backs it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

UNKNOWN = "unknown"
STATUSES = ("current", "needs-verification", "contradicted", "stale", "unknown")
DEFAULT_STALE_AFTER_DAYS = 90
_EMPTY_VALUES = {"", "-", "--", "n/a", "na", "none", "tbd", "?", UNKNOWN}

_TABLE_FIELDS = {
    "source repo": "source_repo",
    "source path": "source_path",
    "source git sha": "source_git_sha",
    "live check": "live_check",
    "class": "class",
    "status": "status",
}
MAX_TEXT_CHARS = 500
MAX_TAGS = 50
# Worse first. When the table and a tag disagree, the earlier one wins.
_STATUS_WORSE_FIRST = ("contradicted", "stale", "needs-verification", "unknown", "current")
_TAG_SCALARS = ("kind", "owner", "environment", "purpose", "status", "class")
_TAG_LISTS = {"runbook": "runbooks", "backup": "backups", "related": "related"}


def stale_after_days(cfg: dict) -> int:
    v = cfg.get("stale_after_days", DEFAULT_STALE_AFTER_DAYS)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        return DEFAULT_STALE_AFTER_DAYS
    return min(v, 3650)


def book_slug(cfg: dict) -> str:
    v = cfg.get("book_slug")
    return v.strip() if isinstance(v, str) and v.strip() else "infrastructure"


def _clean(value: str) -> str:
    """Cell/tag text -> value, or UNKNOWN when it says nothing."""
    text = (value or "").strip().strip("`*").strip()[:MAX_TEXT_CHARS]
    return UNKNOWN if text.lower() in _EMPTY_VALUES else text


# A status the page explicitly declares as `unknown` is a real value (worse than
# `current`); an empty, `tbd` or junk status is absent. `_clean` folds both to
# UNKNOWN, so the status fields carry this marker for the declared one.
DECLARED_UNKNOWN = "unknown (declared)"


def _status_text(value: str) -> str:
    """Status cell/tag text -> cleaned text; DECLARED_UNKNOWN for a literal unknown."""
    if re.sub(r"[\s_]+", "-", (value or "").strip().strip("`*").strip().lower()) == UNKNOWN:
        return DECLARED_UNKNOWN
    return _clean(value)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def parse_provenance_table(markdown: str) -> dict[str, str]:
    """First `| Field | Value |` table in the page. Returns the six known
    fields, each UNKNOWN when the table, the row or the value is absent."""
    out = {name: UNKNOWN for name in _TABLE_FIELDS.values()}
    lines = (markdown or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|"):
            head = [c.lower() for c in _cells(line)]
            if head[:2] == ["field", "value"]:
                start = i
            break  # only the first table counts as the provenance table
    if start is None:
        return out
    for line in lines[start + 1:]:
        if not line.lstrip().startswith("|"):
            break
        cells = _cells(line)
        if len(cells) < 2 or re.fullmatch(r":?-{2,}:?", cells[0]):
            continue  # malformed row or the |---|---| separator
        key = _TABLE_FIELDS.get(re.sub(r"[`*_]", "", cells[0]).strip().lower())
        if key:
            out[key] = _status_text(cells[1]) if key == "status" else _clean(cells[1])
    return out


def parse_tags(tags) -> dict:
    """BookStack tags -> scalars (first non-empty wins) and repeatable lists."""
    out: dict = {name: UNKNOWN for name in _TAG_SCALARS}
    for name in _TAG_LISTS.values():
        out[name] = []
    out["statuses"] = []  # every readable `status` tag, in page order
    for tag in (tags if isinstance(tags, list) else [])[:MAX_TAGS]:
        if not isinstance(tag, dict):
            continue
        name = str(tag.get("name") or "").strip().lower()
        raw = str(tag.get("value") or "")
        value = _clean(raw)
        if name == "status" and _status_text(raw) == DECLARED_UNKNOWN:
            out["statuses"].append(DECLARED_UNKNOWN)
            continue
        if value == UNKNOWN:
            continue
        if name == "status":
            out["statuses"].append(value)
        if name in _TAG_SCALARS and out[name] == UNKNOWN:
            out[name] = value
        elif name in _TAG_LISTS and value not in out[_TAG_LISTS[name]]:
            out[_TAG_LISTS[name]].append(value)
    return out


def parse_when(text: str | None, now: datetime) -> datetime | None:
    """ISO date or datetime -> aware UTC, else None. Only the leading ISO
    date has to be readable; a time part is used when it parses. A date more
    than a day in the future is not a past check: None."""
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})(?![\d])", text or "")
    if not m:
        return None
    try:
        when = datetime.fromisoformat((text or "").strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            when = datetime.fromisoformat(m.group(1))
        except ValueError:
            return None
    try:
        when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    except OverflowError:  # 0001-01-01 or 9999-12-31 with an offset
        return None
    return None if when > now + timedelta(days=1) else when


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normal_status(raw: str) -> str:
    s = re.sub(r"[\s_]+", "-", raw.strip().lower())
    return s if s in STATUSES else UNKNOWN


def merge_status(table: str, *tags: str) -> tuple[str, str | None]:
    """Declared status from the table and every `status` tag (raw text).
    Unreadable ones are ignored; a declared `unknown` (DECLARED_UNKNOWN) is a
    real value, worse than `current`. One distinct readable value -> that value.
    Several that differ -> the worst, plus a visible conflict note."""
    found: list[tuple[str, str]] = []
    for source, raw in (("table", table), *(("tag", t) for t in tags)):
        value = UNKNOWN if raw == DECLARED_UNKNOWN else _normal_status(raw)
        if (value != UNKNOWN or raw == DECLARED_UNKNOWN) and (source, value) not in found:
            found.append((source, value))
    values = {v for _, v in found}
    if not values:
        return UNKNOWN, None
    if len(values) == 1:
        return found[0][1], None
    worst = min(values, key=_STATUS_WORSE_FIRST.index)
    return worst, "status conflict: " + ", ".join(f"{src} {val}" for src, val in found)


def compute_status(declared: str, live_check: str, updated_at: str | None,
                   now: datetime, stale_days: int) -> dict:
    """Decide what the record may claim.

    - `contradicted` is kept: someone found the page wrong.
    - Age counts from the Live check date; with no readable Live check, from
      the page's updated_at (the page was edited, nothing was checked).
    - Older than `stale_days` -> `stale`, whatever the table says.
    - `current` needs a readable Live check. Without one it drops to
      `needs-verification`: an edit date alone is not a check.
    """
    status = _normal_status(declared)
    checked = parse_when(live_check, now)
    edited = parse_when(updated_at, now)
    basis = "live_check" if checked else "page_updated" if edited else "none"
    when = checked or edited
    reason = None
    if status != "contradicted" and when is not None and now - when > timedelta(days=stale_days):
        status = "stale"
        reason = f"{'Live check' if checked else 'Page edit'} is older than {stale_days} days"
    elif status == "current" and not checked:
        status = "needs-verification"
        reason = "Marked current, but no readable Live check date"
    elif status == "unknown":
        reason = "No status recorded"
    return {
        "status": status,
        "status_reason": reason,
        "last_verified": _iso(checked) if checked else UNKNOWN,
        "verified_basis": basis,
        "age_from": _iso(when) if when else UNKNOWN,
    }


def _link(value: str, base_url: str) -> dict:
    """Tag value -> {text, href}. href only for http(s) URLs and for a
    BookStack path (`/books/...`) on the configured wiki; anything else is
    plain text, so a `javascript:` value can never become a link."""
    href = None
    if re.match(r"https?://[^\s]+$", value, re.I):
        href = value
    elif value.startswith("/") and not value.startswith("//") and re.match(r"/[\w./%~-]+$", value):
        href = base_url + value
    return {"text": value, "href": href}


def parse_record(page: dict, *, base_url: str, book_slug: str, now: datetime,
                 stale_days: int) -> dict:
    """One BookStack page (GET /api/pages/{id} body) -> one record."""
    prov = parse_provenance_table(str(page.get("markdown") or ""))
    tags = parse_tags(page.get("tags"))
    updated_at = page["updated_at"][:MAX_TEXT_CHARS] if isinstance(page.get("updated_at"), str) else None
    declared, conflict = merge_status(prov["status"], *tags["statuses"])
    state = compute_status(declared, prov["live_check"], updated_at, now, stale_days)
    state["status_conflict"] = conflict
    klass = prov["class"] if prov["class"] != UNKNOWN else tags["class"]
    slug = str(page["slug"])[:MAX_TEXT_CHARS] if page.get("slug") else None
    url = (f"{base_url}/books/{quote(book_slug, safe='')}/page/{quote(slug, safe='')}"
           if slug else None)
    updated_by = page.get("updated_by")
    repo = prov["source_repo"]
    return {
        "id": page.get("id"),
        "title": str(page.get("name") or "")[:MAX_TEXT_CHARS] or UNKNOWN,
        "url": url,
        "kind": tags["kind"],
        "owner": tags["owner"],
        "environment": tags["environment"],
        "purpose": tags["purpose"],
        "runbooks": [_link(v, base_url) for v in tags["runbooks"]],
        "backups": [_link(v, base_url) for v in tags["backups"]],
        "related": tags["related"],
        "source": {
            "repo": repo,
            "repo_href": repo if re.match(r"https?://[^\s]+$", repo, re.I) else None,
            "path": prov["source_path"],
            "git_sha": prov["source_git_sha"],
            "class": klass,
            "live_check": prov["live_check"],
            "declared_status": declared,
        },
        **state,
        "updated_at": updated_at or UNKNOWN,
        "updated_by": str(updated_by["name"])[:MAX_TEXT_CHARS] if isinstance(updated_by, dict) and updated_by.get("name") else UNKNOWN,
    }


def degraded_record(page_id, name) -> dict:
    """Stand-in for a page that could not be parsed: nothing is claimed."""
    return {
        "id": page_id if isinstance(page_id, int) and not isinstance(page_id, bool) else None,
        "title": str(name or "")[:MAX_TEXT_CHARS] or UNKNOWN,
        "url": None, "kind": UNKNOWN, "owner": UNKNOWN, "environment": UNKNOWN, "purpose": UNKNOWN,
        "runbooks": [], "backups": [], "related": [],
        "source": {"repo": UNKNOWN, "repo_href": None, "path": UNKNOWN, "git_sha": UNKNOWN,
                   "class": UNKNOWN, "live_check": UNKNOWN, "declared_status": UNKNOWN},
        "status": "unknown", "status_reason": "unreadable record", "status_conflict": None,
        "last_verified": UNKNOWN, "verified_basis": "none", "age_from": UNKNOWN,
        "updated_at": UNKNOWN, "updated_by": UNKNOWN,
    }
