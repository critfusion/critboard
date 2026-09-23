"""bead reply: lets the dashboard's bead popup add a comment to a
human-labelled bead and either SEND BACK (wake an agent) or CLOSE it.

Off by default -- config/sources.json's "bead_reply" block, "enabled":
false (see config/sources.example.json's _readme for the full shape and
DEFAULT_BEAD_REPLY below for the fallback). While disabled, GET
/api/bead/{id}/comments and POST /api/bead/{id}/reply both refuse with a
403 and never run a `bd` command -- see main.py. Every write additionally
requires config/sources.json's "allow_config_writes" (same check, same
reasoning, as POST /api/config/layout|theme: a dashboard bound beyond
127.0.0.1 must be able to be made fully read-only).

How SEND BACK routes a bead: the fleet's cron dispatcher wakes an agent for
a bead that is `open`, has NO assignee, and carries a route label like
"needs-claude". A human-labelled bead carries no route label, so it's
invisible to that dispatcher -- SEND BACK's job is to make one visible
again: add the reply as a comment, remove every configured human label
present on the bead, add a route label, and make sure the bead is open and
unassigned. The route label is chosen by matching the bead's `created_by`
(who filed it) against `routes`, an ordered list of [substring, label]
pairs -- first case-insensitive substring match wins; no match falls back
to `default_route`. This is generic on purpose (this is a public repo) --
a fork configures its own routes/default_route for its own fleet's actor
naming.

Security: reply text, bead ids, and labels are all either arbitrary user
input or values read back from a database this dashboard doesn't fully
control (a bead's own labels/created_by). None of it is ever interpolated
into a shell string -- every `bd` call here goes through
collectors.beads.bd_exec_argv/bd_exec_env/run_bd_exec
(asyncio.create_subprocess_exec with an argv list, env vars via `env=`).
Bead ids and every label are additionally validated against the same
strict allow-list pattern used elsewhere in this codebase
(validate_bead_id/validate_label) before they ever reach an argv, and
before acting, the bead is always re-fetched fresh via `bd show` (never
trusted from the cached snapshot) and the write is refused unless it is
still `open` and still carries at least one human label -- this stops a
stale popup from reopening or relabelling a bead an agent already took.
"""

from __future__ import annotations

from .collectors.beads import (
    _parse_json_loose,
    bd_exec_argv,
    bd_exec_env,
    run_bd_exec,
)

# Shipped default -- overridden by config/sources.json's "bead_reply" block
# (see config/sources.example.json's _readme). Off by default; a fork or
# fresh install must opt in explicitly, same reasoning as allow_self_update.
DEFAULT_BEAD_REPLY: dict = {
    "enabled": False,
    "actor": "",
    "routes": [
        ["claude", "needs-claude"],
        ["codex", "needs-codex"],
        ["grok", "needs-grok"],
        ["cursor", "needs-cursor"],
        ["kimi", "needs-kimi"],
        ["opencode", "needs-opencode"],
    ],
    "default_route": "needs-claude",
}

MAX_TEXT_LEN = 8000
DEFAULT_CLOSE_REASON = "Closed via CritBoard bead reply."


class BeadReplyError(Exception):
    """Raised for every refusal/failure on the bead-reply write path.
    `reason` is a stable machine-readable code (main.py maps it into the
    JSON error body); `status_code` is the HTTP status main.py should use."""

    def __init__(self, reason: str, message: str, status_code: int = 400):
        self.reason = reason
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def resolve_config(sources: dict) -> dict:
    """Merge config/sources.json's "bead_reply" block (if any) over
    DEFAULT_BEAD_REPLY, and normalize "routes" into a list of (substr,
    label) str tuples -- silently dropping any malformed entry (not a
    2-element [str, str] pair) rather than raising, since this runs on
    every request (see main.py's config.reload_sources() calls) and a typo
    in a hand-edited sources.json must not 500 every bead popup."""
    raw = sources.get("bead_reply")
    cfg = dict(DEFAULT_BEAD_REPLY)
    if isinstance(raw, dict):
        cfg.update(raw)

    routes: list[tuple[str, str]] = []
    for pair in cfg.get("routes") or []:
        if isinstance(pair, list | tuple) and len(pair) == 2:
            substr, label = pair
            if isinstance(substr, str) and isinstance(label, str) and substr and label:
                routes.append((substr, label))
    cfg["routes"] = routes
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["actor"] = str(cfg.get("actor") or "").strip()
    cfg["default_route"] = str(cfg.get("default_route") or "needs-claude").strip()
    return cfg


def resolve_actor(cfg: dict, human_labels: list[str]) -> str:
    """The BEADS_ACTOR a reply/send-back/close write runs as, for the audit
    trail: the configured "actor" if set, else the first human label, else
    the fixed fallback "critboard-human"."""
    actor = (cfg.get("actor") or "").strip()
    if actor:
        return actor
    for label in human_labels:
        if isinstance(label, str) and label.strip():
            return label.strip()
    return "critboard-human"


def choose_route(created_by: str, routes: list[tuple[str, str]], default_route: str) -> str:
    """First case-insensitive substring match of `created_by` against
    `routes` wins; no match falls back to `default_route`. Generic by
    design -- see module docstring."""
    low = (created_by or "").lower()
    for substr, label in routes:
        if substr.lower() in low:
            return label
    return default_route


