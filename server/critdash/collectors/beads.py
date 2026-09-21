"""beads collector: bd list/stats/ready --json via subprocess.

Verified on this host (2026-09-18):
  bd list --json --all --limit 0   -> JSON array of full issue objects
  bd stats --json  (alias: bd status --json) -> {"schema_version":1,"summary":{...}}
  bd ready --json  -> JSON array of ready issue objects (blocker-aware, excludes
                      in_progress/blocked/deferred/hooked)
`bd` needs `~/.config/beads/env` sourced first and BEADS_ACTOR set, per SPEC.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from datetime import UTC, datetime

from . import BaseCollector, now_iso

# Labels that describe agent routing intent, not a repo. Used to skip them
# when guessing a bead's repo from its labels.
_INTENT_LABEL_RE = (
    "needs-",
    "grok-review",
    "waiting-review",
    "android-test",
    "web-test",
    "owner",
    "tech-debt",
    "tech-",
)


def guess_repo(item: dict) -> str | None:
    for label in item.get("labels") or []:
        low = label.lower()
        if any(low == p or low.startswith(p) for p in _INTENT_LABEL_RE):
            continue
        return label
    title = item.get("title") or ""
    if ":" in title:
        prefix = title.split(":", 1)[0].strip()
        # slug-ish: short, hyphenated/lowercase token, not a whole sentence
        if 2 <= len(prefix) <= 40 and " " not in prefix and any(c.isalpha() for c in prefix):
            return prefix
    return None


def is_review_lane(item: dict) -> bool:
    for label in item.get("labels") or []:
        if "review" in label.lower():
            return True
    return False


def _age_s(created_at: str | None) -> int | None:
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((datetime.now(UTC) - created).total_seconds())


def build_dependency_maps(items: list[dict]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    blocked_by: dict[str, list[str]] = {}
    blocks: dict[str, list[str]] = {}
    for item in items:
        for dep in item.get("dependencies") or []:
            if dep.get("type") == "parent-child":
                continue
            issue_id = dep.get("issue_id")
            depends_on = dep.get("depends_on_id")
            if not issue_id or not depends_on:
                continue
            blocked_by.setdefault(issue_id, []).append(depends_on)
            blocks.setdefault(depends_on, []).append(issue_id)
    return blocked_by, blocks


def transform_item(item: dict, blocked_by: dict, blocks: dict) -> dict:
    bid = item.get("id")
    return {
        "id": bid,
        "title": item.get("title"),
        "status": item.get("status"),
        "priority": item.get("priority"),
        "type": item.get("issue_type"),
        "assignee": item.get("assignee"),
        "labels": item.get("labels") or [],
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "closed_at": item.get("closed_at"),
        "age_s": _age_s(item.get("created_at")),
        "blocked_by": blocked_by.get(bid, []),
        "blocks": blocks.get(bid, []),
        "parent": item.get("parent"),
        "repo": guess_repo(item),
        "url": None,
    }


# -- GET /api/bead/{id} -- on-demand detail (briefing Task 1) ----------------
#
# `bd show <id> --json --include-dependents` -- verified on this host
# 2026-09-18. Real shape has NO "design" field anywhere in the schema (checked
# `bd types`, `bd show --long --json`, and grepped all 281 issues in this
# workspace for a populated `design` key: zero hits) -- the frontend contract
# asks for one anyway, so it is always returned as null, a documented gap
# rather than an invented value. `notes` and `acceptance_criteria` ARE real
# fields. Dependencies embed the full related issue under `dependencies`
# (outbound, this issue depends on) and, with --include-dependents, `dependents`
# (inbound, other issues depend on this one) -- both lists also carry
# parent-child links tagged `dependency_type: "parent-child"`, which must be
# filtered out the same way build_dependency_maps() does for the list view, or
# every epic's children would show up as "blockers".

_BEAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def validate_bead_id(bead_id: str) -> bool:
    """Plausibility check before a bead id ever reaches a subprocess argv.
    Real bead ids look like `myproject-78jr` or `...-q733.1` (dotted child
    suffix) -- alnum plus `_.-`, must not start with `-` (which `bd` could
    otherwise parse as a flag)."""
    return bool(bead_id) and bool(_BEAD_ID_RE.match(bead_id))


class BeadNotFoundError(Exception):
    def __init__(self, bead_id: str):
        self.bead_id = bead_id
        super().__init__(f"bead not found: {bead_id}")


def _non_parent_child_ids(deps: list[dict] | None) -> list[str]:
    return [
        d["id"] for d in (deps or [])
        if d.get("dependency_type") != "parent-child" and d.get("id")
    ]


def transform_bead_detail(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "description": item.get("description"),
        "notes": item.get("notes"),
        # no such field in bd's schema -- see module note above.
        "design": None,
        "acceptance": item.get("acceptance_criteria"),
        "status": item.get("status"),
        "priority": item.get("priority"),
        "type": item.get("issue_type"),
        "assignee": item.get("assignee"),
        "labels": item.get("labels") or [],
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "closed_at": item.get("closed_at"),
        "parent": item.get("parent"),
        "blocked_by": _non_parent_child_ids(item.get("dependencies")),
        "blocks": _non_parent_child_ids(item.get("dependents")),
        "repo": guess_repo(item),
        "url": None,
    }


async def _run_capture(cmd: str, timeout: float = 20.0) -> tuple[int, str, str]:
    """Like _run(), but returns (returncode, stdout, stderr) instead of
    raising on a non-zero exit -- `bd show` on an unknown id exits 1 while
    still printing a well-formed {"error": ...} JSON body on stdout, which the
    caller needs to tell "not found" apart from a real failure."""
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"command timed out after {timeout}s: {cmd}") from None
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def fetch_bead_detail(
    beads_env: str, bd_bin: str, actor: str, bead_id: str, timeout: float = 20.0
) -> dict:
    """Run `bd show <id> --json --include-dependents` and return the
    transformed detail dict. Raises BeadNotFoundError if bd reports no
    matching issue, ValueError if bead_id fails validate_bead_id(), or
    RuntimeError for any other failure (timeout, bad output, non-zero exit
    that isn't a clean "not found")."""
    if not validate_bead_id(bead_id):
        raise ValueError(f"invalid bead id: {bead_id!r}")

    prefix = f". {shlex.quote(beads_env)} 2>/dev/null; export BEADS_ACTOR={shlex.quote(actor)};"
    cmd = (
        f"{prefix} {shlex.quote(bd_bin)} show {shlex.quote(bead_id)} "
        "--json --include-dependents"
    )
    returncode, stdout, stderr = await _run_capture(cmd, timeout)

    try:
        parsed = _parse_json_loose(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bad bd show output: {type(exc).__name__}: {exc}") from exc

    if isinstance(parsed, dict) and parsed.get("error"):
        raise BeadNotFoundError(bead_id)
    if returncode != 0:
        raise RuntimeError(f"bd show exit {returncode}: {stderr.strip()[:500]}")
    if not isinstance(parsed, list) or not parsed:
        raise BeadNotFoundError(bead_id)

    return transform_bead_detail(parsed[0])


def _parse_json_loose(text: str):
    """Parse the first complete JSON value in `text`, ignoring any trailing
    plain-text bd prints after it (e.g. a pagination notice on stdout)."""
    text = text.strip()
    if not text:
        return []
    return json.JSONDecoder().raw_decode(text)[0]


async def _run(cmd: str, timeout: float = 20.0) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"command timed out after {timeout}s: {cmd}") from None
    if proc.returncode != 0:
        raise RuntimeError(f"exit {proc.returncode}: {stderr.decode(errors='replace')[:500]}")
    return stdout.decode(errors="replace")


class BeadsCollector(BaseCollector):
    name = "beads"
    interval_s = 30.0

    def __init__(self, ctx=None, beads_env: str = "~/.config/beads/env", bd_bin: str = "bd",
                 actor: str = "critdash", store=None):
        super().__init__(ctx)
        self.beads_env = beads_env
        self.bd_bin = bd_bin
        self.actor = actor
        self.store = store
        self._prev: dict[str, tuple[str, str | None]] = {}

    def _prefix(self) -> str:
        env_path = shlex.quote(self.beads_env)
        return f". {env_path} 2>/dev/null; export BEADS_ACTOR={shlex.quote(self.actor)};"

    async def collect(self) -> dict:
        bd = shlex.quote(self.bd_bin)
        list_out, stats_out, ready_out = await asyncio.gather(
            _run(f"{self._prefix()} {bd} list --json --all --limit 0"),
            _run(f"{self._prefix()} {bd} stats --json"),
            # --limit 0 = unlimited. Without it, bd ready --json truncates at 100
            # and appends a plain-text pagination notice ("Showing 100 of N ready
            # issues...") onto stdout AFTER the JSON array, which breaks json.loads.
            _run(f"{self._prefix()} {bd} ready --json --limit 0"),
        )
        items_raw = _parse_json_loose(list_out)
        stats_raw = _parse_json_loose(stats_out) or {}
        ready_raw = _parse_json_loose(ready_out)

        blocked_by, blocks = build_dependency_maps(items_raw)
        items = [transform_item(i, blocked_by, blocks) for i in items_raw]

        summary = stats_raw.get("summary", {})
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        closed_today = sum(
            1 for i in items if i["status"] == "closed" and (i.get("closed_at") or "").startswith(today)
        )
        stats = {
            "open": summary.get("open_issues", 0),
            "in_progress": summary.get("in_progress_issues", 0),
            "blocked": summary.get("blocked_issues", 0),
            "closed_today": closed_today,
            "ready": summary.get("ready_issues", len(ready_raw)),
        }

        ready_ids = [i.get("id") for i in ready_raw if i.get("id")]
        lanes = {
            "ready": ready_ids,
            "in_progress": [i["id"] for i in items if i["status"] == "in_progress"],
            "blocked": [i["id"] for i in items if i["status"] == "blocked"],
            "review": [i["id"] for i in items if is_review_lane(i) and i["status"] != "closed"],
        }

        self._detect_transitions(items)

        return {"beads": {"stats": stats, "items": items, "lanes": lanes}}

    def _detect_transitions(self, items: list[dict]) -> None:
        current: dict[str, tuple[str, str | None]] = {}
        ts = now_iso()
        for item in items:
            bid = item["id"]
            status = item["status"]
            assignee = item["assignee"]
            current[bid] = (status, assignee)
            prev = self._prev.get(bid)
            if self.store is None:
                continue
            if prev is None:
                self.store.add_event(ts, "bead_created", "info", f"{bid} created: {item['title']}", bid)
                self.store.add_bead_status(ts, bid, status)
                continue
            prev_status, prev_assignee = prev
            claimed = prev_assignee is None and assignee is not None and assignee != prev_assignee
            if claimed:
                self.store.add_event(ts, "bead_claimed", "info", f"{bid} claimed by {assignee}", bid)
            if status != prev_status:
                if status == "closed":
                    self.store.add_event(ts, "bead_closed", "info", f"{bid} closed", bid)
                elif not claimed:
                    self.store.add_event(
                        ts, "bead_status", "info", f"{bid} {prev_status} -> {status}", bid
                    )
                self.store.add_bead_status(ts, bid, status)
        self._prev = current
