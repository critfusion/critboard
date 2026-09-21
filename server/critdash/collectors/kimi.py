"""Kimi Code collector: a second coding-agent provider alongside Claude Code.

Verified on-disk format (maintainer's dev host, 2026-09-18), confirmed live with:
  cat ~/.kimi-code/session_index.jsonl
  python3 -m json.tool < ~/.kimi-code/workspaces.json
  find ~/.kimi-code/sessions -maxdepth 4 -name wire.jsonl

Root is ~/.kimi-code (configurable: sources.json `kimi_dir`):
  session_index.jsonl -- one JSON object per line, the canonical session list:
      {"sessionId": "session_<uuid>", "sessionDir": "/abs/.../session_<uuid>",
       "workDir": "/abs/path"}
  workspaces.json -- {"workspaces": {"wd_<name>_<hash>": {"root": "...", ...}}}.
      Used only as a fallback for a session's cwd when state.json's own `cwd`
      is missing -- state.json is otherwise authoritative (briefing: "cwd
      from state.json").
  sessions/<wd_*>/session_<uuid>/state.json --
      {"id", "cwd", "createdAt", "updatedAt" (EPOCH MS), "archived",
       "agents": {"main": {"homedir": "..."}}, "lastTurnReason"}.
  sessions/<wd_*>/session_<uuid>/agents/<agentId>/wire.jsonl -- the event
      stream, one JSON object per line, `time` fields are EPOCH MS:
        llm.request            -- {"model", "modelAlias", ...} (current model)
        token_counting.*       -- {"tokens": <int total>, "time"} (the ONLY
                                   record type observed carrying a turn's
                                   token count -- two variant names seen live,
                                   "token_counting.turn_recorded" (has a
                                   turnId) and "token_counting.measured" (does
                                   not) -- treated identically here since both
                                   carry "tokens"/"time" and nothing else this
                                   module needs)
        turn.ended              -- {"reason", "error": {"code", "message"}}
                                    when reason == "failed"

Two constraints that shape this module (do not "fix" around them):
  1. Kimi gives ONE `tokens` total per turn -- no input/output/cache split.
     (A newer live session was ALSO observed emitting a `usage.record` event
     with an inputOther/output/inputCacheRead/inputCacheCreation breakdown
     that sums to the same total -- but that event type is not in the
     briefing's verified format and is not relied on here; only `tokens` is
     read, so this module works whether or not a given Kimi CLI version emits
     it.)
  2. Kimi bills by subscription quota, not per token -- computing a dollar
     cost would be invented. Every rollup this module feeds reports Kimi's
     cost_usd as None (null), never 0.0 -- null means "not applicable", 0.0
     means "free", and conflating them would silently understate a fleet
     total that excludes Kimi's real (unknown) cost.

Ingestion is incremental: each session agent's wire.jsonl is tailed by byte
offset using the SAME `file_offsets` table the Claude collectors use, keyed
under a `kimi::` prefix (mirrors analytics.py's `analytics::` prefix) so
Kimi ingestion never contends with either Claude collector's own offset for
the same path (a real risk only in the sense that all three share one
table -- the keys never collide since Kimi's wire.jsonl paths are disjoint
from Claude's project jsonl paths). A session's current model is tracked
per collector instance across polls (self._session_model), same "keep
small, cross-poll state" pattern as AgentsCollector._status_since.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from . import BaseCollector
from .agents import agent_label, best_worktree_match, short_cwd
from .analytics import classify_error, make_example

_TAIL_READ_BYTES = 65536
_OFFSET_PREFIX = "kimi::"

# Kimi's wire.jsonl carries no `type` field for the token-count record that
# is name-stable across CLI versions -- both variants observed live are
# handled by matching the shared prefix.
_TOKEN_COUNTING_PREFIX = "token_counting."


def epoch_ms_to_iso(ms) -> str | None:
    """Kimi's wire.jsonl/state.json timestamps are epoch MILLISECONDS, unlike
    Claude's ISO8601 `timestamp` strings -- this is the one conversion point
    every Kimi ts passes through, so a wrong unit (e.g. treating it as
    seconds) fails loudly in one place instead of silently in several."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except (TypeError, ValueError, OSError):
        return None