async def fetch_bead_raw(
    bd_bin: str, beads_env: str, actor: str, beads_dir: str, bead_id: str, timeout: float = 20.0
) -> dict:
    """`bd show <id> --json`, via the safe exec path (see module docstring)
    -- always the fresh, current state, never the cached dashboard
    snapshot. Returns bd's raw per-issue dict (id, status, labels, assignee,
    created_by, ...). Raises BeadReplyError("bead_not_found", ..., 404) or
    ("bd_command_failed", ..., 502)."""
    argv = bd_exec_argv(beads_env, bd_bin, ["show", bead_id, "--json"])
    env = bd_exec_env(actor, beads_dir)
    try:
        returncode, stdout, stderr = await run_bd_exec(argv, env, timeout)
    except RuntimeError as exc:
        raise BeadReplyError("bd_command_failed", str(exc), 502) from exc

    try:
        parsed = _parse_json_loose(stdout)
    except ValueError as exc:
        raise BeadReplyError(
            "bd_command_failed", f"bad bd show output: {type(exc).__name__}: {exc}", 502
        ) from exc

    if isinstance(parsed, dict) and parsed.get("error"):
        raise BeadReplyError("bead_not_found", f"bead not found: {bead_id}", 404)
    if returncode != 0:
        raise BeadReplyError(
            "bd_command_failed", f"bd show exit {returncode}: {stderr.strip()[:500]}", 502
        )
    if not isinstance(parsed, list) or not parsed:
        raise BeadReplyError("bead_not_found", f"bead not found: {bead_id}", 404)
    return parsed[0]


async def fetch_bead_comments(
    bd_bin: str, beads_env: str, actor: str, beads_dir: str, bead_id: str, timeout: float = 20.0
) -> list[dict]:
    """`bd comments <id> --json` via the safe exec path."""
    argv = bd_exec_argv(beads_env, bd_bin, ["comments", bead_id, "--json"])
    env = bd_exec_env(actor, beads_dir)
    try:
        returncode, stdout, stderr = await run_bd_exec(argv, env, timeout)
    except RuntimeError as exc:
        raise BeadReplyError("bd_command_failed", str(exc), 502) from exc
    if returncode != 0:
        raise BeadReplyError(
            "bd_command_failed", f"bd comments exit {returncode}: {stderr.strip()[:500]}", 502
        )
    try:
        parsed = _parse_json_loose(stdout)
    except ValueError as exc:
        raise BeadReplyError(
            "bd_command_failed", f"bad bd comments output: {type(exc).__name__}: {exc}", 502
        ) from exc
    return parsed if isinstance(parsed, list) else []


async def do_send_back(
    bd_bin: str,
    beads_env: str,
    actor: str,
    beads_dir: str,
    bead_id: str,
    text: str,
    human_labels_present: list[str],
    route_label: str,
    timeout: float = 20.0,
) -> dict:
    """SEND BACK: add `text` as a comment, then in one `bd update` call
    remove every label in `human_labels_present`, add `route_label`, clear
    the assignee, and set status to open. `text` must already be a
    non-empty stripped string, and every label already validated -- callers
    (main.py) check both before this runs. Returns a summary dict for the
    HTTP response."""
    env = bd_exec_env(actor, beads_dir)

    # "--" ends option parsing: without it a reply starting with "-" is read
    # by bd as a flag, so "-f<path>" or "--file=<path>" would post that local
    # file's contents into the shared beads DB (verified against real bd).
    # No shell is involved (see bd_exec_argv) -- this is argument injection.
    argv_comment = bd_exec_argv(beads_env, bd_bin, ["comments", "add", bead_id, "--", text])
    rc, _out, err = await run_bd_exec(argv_comment, env, timeout)
    if rc != 0:
        raise BeadReplyError(
            "bd_command_failed", f"bd comments add exit {rc}: {err.strip()[:500]}", 502
        )

    update_args = ["update", bead_id]
    for label in human_labels_present:
        update_args += ["--remove-label", label]
    update_args += ["--add-label", route_label, "--assignee", "", "--status", "open", "--json"]
    argv_update = bd_exec_argv(beads_env, bd_bin, update_args)
    rc2, _out2, err2 = await run_bd_exec(argv_update, env, timeout)
    if rc2 != 0:
        raise BeadReplyError(
            "bd_command_failed", f"bd update exit {rc2}: {err2.strip()[:500]}", 502
        )

    return {
        "action": "send_back",
        "bead_id": bead_id,
        "route": route_label,
        "removed_labels": human_labels_present,
        "assignee_cleared": True,
        "status": "open",
    }


async def do_close(
    bd_bin: str,
    beads_env: str,
    actor: str,
    beads_dir: str,
    bead_id: str,
    text: str,
    timeout: float = 20.0,
) -> dict:
    """CLOSE: if `text` (already stripped) is non-empty, add it as a
    comment first; then `bd close` with `text` as the reason, or
    DEFAULT_CLOSE_REASON if `text` is empty."""
    env = bd_exec_env(actor, beads_dir)
    comment_added = False

    if text:
        argv_comment = bd_exec_argv(beads_env, bd_bin, ["comments", "add", bead_id, "--", text])
        rc, _out, err = await run_bd_exec(argv_comment, env, timeout)
        if rc != 0:
            raise BeadReplyError(
                "bd_command_failed", f"bd comments add exit {rc}: {err.strip()[:500]}", 502
            )
        comment_added = True

    reason = text or DEFAULT_CLOSE_REASON
    # One "--reason=<text>" token, never "-r <text>": the value is bound to
    # the flag, so reply text starting with "-" cannot become a flag itself.
    argv_close = bd_exec_argv(beads_env, bd_bin, ["close", bead_id, f"--reason={reason}", "--json"])
    rc2, _out2, err2 = await run_bd_exec(argv_close, env, timeout)
    if rc2 != 0:
        raise BeadReplyError(
            "bd_command_failed", f"bd close exit {rc2}: {err2.strip()[:500]}", 502
        )

    return {
        "action": "close",
        "bead_id": bead_id,
        "comment_added": comment_added,
        "reason": reason,
        "status": "closed",
    }
