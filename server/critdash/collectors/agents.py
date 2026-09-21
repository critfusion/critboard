"""agents collector: `herdr agent list` (one line of JSON on stdout).

Verified shape on this host (2026-09-18):
  {"id":"cli:agent:list","result":{"agents":[{...}],"type":"agent_list"}}
Each agent: agent, agent_status (idle|working|done|...), cwd, foreground_cwd,
pane_id, tab_id, workspace_id, terminal_title/terminal_title_stripped, focused,
agent_session.value (the Claude sessionId).

`bead` (claimed bead resolvable from the agent) is left null: nothing in
`herdr agent list` ties an agent_session to a bd actor identity, so any
match would be a guess. Documented in the final report as a known gap.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
from datetime import UTC, datetime

from . import BaseCollector, now_iso

_STATUS_MAP = {
    "idle": "idle",
    "working": "working",
    "done": "done",
}

# Path prefixes collapsed to short aliases for compact display. Only the
# actual home directory is known generically at runtime (os.path.expanduser);
# an install can extend this via short_cwd's docstring pattern if it wants a
# second alias for its own repo_roots convention.
_SHORT_ROOTS = ((os.path.expanduser("~"), "~"),)

# A session whose jsonl was written more recently than this is "working";
# older (but still inside session_active_window_s) is "idle". Not
# configurable -- session_active_window_s (how far back a session counts as
# "active" at all) is the knob; this is just the working/idle split within
# that window. Never invents a "done" state from file mtime -- see
# scan_session_agents' docstring.
SESSION_WORKING_THRESHOLD_S = 120.0

# How much of a jsonl's tail to read to find its last record. Session log
# lines can be large (thinking blocks, tool output) but the fields we need
# (sessionId/cwd/gitBranch/message.model) are near the end of the last one or
# two lines, so this is generous headroom, not a full-file read.
_TAIL_READ_BYTES = 65536


def _read_last_session_record(path: str, tail_bytes: int = _TAIL_READ_BYTES) -> dict | None:
    """Read only the tail of a jsonl file and pick the freshest value for
    each field the caller needs, independently. A session's most recent
    lines are often housekeeping events (e.g. "system", "queue-operation")
    that carry the sessionId but an explicit null cwd/gitBranch, and no
    message.model at all -- so scanning backward and taking, for each field
    separately, the first line where it is actually non-null picks up the
    real values from a few lines earlier in the same tail window, instead of
    under-reporting them as unknown. Never re-parses the whole file -- see
    scan_session_agents' docstring on why this must stay cheap. Returns None
    if no line in the tail carries a sessionId at all."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            chunk = f.read()
    except OSError:
        return None
    session_id = cwd = git_branch = model = None
    for raw_line in reversed(chunk.split(b"\n")):
        line = raw_line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        if session_id is None and doc.get("sessionId"):
            session_id = doc["sessionId"]
        if cwd is None and doc.get("cwd"):
            cwd = doc["cwd"]
        if git_branch is None and doc.get("gitBranch"):
            git_branch = doc["gitBranch"]
        if model is None:
            m = (doc.get("message") or {}).get("model")
            if m and m != "<synthetic>":
                model = m
        if session_id is not None and cwd is not None and git_branch is not None and model is not None:
            break
    if session_id is None:
        return None
    return {"session_id": session_id, "cwd": cwd, "git_branch": git_branch, "model": model}


def scan_session_agents(
    projects_glob: str, window_s: float, now: datetime | None = None,
    working_threshold_s: float = SESSION_WORKING_THRESHOLD_S, tail_bytes: int = _TAIL_READ_BYTES,
) -> list[dict]:
    """Derive "active agent" entries straight from Claude Code session-log
    activity, independent of herdr -- the fix for Bug 2 (a session started
    outside herdr, e.g. plain tmux or a cron job, is otherwise invisible to
    the dashboard no matter what actually runs on the host).

    A jsonl whose mtime falls within `window_s` of `now` counts as an active
    session. Status is inferred purely from that recency: within
    `working_threshold_s` is "working", otherwise "idle" -- never a "done"
    state, since file mtime can't distinguish "finished" from "waiting on a
    long tool call". The rest of the record (cwd/gitBranch/model) comes from
    the tail of that same file, never a second full parse.
    """
    now = now or datetime.now(UTC)
    pattern = os.path.expanduser(projects_glob)
    out: list[dict] = []
    for path in glob.glob(pattern):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        mtime_dt = datetime.fromtimestamp(mtime, tz=UTC)
        age_s = (now - mtime_dt).total_seconds()
        if age_s < 0 or age_s > window_s:
            continue
        rec = _read_last_session_record(path, tail_bytes)
        if rec is None:
            continue
        out.append({
            "session_id": rec["session_id"],
            "cwd": rec["cwd"],
            "git_branch": rec["git_branch"],
            "model": rec["model"],
            "mtime": mtime_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "working" if age_s <= working_threshold_s else "idle",
        })
    return out


