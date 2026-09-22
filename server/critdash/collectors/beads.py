"""beads collector: bd list/stats/ready --json via subprocess.

Verified on this host (2026-09-18):
  bd list --json --all --limit 0   -> JSON array of full issue objects
  bd stats --json  (alias: bd status --json) -> {"schema_version":1,"summary":{...}}
  bd ready --json  -> JSON array of ready issue objects (blocker-aware, excludes
                      in_progress/blocked/deferred/hooked)

`~/.config/beads/env` is a SITE-SPECIFIC CONVENTION (the original fleet's
own way of sourcing credentials for a shared beads server) -- not something
`bd` itself requires. A generic install with `bd` on PATH and a local
workspace (`bd init`, or `BEADS_DIR` pointed at one) needs no such file.
Verified live with an isolated HOME and no env file (2026-09-21):
  no workspace:  `bd list --json --all --limit 0` -> exit 1, stderr
                 "Error: no beads database found\nHint: run 'bd where' to
                 inspect the resolved workspace, or 'bd init' to create a
                 new database\n      or set BEADS_DIR to point to your
                 .beads directory"
  after `bd init`: same command -> exit 0, stdout "[]" -- works fine.
So the env file is optional, sourced only when it exists; `bd`'s own
no-workspace failure is a real, informative error, not a missing
dependency, and is left to the normal command_failed classification below
(see `_run`) rather than special-cased.

Beads is still an OPTIONAL dependency overall: a host with no beads
workflow at all has no `bd` binary, and that is a normal, expected state on
a fresh install -- not a failure. Availability is judged on exactly one
thing, checked directly in Python before any subprocess ever runs:
  1. the `bd` binary resolves (PATH, or an explicit path).
If that holds, the collector is scheduled; whatever `bd` itself then does
(no workspace configured, network unreachable, ...) surfaces through the
existing command_failed/unreachable classification with bd's own message,
which is already actionable (it suggests `bd init` or `BEADS_DIR`).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
from datetime import UTC, datetime
from pathlib import Path

from . import BaseCollector, CollectorIssue, now_iso

_BD_DEPENDENCY_REMEDY = "Install the bd CLI, or ignore this panel if you do not use beads."


def resolve_bd_bin(bd_bin: str) -> str | None:
    """Resolve bd_bin to an existing, executable path, or None if it can't
    be found. A bare name (no path separator, e.g. "bd") is looked up on
    PATH via shutil.which; anything containing a "/" (bd_bin's own default
    is the full path "~/.local/bin/bd") is checked directly after expanding
    "~" -- deliberately NOT dependent on the beads env file having been
    sourced yet (see module docstring), since that file's job is DB/actor
    config, not extending PATH for this specific binary."""
    if "/" in bd_bin:
        p = Path(bd_bin).expanduser()
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    return shutil.which(bd_bin)


def bd_shell_prefix(beads_env: str, actor: str, beads_dir: str = "") -> str:
    """Shell prefix for a `bd` invocation: sources `beads_env` only when
    that file actually exists -- it is an optional, site-specific
    convention (see module docstring), never something `bd` requires -- then
    exports BEADS_ACTOR. A generic `bd init`/BEADS_DIR workspace with no env
    file at all runs `bd` directly, with no `.` (dot) of a nonexistent path
    ever reaching the shell. Shared by BeadsCollector._prefix() and
    fetch_bead_detail() so the two call sites can't drift.

    `beads_dir` (config key of the same name), when set, is exported as
    BEADS_DIR *after* beads_env is sourced -- so it overrides whatever
    BEADS_DIR beads_env's own script may have exported. This is the fix for
    a `bd` that works fine from a user's own shell but reports "no
    workspace configured" here: the collector runs `bd` from CritBoard's
    own working directory, not the user's, and doesn't inherit their shell
    env at all. Precedence, high to low: beads_dir -> whatever beads_env
    provides -> bd's own resolution (nothing exported here)."""
    parts = []
    env_path = Path(os.path.expanduser(beads_env)) if beads_env else None
    if env_path is not None and env_path.is_file():
        parts.append(f". {shlex.quote(str(env_path))} 2>/dev/null;")
    parts.append(f"export BEADS_ACTOR={shlex.quote(actor)};")
    if beads_dir:
        parts.append(f"export BEADS_DIR={shlex.quote(os.path.expanduser(beads_dir))};")
    return " ".join(parts)

