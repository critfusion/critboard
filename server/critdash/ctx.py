"""Shared, mutable cross-collector context.

Collectors run on independent intervals (agents every 5s, worktrees every 60s,
usage every 10s) but some fields need data from another collector's *latest*
result (e.g. an agent's branch/repo comes from the worktrees collector; a
worktree's `agents` list comes from the agents collector). Rather than force
every collector onto one interval, each collector publishes its transformed
result onto this shared object, and reads whatever it needs from the others'
last-known value -- which may be up to that collector's own interval stale,
which is acceptable for a dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .store import Store


@dataclass
class AppContext:
    config: Config
    store: Store
    latest_agents: list[dict] = field(default_factory=list)
    latest_worktrees: list[dict] = field(default_factory=list)
    usage_by_session: dict[str, dict] = field(default_factory=dict)
    pricing: dict[str, Any] = field(default_factory=dict)
    # name -> {"agents": [...], "worktrees": [...], "system": {...}, "ok": bool,
    # "error": str|None, "last_run": iso, "last_ok": iso|None}, written by
    # RemoteCollector, read by AgentsCollector/WorktreesCollector to merge
    # remote rows into the fleet-wide "agents"/"worktrees" snapshot keys. On a
    # failed probe RemoteCollector overwrites only ok/error/last_run here and
    # keeps the previous agents/worktrees -- "stale but present" rather than
    # vanished, per the briefing's failure-behavior requirement.
    remote_hosts: dict[str, dict] = field(default_factory=dict)
    # written by ProductivityCollector (its own slow interval), read by
    # AnalyticsCollector on every one of its own (faster) polls and folded
    # into analytics.productivity -- see productivity.py's module docstring
    # for why these two collectors can't both return the "analytics" key.
    latest_productivity: dict = field(default_factory=dict)
    # written by KimiCollector, read by AgentsCollector on every one of its
    # own (faster) polls and folded into the merged "agents" list -- same
    # cross-collector-via-ctx pattern as remote_hosts/latest_worktrees above.
    # KimiCollector alone owns discovery/ingestion of ~/.kimi-code; it does
    # not return an "agents" key itself since AgentsCollector already owns
    # that top-level snapshot key (two collectors writing the same key would
    # stomp each other -- see collectors/remote.py's docstring on this rule).
    latest_kimi_agents: list[dict] = field(default_factory=list)
    # written by BeadsCollector on every successful poll: id -> {"status",
    # "title"}. None (the default) means the beads collector has never
    # completed a poll on this host -- e.g. no `bd` binary -- which
    # AgentsCollector's session-bead cross-check treats as "no beads data
    # to check against", not "zero beads": a bead resolved from a session
    # transcript is left null rather than trusted unchecked. Once beads
    # HAS polled at least once, this is a real (possibly empty) dict.
    latest_beads_by_id: dict[str, dict] | None = None