def _map_status(raw: str | None) -> str:
    if raw is None:
        return "unknown"
    return _STATUS_MAP.get(raw, "unknown")


def short_cwd(cwd: str | None) -> str | None:
    """cwd with the home dir collapsed to '~', for compact display.
    Longest-prefix match wins."""
    if not cwd:
        return cwd
    for prefix, alias in _SHORT_ROOTS:
        if cwd == prefix:
            return alias
        if cwd.startswith(prefix.rstrip("/") + "/"):
            return alias + cwd[len(prefix.rstrip("/")):]
    return cwd


def agent_label(*, repo: str | None, cwd: str | None, title: str | None, kind: str | None) -> str | None:
    """Pick the most useful display label for an agent card: the repo it's
    working in, else the basename of its cwd (skipped when cwd is just the
    home dir -- that basename isn't informative), else the raw terminal
    title, else the agent kind as a last resort."""
    if repo:
        return repo
    home = os.path.expanduser("~")
    if cwd and cwd.rstrip("/") != home.rstrip("/"):
        return os.path.basename(cwd.rstrip("/")) or cwd
    if title:
        return title
    return kind


def parse_herdr_output(text: str) -> list[dict]:
    line = text.strip().splitlines()[-1] if text.strip() else ""
    if not line:
        return []
    doc = json.loads(line)
    return doc.get("result", {}).get("agents", [])


def best_worktree_match(cwd: str, worktrees: list[dict], host: str | None = None) -> dict | None:
    """`host`, when given, restricts candidates to worktrees tagged with that
    host -- required once ctx.latest_worktrees can hold a merged fleet-wide
    list, so a local agent's cwd (e.g. "/home/user/repos/foo") never matches
    a same-looking path reported by a different host's worktree scan."""
    best = None
    best_len = -1
    for wt in worktrees:
        if host is not None and wt.get("host", "localhost") != host:
            continue
        path = wt.get("path") or ""
        if cwd == path or cwd.startswith(path.rstrip("/") + "/"):
            if len(path) > best_len:
                best = wt
                best_len = len(path)
    return best


def _usage_fields(session: str | None, usage_by_session: dict) -> dict:
    usage = usage_by_session.get(session, {}) if session else {}
    default_tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0}
    return {
        "tokens_today": usage.get("tokens_today", default_tokens),
        "cost_today_usd": usage.get("cost_today_usd", 0.0),
        "msg_count_today": usage.get("msg_count_today", 0),
        "subagents_active": usage.get("subagents_active", 0),
        "last_activity": usage.get("last_activity"),
        "last_model": usage.get("last_model"),
    }


def _build_herdr_agent(raw: dict, worktrees: list[dict], host: str, usage_by_session: dict) -> dict:
    session = (raw.get("agent_session") or {}).get("value")
    cwd = raw.get("cwd") or raw.get("foreground_cwd")
    status = _map_status(raw.get("agent_status"))
    wt = best_worktree_match(cwd, worktrees, host=host) if cwd else None
    repo = wt.get("repo") if wt else None
    title = raw.get("terminal_title_stripped") or raw.get("terminal_title")
    kind = raw.get("agent")
    usage = _usage_fields(session, usage_by_session)

    return {
        "id": session,
        "kind": kind,
        "status": status,
        "cwd": cwd,
        "cwd_short": short_cwd(cwd),
        "repo": repo,
        "branch": wt.get("branch") if wt else None,
        "pane": raw.get("pane_id"),
        "workspace": raw.get("workspace_id"),
        "title": title,
        "label": agent_label(repo=repo, cwd=cwd, title=title, kind=kind),
        "focused": bool(raw.get("focused")),
        "session_id": session,
        "bead": None,
        "last_activity": usage["last_activity"],
        "tokens_today": usage["tokens_today"],
        "cost_today_usd": usage["cost_today_usd"],
        "msg_count_today": usage["msg_count_today"],
        "subagents_active": usage["subagents_active"],
        "model": usage["last_model"],
        "host": host,
        "stale": False,
        "source": "herdr",
        # internal, stripped before appending to _status_since bookkeeping ID
        "_status_key": session or cwd,
        "_event_desc": f"{kind} {status} ({title or cwd})",
    }


