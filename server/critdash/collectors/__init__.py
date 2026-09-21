"""Collector framework: BaseCollector + scheduler.

Each collector owns one slice of the snapshot. The scheduler runs every
collector on its own asyncio task at its own interval, catches every
exception a collector raises, and records source health into
snapshot.sources[name] without ever letting one collector's failure stop
another's schedule.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("critdash.collectors")

# The small, fixed vocabulary every collector classifies its failures into --
# kept short and meaningful rather than growing one code per collector:
#   dependency_missing -- an external binary this collector shells out to
#                          is not installed / not on PATH.
#   config_missing      -- a config file this collector needs (e.g. an env
#                          file it sources) does not exist.
#   command_failed       -- the external command ran and exited non-zero (or
#                          produced output the collector couldn't use); the
#                          real stderr/stdout is in `detail`, never guessed.
#   unreachable           -- the command timed out or otherwise indicates the
#                          thing it talks to over the network isn't
#                          responding.
REASON_CODES = ("dependency_missing", "config_missing", "command_failed", "unreachable")


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class CollectorIssue(Exception):
    """Raise from `collect()` to report a structured, actionable failure
    instead of a bare exception string reaching the browser.

    reason_code: one of REASON_CODES above.
    detail: the real, specific reason -- a path that was checked, a binary
        name, or actual (trimmed) command output. Never a guess.
    remedy: what to do about it, in plain language. Leave it None rather
        than invent one for a failure mode the code cannot classify.
    optional: True when this is an unconfigured *optional* dependency (a
        normal state on a fresh install -- e.g. bd/herdr/ssh not installed
        or not set up yet). False for a genuine failure of something that IS
        configured (the binary exists, ran, and broke) -- the frontend uses
        this to pick an informational tone vs. a warning one.
    """

    def __init__(self, reason_code: str, detail: str, remedy: str | None = None, optional: bool = False):
        self.reason_code = reason_code
        self.detail = detail
        self.remedy = remedy
        self.optional = optional
        super().__init__(detail)


@dataclass
class SourceHealth:
    ok: bool = False
    last_ok: str | None = None
    last_run: str | None = None
    duration_ms: float = 0.0
    error: str | None = None
    stale: bool = True
    reason_code: str | None = None
    detail: str | None = None
    remedy: str | None = None
    optional: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "last_ok": self.last_ok,
            "last_run": self.last_run,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "stale": self.stale,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "remedy": self.remedy,
            "optional": self.optional,
        }


class BaseCollector(ABC):
    """Subclass and implement `collect()`.

    `collect()` returns the dict this collector contributes to the snapshot
    (e.g. {"beads": {...}} or {"agents": [...]}). Raising is fine -- the
    scheduler catches it and marks the source unhealthy.
    """

    name: str = "base"
    interval_s: float = 30.0

    def __init__(self, ctx: Any = None):
        self.ctx = ctx

    @abstractmethod
    async def collect(self) -> dict:
        ...


@dataclass
class _CollectorState:
    collector: BaseCollector
    health: SourceHealth = field(default_factory=SourceHealth)
    last_ok_ts: float = 0.0


class Scheduler:
    """Runs each collector on its own asyncio task at its own interval."""

    def __init__(self, on_result, on_health=None):
        """
        on_result(name: str, data: dict) -- called after a successful collect.
        on_health(name: str, health: SourceHealth) -- called after every run.
        """
        self._states: dict[str, _CollectorState] = {}
        self._tasks: list[asyncio.Task] = []
        self._on_result = on_result
        self._on_health = on_health
        self._stopping = False

    def register(self, collector: BaseCollector) -> None:
        self._states[collector.name] = _CollectorState(collector=collector)

    def health_snapshot(self) -> dict[str, dict]:
        return {name: st.health.to_dict() for name, st in self._states.items()}

    async def run_once(self, name: str) -> None:
        st = self._states[name]
        await self._run_collector(st)

    async def _run_collector(self, st: _CollectorState) -> None:
        collector = st.collector
        start = time.monotonic()
        run_ts = now_iso()
        st.health.last_run = run_ts
        try:
            data = await collector.collect()
            duration_ms = (time.monotonic() - start) * 1000.0
            st.health.ok = True
            st.health.error = None
            st.health.reason_code = None
            st.health.detail = None
            st.health.remedy = None
            st.health.optional = False
            st.health.duration_ms = round(duration_ms, 2)
            st.health.last_ok = run_ts
            st.last_ok_ts = time.monotonic()
            st.health.stale = False
            if self._on_result is not None:
                self._on_result(collector.name, data)
        except CollectorIssue as exc:
            duration_ms = (time.monotonic() - start) * 1000.0
            st.health.ok = False
            st.health.error = f"{exc.reason_code}: {exc.detail}"
            st.health.reason_code = exc.reason_code
            st.health.detail = exc.detail
            st.health.remedy = exc.remedy
            st.health.optional = exc.optional
            st.health.duration_ms = round(duration_ms, 2)
            # An unconfigured optional dependency is a normal state on a
            # fresh install, not a problem worth a warning-level log line.
            log = logger.info if exc.optional else logger.warning
            log("collector %s: %s", collector.name, st.health.error)
        except Exception as exc:  # noqa: BLE001 - collectors must never kill the scheduler
            duration_ms = (time.monotonic() - start) * 1000.0
            st.health.ok = False
            st.health.error = f"{type(exc).__name__}: {exc}"
            st.health.reason_code = None
            st.health.detail = None
            st.health.remedy = None
            st.health.optional = False
            st.health.duration_ms = round(duration_ms, 2)
            logger.warning("collector %s failed: %s", collector.name, st.health.error)
        finally:
            if st.last_ok_ts:
                age = time.monotonic() - st.last_ok_ts
                st.health.stale = age > (3 * collector.interval_s)
            else:
                st.health.stale = True
            if self._on_health is not None:
                self._on_health(collector.name, st.health)

    async def _loop_for(self, name: str) -> None:
        st = self._states[name]
        while not self._stopping:
            await self._run_collector(st)
            try:
                await asyncio.sleep(st.collector.interval_s)
            except asyncio.CancelledError:
                break

    def start(self) -> None:
        for name in self._states:
            self._tasks.append(asyncio.create_task(self._loop_for(name), name=f"collector:{name}"))

    def start_one(self, name: str) -> None:
        """Start a single collector's loop task after start() has already
        run -- used when a collector that was inactive at startup (its
        optional dependency was absent) gets registered live once periodic
        re-detection finds the dependency now present (e.g. `bd` got
        installed after the dashboard started). Every other collector's task
        is untouched."""
        self._tasks.append(asyncio.create_task(self._loop_for(name), name=f"collector:{name}"))

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