def load_session_index(kimi_dir: str) -> list[dict]:
    """Parse session_index.jsonl -- the canonical enumeration of Kimi
    sessions. Malformed/incomplete lines are skipped, never fatal."""
    path = os.path.join(kimi_dir, "session_index.jsonl")
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = doc.get("sessionId")
        sdir = doc.get("sessionDir")
        if not sid or not sdir:
            continue
        out.append({"session_id": sid, "session_dir": sdir, "work_dir": doc.get("workDir")})
    return out


def load_workspaces(kimi_dir: str) -> dict[str, dict]:
    """Parse workspaces.json -> {workspace_id: {"root", "name", ...}}. Used
    only as a fallback source for cwd (see module docstring)."""
    path = os.path.join(kimi_dir, "workspaces.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    workspaces = doc.get("workspaces")
    return workspaces if isinstance(workspaces, dict) else {}


def workspace_id_from_session_dir(session_dir: str) -> str | None:
    """A session dir is .../sessions/<workspace_id>/session_<uuid> -- the
    workspace id is its grandparent-relative basename."""
    if not session_dir:
        return None
    return os.path.basename(os.path.dirname(session_dir.rstrip("/"))) or None


def load_session_state(session_dir: str) -> dict | None:
    """state.json is a small, complete JSON document (not a jsonl log) --
    always a full read, never incremental. Returns None if missing/invalid so
    a deleted/archived session directory is silently skipped rather than
    crashing discovery."""
    path = os.path.join(session_dir, "state.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def discover_kimi_sessions(kimi_dir: str) -> list[dict]:
    """Enumerate every Kimi session on disk: session_index.jsonl for the
    canonical (id, dir, workDir) triple, state.json for the live cwd/
    updatedAt/agents, workspaces.json only as a cwd fallback. Returns one
    dict per session with a resolved `agent_wire_paths` map (agentId ->
    wire.jsonl path) so callers never need to know the on-disk layout."""
    index = load_session_index(kimi_dir)
    if not index:
        return []
    workspaces = load_workspaces(kimi_dir)

    out: list[dict] = []
    for entry in index:
        session_dir = entry["session_dir"]
        state = load_session_state(session_dir)
        if state is None:
            continue
        cwd = state.get("cwd") or entry.get("work_dir")
        if not cwd:
            wsid = workspace_id_from_session_dir(session_dir)
            cwd = (workspaces.get(wsid) or {}).get("root")
        agents = state.get("agents") or {}
        wire_paths = {
            agent_id: os.path.join(info["homedir"], "wire.jsonl")
            for agent_id, info in agents.items()
            if isinstance(info, dict) and info.get("homedir")
        }
        out.append({
            "session_id": entry["session_id"],
            "session_dir": session_dir,
            "cwd": cwd,
            "updated_at_ms": state.get("updatedAt"),
            "archived": bool(state.get("archived", False)),
            "last_turn_reason": state.get("lastTurnReason"),
            "agent_wire_paths": wire_paths,
        })
    return out


def tail_last_request(wire_path: str, tail_bytes: int = _TAIL_READ_BYTES) -> tuple[str, int] | None:
    """Scan the tail of a wire.jsonl backward for the most recent
    llm.request -- never a full-file parse (a long session's wire log can be
    large). Returns (model, time_ms) so a caller comparing several agents'
    wire.jsonl files (see tail_last_model_across_agents) can tell which one's
    llm.request is actually the most recent, not just which file happens to
    be listed first. Prefers modelAlias (e.g. "kimi-code/kimi-for-coding",
    provider-qualified) over the bare `model` field, falling back to it when
    modelAlias is absent."""
    try:
        size = os.path.getsize(wire_path)
        with open(wire_path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            chunk = f.read()
    except OSError:
        return None
    for raw_line in reversed(chunk.split(b"\n")):
        line = raw_line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict) or doc.get("type") != "llm.request":
            continue
        model = doc.get("modelAlias") or doc.get("model")
        time_ms = doc.get("time")
        if model and isinstance(time_ms, int):
            return model, time_ms
    return None


def tail_last_model(wire_path: str, tail_bytes: int = _TAIL_READ_BYTES) -> str | None:
    result = tail_last_request(wire_path, tail_bytes)
    return result[0] if result else None


def tail_last_model_across_agents(
    wire_paths: dict[str, str], tail_bytes: int = _TAIL_READ_BYTES,
) -> str | None:
    """A Kimi session can have MORE than one live agent -- the orchestrator
    ("main") plus any subagents it dispatched (state.json's `agents` dict
    then has "main", "agent-0", "agent-1", ... -- verified live on the
    maintainer's dev host, 2026-09-18: an active session had 4). Checking only "main"'s wire.jsonl
    tail can miss the model entirely when main's last `tail_bytes` are
    dominated by tool-call bookkeeping with no llm.request in that window,
    while a subagent's log has a fresh one -- so every agent's wire.jsonl is
    tail-read and the model from whichever has the most recent `time` wins."""
    best_model: str | None = None
    best_time = -1
    for wire_path in wire_paths.values():
        result = tail_last_request(wire_path, tail_bytes)
        if result is None:
            continue
        model, time_ms = result
        if time_ms > best_time:
            best_time = time_ms
            best_model = model
    return best_model


def classify_kimi_error(message: str | None, code: str | None) -> str:
    """Route through the SAME rule table analytics.py's tool-call errors use
    (classify_error), on whichever text is available -- message first (more
    specific), falling back to the bare error code. The quota/billing rule
    that Kimi's real failure needs ("quota_exceeded") lives in that shared
    table, added alongside the existing rules -- see analytics.py."""
    return classify_error(message or code or "")


def _session_project(cwd: str | None) -> str | None:
    """Kimi's cwd is already a real filesystem path (unlike Claude's
    dash-encoded project dirnames), so the project name is just its
    basename -- no decode step needed."""
    if not cwd:
        return None
    return os.path.basename(cwd.rstrip("/")) or None


def _build_kimi_agent(
    rec: dict, worktrees: list[dict], host: str, usage_by_session: dict, now: datetime,
    working_threshold_s: float,
) -> dict:
    session = rec["session_id"]
    cwd = rec.get("cwd")
    wt = best_worktree_match(cwd, worktrees, host=host) if cwd else None
    repo = wt.get("repo") if wt else None
    branch = wt.get("branch") if wt else None

    updated_iso = epoch_ms_to_iso(rec.get("updated_at_ms"))
    age_s = None
    if updated_iso:
        try:
            updated_dt = datetime.fromisoformat(updated_iso.replace("Z", "+00:00"))
            age_s = (now - updated_dt).total_seconds()
        except ValueError:
            age_s = None
    status = "working" if (age_s is not None and age_s <= working_threshold_s) else "idle"

    usage = usage_by_session.get(session) or {}
    tokens_today = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                     "total": usage.get("tokens", 0)}

    return {
        "id": session,
        "kind": "kimi",
        "status": status,
        "cwd": cwd,
        "cwd_short": short_cwd(cwd),
        "repo": repo,
        "branch": branch,
        "pane": None,
        "workspace": None,
        "title": None,
        "label": agent_label(repo=repo, cwd=cwd, title=None, kind="kimi"),
        "focused": False,
        "session_id": session,
        "bead": None,
        "last_activity": usage.get("last_ts") or updated_iso,
        "tokens_today": tokens_today,
        # null, not 0.0 -- Kimi bills by subscription quota, a dollar figure
        # here would be invented. See module docstring.
        "cost_today_usd": None,
        "msg_count_today": usage.get("messages", 0),
        "subagents_active": max(0, len(rec["agent_wire_paths"]) - 1),
        "model": tail_last_model_across_agents(rec["agent_wire_paths"]),
        "host": host,
        "stale": False,
        "source": "session",
        "_status_key": session,
        "_event_desc": f"kimi session {status} ({cwd})",
    }


class KimiCollector(BaseCollector):
    name = "kimi"
    interval_s = 10.0

    def __init__(
        self, ctx=None, store=None, host: str = "localhost", kimi_dir: str = "~/.kimi-code",
        session_active_window_s: float = 900.0, working_threshold_s: float = 120.0,
    ):
        super().__init__(ctx)
        self.store = store
        self.host = host
        self.kimi_dir = kimi_dir
        self.session_active_window_s = session_active_window_s
        self.working_threshold_s = working_threshold_s
        # per-session "most recently seen llm.request model", carried across
        # polls the same way AgentsCollector carries _status_since -- an
        # incremental wire.jsonl read only sees NEW lines each poll, so the
        # model chosen at the moment of a later token_counting.* line (which
        # may land in a different poll than its llm.request) must be
        # remembered here rather than re-derived from scratch every time.
        self._session_model: dict[str, str | None] = {}

    async def collect(self) -> dict:
        kimi_dir = os.path.expanduser(self.kimi_dir)
        if not os.path.isdir(kimi_dir):
            # Silent no-op: Kimi is not installed on this host. Not an error
            # -- see remote_probe.py's mirror of this same rule.
            if self.ctx is not None:
                self.ctx.latest_kimi_agents = []
            return {}

        sessions = discover_kimi_sessions(kimi_dir)
        now = datetime.now(UTC)
        worktrees = self.ctx.latest_worktrees if self.ctx is not None else []

        new_turns: list[dict] = []
        new_errors: list[dict] = []
        for rec in sessions:
            project = _session_project(rec.get("cwd"))
            for wire_path in rec["agent_wire_paths"].values():
                turns, errors = self._ingest_wire_file(wire_path, rec["session_id"], project)
                new_turns.extend(turns)
                new_errors.extend(errors)

        if self.store is not None:
            if new_turns:
                self.store.insert_kimi_turn_events(new_turns)
            if new_errors:
                self.store.insert_kimi_error_events(new_errors)

        usage_by_session: dict[str, dict] = {}
        if self.store is not None:
            today_start = now.strftime("%Y-%m-%dT00:00:00Z")
            for row in self.store.kimi_usage_by_session(today_start, host=self.host):
                usage_by_session[row["session_id"]] = {
                    "tokens": row["tokens"], "messages": row["messages"], "last_ts": row["last_ts"],
                }

        active = [
            rec for rec in sessions
            if rec.get("updated_at_ms") is not None
            and (now - datetime.fromtimestamp(rec["updated_at_ms"] / 1000.0, tz=UTC)).total_seconds()
            <= self.session_active_window_s
        ]
        agents = [
            _build_kimi_agent(rec, worktrees, self.host, usage_by_session, now, self.working_threshold_s)
            for rec in active
        ]

        if self.ctx is not None:
            self.ctx.latest_kimi_agents = agents

        return {}

    def _ingest_wire_file(self, wire_path: str, session_id: str, project: str | None) -> tuple[list, list]:
        turns: list[dict] = []
        errors: list[dict] = []
        if self.store is None:
            return turns, errors

        offset_key = _OFFSET_PREFIX + wire_path
        try:
            st = os.stat(wire_path)
        except OSError:
            return turns, errors
        inode, size = st.st_ino, st.st_size

        prev = self.store.get_offset(offset_key)
        offset = 0
        if prev is not None:
            prev_inode, prev_offset, _prev_mtime = prev
            if prev_inode == inode and prev_offset <= size:
                offset = prev_offset

        if offset >= size:
            self.store.set_offset(offset_key, inode, size, st.st_mtime)
            return turns, errors

        with open(wire_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()

        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            self.store.set_offset(offset_key, inode, offset, st.st_mtime)
            return turns, errors
        usable = chunk[: last_nl + 1]
        new_offset = offset + len(usable)

        for raw_line in usable.split(b"\n"):
            line = raw_line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(doc, dict):
                continue
            rtype = doc.get("type") or ""

            if rtype == "llm.request":
                model = doc.get("modelAlias") or doc.get("model")
                if model:
                    self._session_model[session_id] = model
                continue

            if rtype.startswith(_TOKEN_COUNTING_PREFIX):
                ts_ms = doc.get("time")
                ts = epoch_ms_to_iso(ts_ms)
                if ts_ms is None or ts is None:
                    continue
                turns.append({
                    "host": self.host, "session_id": session_id, "ts": ts, "ts_ms": ts_ms,
                    "model": self._session_model.get(session_id), "tokens": doc.get("tokens") or 0,
                    "project": project,
                })
                continue

            if rtype == "turn.ended" and doc.get("reason") == "failed":
                ts_ms = doc.get("time")
                ts = epoch_ms_to_iso(ts_ms)
                if ts_ms is None or ts is None:
                    continue
                error = doc.get("error") or {}
                code = error.get("code")
                message = error.get("message")
                errors.append({
                    "host": self.host, "session_id": session_id, "ts": ts, "ts_ms": ts_ms,
                    "kind": classify_kimi_error(message, code), "code": code,
                    "example": make_example(message),
                })

        self.store.set_offset(offset_key, inode, new_offset, st.st_mtime)
        return turns, errors


__all__ = [
    "KimiCollector",
    "classify_kimi_error",
    "discover_kimi_sessions",
    "epoch_ms_to_iso",
    "load_session_index",
    "load_session_state",
    "load_workspaces",
    "tail_last_model",
    "tail_last_model_across_agents",
    "tail_last_request",
    "workspace_id_from_session_dir",
]