def _build_session_agent(rec: dict, worktrees: list[dict], host: str, usage_by_session: dict) -> dict:
    """A session-derived agent entry has no herdr record at all: every field
    herdr alone can supply (pane/workspace/title/focused) is genuinely
    unknown, so it is null, per the briefing ("use null where genuinely
    unknown"). `kind` is the one exception: this record was built by scanning
    a Claude Code session log under claude_projects_dir, so the producer is
    known -- "claude" -- even without herdr. merge_agent_sources overwrites
    this with herdr's kind when herdr also reports the session, since herdr
    stays authoritative there (it may legitimately say something other than
    "claude")."""
    session = rec["session_id"]
    cwd = rec.get("cwd")
    wt = best_worktree_match(cwd, worktrees, host=host) if cwd else None
    repo = wt.get("repo") if wt else None
    branch = rec.get("git_branch") or (wt.get("branch") if wt else None)
    usage = _usage_fields(session, usage_by_session)

    return {
        "id": session,
        "kind": "claude",
        "status": rec["status"],
        "cwd": cwd,
        "cwd_short": short_cwd(cwd),
        "repo": repo,
        "branch": branch,
        "pane": None,
        "workspace": None,
        "title": None,
        "label": agent_label(repo=repo, cwd=cwd, title=None, kind="claude"),
        "focused": False,
        "session_id": session,
        "bead": None,
        "last_activity": usage["last_activity"] or rec["mtime"],
        "tokens_today": usage["tokens_today"],
        "cost_today_usd": usage["cost_today_usd"],
        "msg_count_today": usage["msg_count_today"],
        "subagents_active": usage["subagents_active"],
        "model": rec.get("model") or usage["last_model"],
        "host": host,
        "stale": False,
        "source": "session",
        "_status_key": session,
        "_event_desc": f"session {rec['status']} ({cwd})",
    }


def merge_agent_sources(herdr_built: list[dict], session_records: list[dict]) -> list[dict]:
    """Merge herdr-derived and session-derived agent entries by session id --
    a session known to both sources produces ONE entry (source="both"), with
    herdr's pane/workspace/title/status/focused/kind enriching the
    session-derived base record (cwd/branch/model come from the jsonl, the
    thing herdr can't see). herdr-only and session-only entries pass through
    unchanged with source="herdr"/"session" respectively."""
    session_by_id = {r["session_id"]: r for r in session_records if r.get("session_id")}
    consumed: set[str] = set()
    merged: list[dict] = []

    for h in herdr_built:
        sid = h.get("session_id")
        if sid and sid in session_by_id:
            base = dict(session_by_id[sid])
            base.update({
                "pane": h["pane"], "workspace": h["workspace"], "title": h["title"],
                "status": h["status"], "focused": h["focused"], "kind": h["kind"],
                "source": "both", "_status_key": h["_status_key"],
                "_event_desc": h["_event_desc"],
            })
            # recompute label now that herdr's title/kind are available -- a
            # session-only build always passes title=kind=None, so a merged
            # agent whose cwd happens to be the home dir would otherwise lose
            # herdr's title fallback (see agent_label's priority order).
            base["label"] = agent_label(
                repo=base["repo"], cwd=base["cwd"], title=h["title"], kind=h["kind"]
            )
            merged.append(base)
            consumed.add(sid)
        else:
            merged.append(h)

    for sid, base in session_by_id.items():
        if sid not in consumed:
            merged.append(base)

    return merged


