"""agents collector: `herdr agent list` (one line of JSON on stdout).

Verified shape on this host (2026-09-18):
  {"id":"cli:agent:list","result":{"agents":[{...}],"type":"agent_list"}}
Each agent: agent, agent_status (idle|working|done|...), cwd, foreground_cwd,
pane_id, tab_id, workspace_id, terminal_title/terminal_title_stripped, focused,
agent_session.value (the Claude sessionId).

`bead` is never resolved from `herdr agent list` itself: nothing in it ties
an agent_session to a bd actor identity (one actor claims for many
sessions), so any match from herdr's output alone would be a guess. It IS
resolved -- for kinds in bead_sessions.BEAD_TRACKED_KINDS -- from that
session's OWN transcript (see bead_sessions.py). The normal path: a
session-derived record (`_build_session_agent`/KimiCollector) already
carries its own `_transcript_paths`, found by recent file activity
(session_active_window_s), and that gets merged onto the herdr entry.

But a pane herdr still lists (status "done"/"idle") whose transcript hasn't
been touched in longer than session_active_window_s has NO session record at
all -- `_transcript_paths` is only ever set on session-derived records, so
that pane would otherwise show bead=None even while its session still holds
an unreleased, in_progress claim (the most important case for the owner: an
agent that went quiet while holding a bead is how beads get "forgotten").
For exactly that case, `collect()` falls back to `_resolve_transcript_paths`,
which looks the transcript up BY SESSION ID (not by recent activity) --
cached per (kind, session_id) across ticks, see that method's docstring.
`bead_tracked` says whether this agent's kind has a transcript extractor at
all; a kind without one (e.g. herdr reporting "codex") always gets
bead=None, honestly, rather than the misleading "no active bead" a
tracked-but-idle session would show.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
from datetime import UTC, datetime

from . import BaseCollector, CollectorIssue, now_iso
from .bead_sessions import BEAD_TRACKED_KINDS, resolve_session_bead

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
            "path": path,
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
        "_transcript_paths": [rec["path"]] if rec.get("path") else [],
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


def _bead_recency(ts: float | None, last_activity) -> float:
    """A claim candidate's recency, for "whose claim is more recent" in the
    dedupe step below. A local (claude/kimi) claim carries the transcript's
    own claim timestamp. A remote claim carries none -- remote_probe.py
    returns ONLY bead ids, nothing else leaves the remote host -- so
    `last_activity` (already part of every agent's normal payload) is the
    best available recency proxy for those. This is an approximation for
    remote agents, documented here rather than silently assumed."""
    if ts is not None:
        return ts
    if isinstance(last_activity, str) and last_activity:
        try:
            return datetime.fromisoformat(last_activity.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return float("-inf")


def _claim_recency(a: dict) -> float:
    return _bead_recency(a.get("_bead_claim_ts"), a.get("last_activity"))


def _apply_bead_cross_check(agents: list[dict], beads_by_id: dict[str, dict] | None) -> None:
    """Resolves each bead_tracked agent's final `bead`/`bead_title` from its
    raw `_bead_claims` (the ordered, most-recent-first list of unreleased
    claims `resolve_session_bead`/the remote probe produced -- see
    bead_sessions.py's docstring, Defect 3):

    1. Cross-check against CURRENT beads data: an agent's candidate list is
       first narrowed to only the claims whose bead is still `in_progress`
       RIGHT NOW (never a guess from the transcript alone -- the work may
       have ended, or someone else may have taken it, since that transcript
       line was written). If beads_by_id is None, the beads collector has
       never completed a poll on this host at all (see ctx.py) -- there is
       nothing to check against, so every candidate is dropped rather than
       trusted unchecked, per the briefing ("do not cross-check against
       nothing"). The chosen bead is the FIRST surviving candidate -- i.e.
       the most recent of this session's unreleased claims that is
       currently in_progress, not just its most recent claim outright (a
       session holding bead A (in_progress) whose most recent claim was
       bead B (now blocked) must still show A, not go blank).
    2. Dedupe: if two live agents' current candidate is the SAME bead (e.g.
       two sessions really did race, or a shared actor's claim got
       misread), only the more recently-claimed one keeps it -- the loser
       does not go straight to null, it falls through to ITS OWN next
       in_progress candidate (repeating until every collision is resolved,
       since advancing a loser can create a new collision with a third
       agent's candidate).
    """
    beads_by_id = beads_by_id or {}
    tracked = [a for a in agents if a.get("bead_tracked")]
    for a in tracked:
        claims = a.get("_bead_claims") or []
        a["_bead_candidates"] = [
            (bid, ts) for bid, ts in claims
            if (beads_by_id.get(bid) or {}).get("status") == "in_progress"
        ]
        a["_bead_idx"] = 0

    changed = True
    while changed:
        changed = False
        by_bead: dict[str, list[dict]] = {}
        for a in tracked:
            cands, idx = a["_bead_candidates"], a["_bead_idx"]
            if idx < len(cands):
                by_bead.setdefault(cands[idx][0], []).append(a)
        for claimants in by_bead.values():
            if len(claimants) < 2:
                continue

            def _candidate_recency(a: dict) -> float:
                cands, idx = a["_bead_candidates"], a["_bead_idx"]
                return _bead_recency(cands[idx][1], a.get("last_activity"))

            claimants.sort(key=_candidate_recency, reverse=True)
            for loser in claimants[1:]:
                loser["_bead_idx"] += 1
                changed = True

    for a in tracked:
        cands, idx = a["_bead_candidates"], a["_bead_idx"]
        if idx < len(cands):
            bid, ts = cands[idx]
            a["bead"] = bid
            a["bead_title"] = beads_by_id[bid].get("title")
            a["_bead_claim_ts"] = ts
        else:
            a["bead"] = None
            a["bead_title"] = None
            a["_bead_claim_ts"] = None

    for a in agents:
        a.pop("_bead_claims", None)
        a.pop("_bead_candidates", None)
        a.pop("_bead_idx", None)
        a.pop("_bead_claim_ts", None)


class AgentsCollector(BaseCollector):
    name = "agents"
    interval_s = 5.0

    def __init__(
        self, ctx=None, herdr_bin: str = "herdr", store=None, host: str = "localhost",
        session_projects_glob: str | None = None, session_active_window_s: float = 900.0,
        claude_projects_dir: str | None = None, kimi_dir: str | None = None,
        codex_dir: str | None = None, grok_dir: str | None = None, cursor_dir: str | None = None,
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
        # Base dirs for the BY-SESSION-ID transcript lookup a herdr-listed
        # pane falls back to when it has no session record at all (see
        # _resolve_transcript_paths). None disables that lookup for the same
        # "opt-in, never touch a real path a caller didn't ask for" reason
        # session_projects_glob is opt-in above -- main.py wires both from
        # config.
        self.claude_projects_dir = claude_projects_dir
        self.kimi_dir = kimi_dir
        # Same opt-in, None-disables-the-lookup rule as claude_projects_dir/
        # kimi_dir above, for the three formats added later (see
        # bead_sessions.py's module docstring for the verified on-disk
        # shape each of these lookups targets).
        self.codex_dir = codex_dir
        self.grok_dir = grok_dir
        self.cursor_dir = cursor_dir
        self._status_since: dict[str, tuple[str, str]] = {}
        # Per-transcript-path incremental scan state for the session-bead
        # extractor (see bead_sessions.py) -- one shared dict across all
        # sessions, since each entry is already keyed by absolute path.
        # Never cleared, but only ever grows by the set of transcript paths
        # actually seen live, which scan_session_agents/KimiCollector both
        # already bound to "recently active" sessions.
        self._bead_cache: dict = {}
        # (kind, session_id) -> transcript path(s), for herdr-listed panes
        # that have no session record (see _resolve_transcript_paths). Only
        # ever populated for sessions herdr is CURRENTLY reporting -- never
        # grows to cover every session ever seen -- and never caches a MISS
        # (no path found), since a pane herdr just started reporting may not
        # have written its transcript's first line yet; the lookup itself is
        # cheap (bounded by however many tracked-kind panes herdr lists this
        # tick) so retrying next tick beats a false permanent null.
        self._session_transcript_cache: dict[tuple[str, str], list[str]] = {}

    def _lookup_claude_transcript(self, session_id: str) -> list[str]:
        """A Claude session's jsonl lives at
        <claude_projects_dir>/<project-dir>/<session-id>.jsonl -- but
        `project-dir` is NOT derivable from the pane's cwd (it can be a
        dash-encoded path for a DIFFERENT, symlinked path than the cwd herdr
        reports -- verified live on this host 2026-09-23), so this looks the
        file up by filename across every project dir instead of guessing a
        project dir from cwd. glob.glob("<dir>/*/<id>.jsonl") costs one
        readdir of claude_projects_dir (to expand the "*") plus one stat per
        project dir -- not a walk of any project dir's own contents."""
        if not self.claude_projects_dir:
            return []
        pattern = os.path.join(os.path.expanduser(self.claude_projects_dir), "*", f"{session_id}.jsonl")
        return glob.glob(pattern)[:1]

    def _lookup_kimi_transcript(self, session_id: str) -> list[str]:
        # Local import: kimi.py imports FROM this module (agent_label,
        # best_worktree_match, short_cwd) at module scope, so a module-level
        # import here would be circular. The cost of a deferred import is
        # paid once per interpreter (module caching), not per call.
        from .kimi import find_kimi_session_wire_paths

        if not self.kimi_dir:
            return []
        return find_kimi_session_wire_paths(os.path.expanduser(self.kimi_dir), session_id)

    def _lookup_codex_transcript(self, session_id: str) -> list[str]:
        """A Codex session's rollout jsonl lives at
        <codex_dir>/sessions/YYYY/MM/DD/rollout-<timestamp>-<session-id>.jsonl
        -- the session id was verified live to appear VERBATIM at the end of
        the filename (after the timestamp prefix bd_sessions.py's own
        extractor never reads), so this globs across every year/month/day
        dir rather than trying to derive the date from anything herdr
        reports (herdr gives no session-start date at all)."""
        if not self.codex_dir:
            return []
        pattern = os.path.join(
            os.path.expanduser(self.codex_dir), "sessions", "*", "*", "*", f"rollout-*-{session_id}.jsonl"
        )
        return glob.glob(pattern)[:1]

    def _lookup_grok_transcript(self, session_id: str) -> list[str]:
        """A Grok session's own dir is
        <grok_dir>/sessions/<url-quoted-cwd>/<session-id>/ -- verified live:
        the dir is named EXACTLY the session id herdr reports, under a
        parent dir per (URL-quoted) working directory this host has ever
        run Grok in, so this globs across every quoted-cwd dir rather than
        re-deriving the quoting from the pane's own cwd (which may not even
        be the cwd the session was STARTED in). Returns the two paths
        GrokExtractor needs, in the fixed [chat_history.jsonl, events.jsonl]
        order `_scan_grok_group` requires -- or [] if either file is
        missing (e.g. a session with no conversation yet), same "nothing to
        extract" signal every other lookup gives for an empty/absent
        transcript."""
        if not self.grok_dir:
            return []
        dirs = glob.glob(os.path.join(os.path.expanduser(self.grok_dir), "sessions", "*", session_id))
        if not dirs:
            return []
        chat_path = os.path.join(dirs[0], "chat_history.jsonl")
        events_path = os.path.join(dirs[0], "events.jsonl")
        if not os.path.exists(chat_path) or not os.path.exists(events_path):
            return []
        return [chat_path, events_path]

    def _lookup_cursor_transcript(self, session_id: str) -> list[str]:
        """A Cursor session's store.db lives at
        <cursor_dir>/chats/<workspace-hash>/<session-id>/store.db --
        verified live: the chats/<hash>/<id> dir name is the SAME session
        id herdr reports for every session that has actually run a
        conversation (hasConversation: true in that dir's meta.json); a
        pane herdr lists that hasn't run one yet has the dir but no
        store.db, so this returns [] for it -- correctly "no bead claims
        yet", not a lookup failure. (A second, separate transcript exists
        at ~/.cursor/projects/<project>/agent-transcripts/<id>/<id>.jsonl
        with a matching id, but it records no tool RESULT at all -- see
        bead_sessions.py's module docstring -- so it is not used here.)"""
        if not self.cursor_dir:
            return []
        pattern = os.path.join(os.path.expanduser(self.cursor_dir), "chats", "*", session_id, "store.db")
        return glob.glob(pattern)[:1]

    def _resolve_transcript_paths(self, kind: str, session_id: str) -> list[str]:
        """For a herdr-listed pane with no session record at all -- its
        transcript's mtime (Claude) or state.json updatedAt (Kimi) fell
        outside session_active_window_s, e.g. herdr still shows the pane as
        "done"/"idle" long after its last bd activity -- look up that
        session's transcript BY SESSION ID instead of by recent file
        activity, so its unreleased bead claim is still found no matter how
        stale the file looks. Cached across ticks by (kind, session_id): a
        cache hit is reused without touching the filesystem again UNLESS one
        of its cached paths has since disappeared (session ended and its log
        was pruned/archived), which forces one fresh lookup."""
        key = (kind, session_id)
        cached = self._session_transcript_cache.get(key)
        if cached and all(os.path.exists(p) for p in cached):
            return cached
        if kind == "claude":
            paths = self._lookup_claude_transcript(session_id)
        elif kind == "kimi":
            paths = self._lookup_kimi_transcript(session_id)
        elif kind == "codex":
            paths = self._lookup_codex_transcript(session_id)
        elif kind == "grok":
            paths = self._lookup_grok_transcript(session_id)
        elif kind == "cursor":
            paths = self._lookup_cursor_transcript(session_id)
        else:
            paths = []
        if paths:
            self._session_transcript_cache[key] = paths
        else:
            self._session_transcript_cache.pop(key, None)
        return paths

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
            # herdr IS present here (the exec above succeeded), so a failure
            # from this point on is a real problem with a configured tool,
            # not an absent optional dependency -- optional=False, unlike
            # the collector-wide "no herdr at all" case above (which isn't
            # even an error: it falls through to session-derived agents).
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise CollectorIssue(
                    "command_failed", "herdr agent list timed out after 10s",
                    remedy="Check that herdr is installed correctly and responding "
                    "(run `herdr agent list` by hand).",
                ) from None
            if proc.returncode != 0:
                out_text = stdout.decode(errors="replace").strip()
                err_text = stderr.decode(errors="replace").strip()
                detail = err_text[:300] or out_text[:300] or f"(no output on exit {proc.returncode})"
                raise CollectorIssue(
                    "command_failed", f"herdr exited {proc.returncode}: {detail}",
                    remedy="Run `herdr agent list` by hand to see the full error.",
                )
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
            paths = a.pop("_transcript_paths", None)
            kind = a.get("kind")
            a["bead_tracked"] = kind in BEAD_TRACKED_KINDS
            a["bead_title"] = None
            a["bead"] = None
            # No session record for this session id at all (only herdr knows
            # about it) -- fall back to a by-session-id lookup rather than
            # leaving this pane's bead permanently null just because its
            # transcript went quiet (see _resolve_transcript_paths).
            if not paths and a["bead_tracked"] and a.get("session_id"):
                paths = self._resolve_transcript_paths(kind, a["session_id"])
            if a["bead_tracked"] and paths:
                a["_bead_claims"] = resolve_session_bead(kind, paths, self._bead_cache)
            else:
                a["_bead_claims"] = []
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
                remote_entry.setdefault("bead_tracked", remote_entry.get("kind") in BEAD_TRACKED_KINDS)
                remote_entry["bead"] = None
                remote_entry.setdefault("bead_title", None)
                # remote_probe.py returns ONLY an ordered list of unreleased
                # claim ids ("bead_claims"), never a claim timestamp -- nothing
                # but ids leaves the remote host. Rewrap it into this
                # collector's own (bead_id, ts) shape (ts always None here) so
                # _apply_bead_cross_check can apply the exact same in_progress
                # selection/dedupe rule to remote sessions as local ones; see
                # that function's docstring for how recency is approximated
                # (last_activity) for a remote claimant with no ts.
                remote_claim_ids = remote_entry.pop("bead_claims", None) or []
                remote_entry["_bead_claims"] = [(bid, None) for bid in remote_claim_ids]
                agents.append(remote_entry)

        beads_by_id = self.ctx.latest_beads_by_id if self.ctx is not None else None
        _apply_bead_cross_check(agents, beads_by_id)

        if self.ctx is not None:
            self.ctx.latest_agents = agents

        return {"agents": agents}
