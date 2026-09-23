"""Validates the /api/snapshot response keeps the frozen SPEC shape even when
every collector is failing -- one dead source must never blank the page."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from critdash.collectors import BaseCollector, Scheduler
from critdash.state import SnapshotStore

COLLECTOR_NAMES = ["beads", "agents", "worktrees", "usage", "dispatch", "system", "remote"]


class AlwaysFailCollector(BaseCollector):
    interval_s = 0.01

    def __init__(self, name: str):
        super().__init__()
        self.name = name

    async def collect(self) -> dict:
        raise RuntimeError(f"{self.name}: simulated source outage")


def build_test_app() -> tuple[FastAPI, SnapshotStore]:
    snap = SnapshotStore(host="localhost-test")

    def on_result(name, data):
        for key, value in data.items():
            snap.update_path(key, value)

    def on_health(_name, _health):
        snap.update_sources(scheduler.health_snapshot())

    scheduler = Scheduler(on_result=on_result, on_health=on_health)
    for name in COLLECTOR_NAMES:
        scheduler.register(AlwaysFailCollector(name))

    app = FastAPI()

    @app.get("/api/snapshot")
    async def get_snapshot():
        return JSONResponse(snap.full_snapshot())

    @app.get("/api/healthz")
    async def healthz():
        collectors = scheduler.health_snapshot()
        return {"ok": all(h["ok"] for h in collectors.values()), "collectors": collectors}

    return app, snap, scheduler


@pytest.fixture
def failing_client():
    app, snap, scheduler = build_test_app()
    import asyncio

    async def run_all():
        for name in COLLECTOR_NAMES:
            await scheduler.run_once(name)

    asyncio.run(run_all())
    return TestClient(app)


def test_snapshot_has_all_top_level_keys_when_every_collector_fails(failing_client):
    resp = failing_client.get("/api/snapshot")
    assert resp.status_code == 200
    doc = resp.json()
    for key in (
        "generated_at", "host", "uptime_s", "sources", "hosts", "beads", "agents",
        "worktrees", "usage", "dispatch", "system", "events",
    ):
        assert key in doc, f"missing top-level key: {key}"


def test_all_sources_marked_not_ok_and_stale(failing_client):
    doc = failing_client.get("/api/snapshot").json()
    for name in COLLECTOR_NAMES:
        assert name in doc["sources"], f"missing source health for {name}"
        health = doc["sources"][name]
        assert health["ok"] is False
        assert health["stale"] is True
        assert health["error"] is not None
        assert "simulated source outage" in health["error"]


def test_default_shapes_survive_total_outage(failing_client):
    doc = failing_client.get("/api/snapshot").json()
    # nothing populated these (every collector raised), so the SPEC-shaped
    # empty defaults from state.empty_snapshot() must still be present and
    # of the right type, not None / missing.
    assert doc["beads"]["items"] == []
    assert set(doc["beads"]["lanes"].keys()) == {"ready", "in_progress", "blocked", "review"}
    assert doc["agents"] == []
    assert doc["worktrees"] == []
    assert doc["usage"]["totals"] == {}
    assert doc["usage"]["by_model"] == []
    assert doc["usage"]["by_host"] == []
    assert "block" not in doc["usage"]
    assert doc["hosts"] == []
    assert doc["dispatch"]["routes"] == []
    assert isinstance(doc["system"], dict)
    assert doc["events"] == []


def test_healthz_reports_overall_not_ok(failing_client):
    resp = failing_client.get("/api/healthz")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["ok"] is False
    assert len(doc["collectors"]) == len(COLLECTOR_NAMES)