class AgentsCollector(BaseCollector):
    name = "agents"
    interval_s = 5.0

    def __init__(
        self, ctx=None, herdr_bin: str = "herdr", store=None, host: str = "localhost",
        session_projects_glob: str | None = None, session_active_window_s: float = 900.0,
    ):
        super().__init__(ctx)
        self.herdr_bin = herdr_bin
        self.store = store
        self.host = host
        # None (the default) disables session-derived agent detection --
        # main.py wires this to config's claude_projects_dir explicitly. Kept
        # opt-in at the constructor level so existing herdr-only tests (and
        # any caller that doesn't pass it) never accidentally glob this
        # machine's real ~/.claude/projects.
        self.session_projects_glob = session_projects_glob
        self.session_active_window_s = session_active_window_s
        self._status_since: dict[str, tuple[str, str]] = {}

    async def collect(self) -> dict:
        # herdr is optional: a fresh install with only Claude Code (or any
        # other provider) has no herdr binary at all. That must not make this
        # collector unhealthy forever, so a missing binary (OSError from exec)
        # is treated as "no herdr agents" and falls through to session-derived
        # agents below, same "optional, auto-detected" treatment as every
        # other provider. A herdr that IS present but times out or exits
        # non-zero still raises -- that's a real error, not an absent binary.
        raw_agents: list[dict] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                self.herdr_bin, "agent", "list",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            proc = None
        if proc is not None:
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError("herdr agent list timed out") from None
            if proc.returncode != 0:
                raise RuntimeError(f"herdr exit {proc.returncode}: {stderr.decode(errors='replace')[:300]}")
            raw_agents = parse_herdr_output(stdout.decode(errors="replace"))
        worktrees = self.ctx.latest_worktrees if self.ctx is not None else []
        usage_by_session = self.ctx.usage_by_session if self.ctx is not None else {}

        herdr_built = [
            _build_herdr_agent(raw, worktrees, self.host, usage_by_session) for raw in raw_agents
        ]

        session_records: list[dict] = []
        if self.session_projects_glob:
            scanned = scan_session_agents(self.session_projects_glob, self.session_active_window_s)
            session_records = [
                _build_session_agent(rec, worktrees, self.host, usage_by_session) for rec in scanned
            ]

        # Kimi (second provider): KimiCollector owns discovery/ingestion of
        # ~/.kimi-code on its own interval and publishes ready-built agent
        # dicts (kind="kimi") to ctx.latest_kimi_agents -- folded into the
        # SAME merge_agent_sources() pass as the Claude session records, so a
        # Kimi session herdr also happens to report gets merged onto its
        # session id exactly like a Claude one does, no duplicate entry.
        kimi_records = list(self.ctx.latest_kimi_agents) if self.ctx is not None else []
        session_records = session_records + kimi_records

        merged = merge_agent_sources(herdr_built, session_records)

        ts = now_iso()
        agents = []
        for a in merged:
            key = a.pop("_status_key", None) or a.get("session_id") or a.get("cwd") or ""
            event_desc = a.pop("_event_desc", "")
            status = a["status"]
            prev = self._status_since.get(key)
            if prev is None or prev[0] != status:
                since = ts
                if self.store is not None and a.get("session_id"):
                    self.store.add_agent_status(ts, a["session_id"], status)
                    self.store.add_event(ts, "agent_status", "info", event_desc, a["session_id"])
            else:
                since = prev[1]
            self._status_since[key] = (status, since)
            a["status_since"] = since
            agents.append(a)

        # Fleet-wide merge: fold in every remote host's last-known agents.
        # Each remote agent already carries repo/branch resolved against that
        # SAME host's own worktrees (done inside remote_probe.py), so no
        # cross-host join is needed here -- only host-tagging staleness,
        # recomputed live from the host's CURRENT ok state on every tick
        # (agents runs every 5s; remote runs every 120s, so this reflects
        # reachability changes faster than remote_hosts itself refreshes).
        remote_hosts = self.ctx.remote_hosts if self.ctx is not None else {}
        for rh in remote_hosts.values():
            stale = not rh.get("ok", False)
            for a in rh.get("agents", []):
                remote_entry = dict(a)
                remote_entry["stale"] = stale
                agents.append(remote_entry)

        if self.ctx is not None:
            self.ctx.latest_agents = agents

        return {"agents": agents}
