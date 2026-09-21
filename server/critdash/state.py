"""In-memory snapshot store shared by collectors, the SSE broadcaster, and /api/snapshot."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


EMPTY_SOURCE_HEALTH = {
    "ok": False,
    "last_ok": None,
    "last_run": None,
    "duration_ms": 0.0,
    "error": None,
    "stale": True,
}


def empty_snapshot(host: str) -> dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "host": host,
        "uptime_s": 0,
        "sources": {},
        "hosts": [],
        "beads": {
            "stats": {}, "items": [],
            "lanes": {"ready": [], "in_progress": [], "blocked": [], "review": []},
        },
        "agents": [],
        "worktrees": [],
        "usage": {
            "totals": {},
            "by_model": [],
            "by_project": [],
            "by_agent": [],
            "by_host": [],
            "timeline": [],
            "burn": {
                "usd_per_hour_1h": 0.0, "usd_per_hour_24h": 0.0,
                "projected_month_usd": 0.0, "tokens_per_min_5m": 0.0,
            },
            "cache_hit_ratio_today": 0.0,
            "block": {
                "started_at": None, "ends_at": None, "tokens": 0,
                "cost_usd": 0.0, "pct_elapsed": 0.0,
            },
            "budget": {
                "monthly_usd": 0.0, "spent_mtd_usd": 0.0, "pct": 0.0,
                "projected_month_usd": 0.0,
            },
        },
        "dispatch": {"routes": [], "paused_all": False, "recent": []},
        "system": {},
        "events": [],
        "version": {"build": None, "started_at": None},
        "analytics": {
            "errors": {
                "window": "7d", "top_errors": [], "by_tool": [], "trouble_files": [],
                "api_errors": [], "total_errors": 0, "truncated": False,
            },
            "tools": {
                "window": "7d", "usage": [],
                "decisions": {"accepted": 0, "rejected": 0, "total": 0}, "truncated": False,
            },
            "subagents": {
                "window": "7d", "sessions": [],
                "totals": {"main_cost_usd": 0.0, "sidechain_cost_usd": 0.0, "sidechain_share": 0.0},
                "truncated": False,
            },
            "productivity": {
                "commits_7d": 0, "commits_30d": 0, "lines_added_7d": 0, "lines_removed_7d": 0,
                "files_changed_7d": 0, "by_repo": [], "truncated": False,
            },
        },
    }


class SnapshotStore:
    """Holds the current snapshot in memory and fans out patches to SSE subscribers.

    Not thread-safe across OS threads by design -- everything here runs on the
    single asyncio event loop (collectors are async, FastAPI handlers are async).
    """

    def __init__(self, host: str):
        self.host = host
        self.start_time = time.monotonic()
        self.snapshot: dict[str, Any] = empty_snapshot(host)
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _publish(self, event: str, data: Any) -> None:
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def touch(self) -> None:
        self.snapshot["generated_at"] = now_iso()
        self.snapshot["uptime_s"] = int(time.monotonic() - self.start_time)

    def update_path(self, key: str, value: Any, publish: bool = True) -> None:
        """Replace a top-level snapshot key (e.g. 'agents', 'beads') and emit
        a patch event with the dotted path -> value."""
        self.snapshot[key] = value
        self.touch()
        if publish:
            self._publish("patch", {"paths": {key: value}})

    def update_nested(self, top_key: str, sub_key: str, value: Any, publish: bool = True) -> None:
        """Replace snapshot[top_key][sub_key] (e.g. usage.burn) and emit a
        dotted-path patch."""
        self.snapshot.setdefault(top_key, {})[sub_key] = value
        self.touch()
        if publish:
            self._publish("patch", {"paths": {f"{top_key}.{sub_key}": value}})

    def set_version(self, value: Any, publish: bool = True) -> None:
        """Like update_path but emits a dedicated `event: version` SSE frame
        (not wrapped in the generic `patch` envelope), per SPEC Task 3 --
        the frontend's reload banner listens for this event name directly."""
        self.snapshot["version"] = value
        self.touch()
        if publish:
            self._publish("version", value)

    def update_sources(self, sources: dict) -> None:
        self.snapshot["sources"] = sources
        self.touch()
        self._publish("patch", {"paths": {"sources": sources}})

    def full_snapshot(self) -> dict:
        self.touch()
        return self.snapshot

    def publish_resync(self) -> None:
        self._publish("snapshot", self.full_snapshot())

    def publish_ping(self) -> None:
        self._publish("ping", {})