# Labels that describe agent routing intent, not a repo. Used to skip them
# when guessing a bead's repo from its labels.
_INTENT_LABEL_RE = (
    "needs-",
    "grok-review",
    "waiting-review",
    "device-check",
    "browser-check",
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
    beads_env: str, bd_bin: str, actor: str, bead_id: str, timeout: float = 20.0, beads_dir: str = ""
) -> dict:
    """Run `bd show <id> --json --include-dependents` and return the
    transformed detail dict. Raises BeadNotFoundError if bd reports no
    matching issue, ValueError if bead_id fails validate_bead_id(), or
    RuntimeError for any other failure (timeout, bad output, non-zero exit
    that isn't a clean "not found")."""
    if not validate_bead_id(bead_id):
        raise ValueError(f"invalid bead id: {bead_id!r}")

    prefix = bd_shell_prefix(beads_env, actor, beads_dir)
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
    """Run cmd via BeadsCollector.collect() and return stdout, or raise a
    CollectorIssue -- never a bare RuntimeError -- describing what actually
    went wrong. By the time this runs, the caller has already confirmed the
    `bd` binary and the env file both exist (see collect()), so a failure
    here means the command ran and genuinely failed: a real command_failed
    or, for a timeout, a network-shaped unreachable (the beads env file
    points `bd` at a server; a hang is the closest signal available that
    it isn't responding). `optional=False` on both -- the user HAS bd
    configured, so this is a real problem worth a warning, not an
    unconfigured-dependency notice."""
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise CollectorIssue(
            "unreachable",
            f"bd command timed out after {timeout}s",
            remedy="Check network connectivity to the beads server named in your beads env file.",
        ) from None
    if proc.returncode != 0:
        out_text = stdout.decode(errors="replace").strip()
        err_text = stderr.decode(errors="replace").strip()
        # A tool that prints its error to stdout instead of stderr must not
        # produce a blank reason -- fall back to stdout when stderr is empty.
        detail = err_text[:500] or out_text[:500] or f"(no output on exit {proc.returncode})"
        raise CollectorIssue(
            "command_failed",
            f"bd exited {proc.returncode}: {detail}",
            remedy="Run the bd command by hand (see beads env file) to see the full error.",
        )
    return stdout.decode(errors="replace")


_BEADS_DIR_REMEDY = (
    "Set beads_dir to the .beads directory ITSELF (e.g. /path/to/project/.beads), not its parent -- "
    "that's the easy mistake. Run `bd where --json` inside your beads workspace to see the right "
    "value (its \"path\" field)."
)


def validate_beads_dir(beads_dir: str) -> CollectorIssue | None:
    """None if `beads_dir` is unset (fine -- it's optional, see module
    docstring) or points at an existing directory. A configured-but-wrong
    value is a real misconfiguration (the user set it, just to the wrong
    path -- most often the workspace's parent instead of the .beads
    directory itself), so this is reported as `optional=False`: worth a
    warning, not a quiet "not configured" notice."""
    if not beads_dir:
        return None
    if not Path(os.path.expanduser(beads_dir)).is_dir():
        return CollectorIssue(
            "config_missing",
            f"beads_dir does not exist or is not a directory: {beads_dir!r}",
            remedy=_BEADS_DIR_REMEDY,
            optional=False,
        )
    return None


def availability_issue(bd_bin: str, beads_dir: str = "") -> CollectorIssue | None:
    """None if `bd` is installed and `beads_dir` (if set) is valid -- the
    preflight checks BeadsCollector.collect() runs before ever shelling
    out, factored out so main.py can run the exact same checks at startup
    (and on periodic re-detection) to decide whether to schedule this
    collector at all, without duplicating the logic or actually running
    collect().

    The beads env file is deliberately NOT checked here (see module
    docstring): it is optional, and a `bd` with no workspace configured
    fails with its own clear, actionable error once collect() actually runs
    it -- that surfaces through the normal command_failed classification,
    not through this availability gate. `beads_dir` IS checked here (see
    validate_beads_dir) since a bad value is a configuration mistake worth
    surfacing immediately, not a "bd itself will explain it" case."""
    if resolve_bd_bin(bd_bin) is None:
        return CollectorIssue(
            "dependency_missing",
            f"bd is not installed: {bd_bin!r} was not found on PATH",
            remedy=_BD_DEPENDENCY_REMEDY,
            optional=True,
        )
    return validate_beads_dir(beads_dir)


class BeadsCollector(BaseCollector):
    name = "beads"
    interval_s = 30.0

    def __init__(self, ctx=None, beads_env: str = "~/.config/beads/env", bd_bin: str = "bd",
                 actor: str = "critdash", store=None, beads_dir: str = ""):
        super().__init__(ctx)
        self.beads_env = beads_env
        self.bd_bin = bd_bin
        self.actor = actor
        self.store = store
        self.beads_dir = beads_dir
        self._prev: dict[str, tuple[str, str | None]] = {}

    def _prefix(self) -> str:
        return bd_shell_prefix(self.beads_env, self.actor, self.beads_dir)

    async def collect(self) -> dict:
        issue = availability_issue(self.bd_bin, self.beads_dir)
        if issue is not None:
            raise issue

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
