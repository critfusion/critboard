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


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class SourceHealth:
    ok: bool = False
    last_ok: str | None = None
    last_run: str | None = None
    duration_ms: float = 0.0
    error: str | None = None
    stale: bool = True

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "last_ok": self.last_ok,
            "last_run": self.last_run,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "stale": self.stale,
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
            st.health.duration_ms = round(duration_ms, 2)
            st.health.last_ok = run_ts
            st.last_ok_ts = time.monotonic()
            st.health.stale = False
            if self._on_result is not None:
                self._on_result(collector.name, data)
        except Exception as exc:  # noqa: BLE001 - collectors must never kill the scheduler
            duration_ms = (time.monotonic() - start) * 1000.0
            st.health.ok = False
            st.health.error = f"{type(exc).__name__}: {exc}"
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
