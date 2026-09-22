"""GET /api/bead/{id} (briefing Task 1): validation, bd show parsing, 404, cache."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from critdash import main as main_mod
from critdash.collectors import beads as beads_mod
from critdash.collectors.beads import (
    BeadNotFoundError,
    fetch_bead_detail,
    transform_bead_detail,
    validate_bead_id,
)

# -- validate_bead_id ---------------------------------------------------------


@pytest.mark.parametrize(
    "bead_id",
    [
        "demo-fleet-q733",
        "demo-fleet-q733.1",
        "demo-fleet-592a",
        "a",
        "abc_123.4-5",
    ],
)
def test_validate_bead_id_accepts_real_shapes(bead_id):
    assert validate_bead_id(bead_id) is True


@pytest.mark.parametrize(
    "bead_id",
    [
        "",
        "-rf",
        "--json",
        "demo fleet q733",
        "id;rm -rf /",
        "id$(whoami)",
        "id`whoami`",
        "../../etc/passwd",
        "a" * 200,
    ],
)
def test_validate_bead_id_rejects_implausible_ids(bead_id):
    assert validate_bead_id(bead_id) is False


# -- transform_bead_detail -----------------------------------------------------


def test_transform_bead_detail_shape_and_design_gap():
    item = {
        "id": "demo-fleet-q733",
        "title": "t",
        "description": "d",
        "notes": "n",
        "acceptance_criteria": "a",
        "status": "open",
        "priority": 1,
        "issue_type": "epic",
        "assignee": None,
        "labels": ["dashboard"],
        "created_at": "2026-09-18T13:38:20Z",
        "updated_at": "2026-09-18T14:32:27Z",
        "closed_at": None,
        "parent": None,
        "dependencies": [
            {"id": "blocker-1", "dependency_type": "blocks"},
            {"id": "parent-1", "dependency_type": "parent-child"},
        ],
        "dependents": [
            {"id": "child-1", "dependency_type": "blocks"},
            {"id": "child-2", "dependency_type": "parent-child"},
        ],
    }
    detail = transform_bead_detail(item)
    for key in (
        "id", "title", "description", "notes", "design", "acceptance", "status",
        "priority", "type", "assignee", "labels", "created_at", "updated_at",
        "closed_at", "parent", "blocked_by", "blocks", "repo", "url",
    ):
        assert key in detail
    assert detail["design"] is None  # bd has no such field -- documented gap
    assert detail["acceptance"] == "a"
    assert detail["notes"] == "n"
    # parent-child dependency entries never show up as blockers/blocks
    assert detail["blocked_by"] == ["blocker-1"]
    assert detail["blocks"] == ["child-1"]


def test_transform_bead_detail_missing_optional_fields_are_none():
    item = {"id": "x", "title": "t"}
    detail = transform_bead_detail(item)
    assert detail["notes"] is None
    assert detail["design"] is None
    assert detail["assignee"] is None
    assert detail["labels"] == []
    assert detail["blocked_by"] == []
    assert detail["blocks"] == []


# -- fetch_bead_detail (mocked subprocess) -------------------------------------


@pytest.mark.asyncio
async def test_fetch_bead_detail_success(monkeypatch):
    payload = '[{"id": "abc-1", "title": "t", "status": "open"}]'

    async def fake_run_capture(cmd, timeout=20.0):
        assert "bd" in cmd and "show" in cmd and "abc-1" in cmd
        return 0, payload, ""

    monkeypatch.setattr(beads_mod, "_run_capture", fake_run_capture)
    detail = await fetch_bead_detail("~/.config/beads/env", "bd", "critdash", "abc-1")
    assert detail["id"] == "abc-1"
    assert detail["status"] == "open"


@pytest.mark.asyncio
async def test_fetch_bead_detail_not_found(monkeypatch):
    async def fake_run_capture(cmd, timeout=20.0):
        return 1, '{"error": "no issues found matching the provided IDs", "schema_version": 1}', "boom"

    monkeypatch.setattr(beads_mod, "_run_capture", fake_run_capture)
    with pytest.raises(BeadNotFoundError):
        await fetch_bead_detail("~/.config/beads/env", "bd", "critdash", "bogus-id")


@pytest.mark.asyncio
async def test_fetch_bead_detail_rejects_invalid_id_before_subprocess(monkeypatch):
    async def fake_run_capture(cmd, timeout=20.0):
        raise AssertionError("must not reach the subprocess for an invalid id")

    monkeypatch.setattr(beads_mod, "_run_capture", fake_run_capture)
    with pytest.raises(ValueError):
        await fetch_bead_detail("~/.config/beads/env", "bd", "critdash", "--dangerous-flag")


@pytest.mark.asyncio
async def test_fetch_bead_detail_other_failure_raises_runtime_error(monkeypatch):
    async def fake_run_capture(cmd, timeout=20.0):
        return 1, "", "some other bd error"

    monkeypatch.setattr(beads_mod, "_run_capture", fake_run_capture)
    with pytest.raises(RuntimeError):
        await fetch_bead_detail("~/.config/beads/env", "bd", "critdash", "abc-1")


# -- GET /api/bead/{id} endpoint (real app, fetch_bead_detail monkeypatched) --


def test_get_bead_detail_endpoint_success(monkeypatch):
    async def fake_fetch(beads_env, bd_bin, actor, bead_id, timeout=20.0, beads_dir=""):
        return {"id": bead_id, "title": "fake bead", "status": "open"}

    monkeypatch.setattr(main_mod, "fetch_bead_detail", fake_fetch)
    main_mod._bead_detail_cache.clear()
    client = TestClient(main_mod.app)
    resp = client.get("/api/bead/test-endpoint-ok-1")
    assert resp.status_code == 200
    assert resp.json()["id"] == "test-endpoint-ok-1"
    assert resp.headers["cache-control"] == "no-cache"


def test_get_bead_detail_endpoint_404(monkeypatch):
    async def fake_fetch(beads_env, bd_bin, actor, bead_id, timeout=20.0, beads_dir=""):
        raise BeadNotFoundError(bead_id)

    monkeypatch.setattr(main_mod, "fetch_bead_detail", fake_fetch)
    main_mod._bead_detail_cache.clear()
    client = TestClient(main_mod.app)
    resp = client.get("/api/bead/test-endpoint-missing-1")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_get_bead_detail_endpoint_rejects_invalid_id(monkeypatch):
    async def fake_fetch(beads_env, bd_bin, actor, bead_id, timeout=20.0, beads_dir=""):
        raise AssertionError("must not call fetch_bead_detail for an invalid id")

    monkeypatch.setattr(main_mod, "fetch_bead_detail", fake_fetch)
    client = TestClient(main_mod.app)
    resp = client.get("/api/bead/--dangerous-flag")
    assert resp.status_code == 400


def test_get_bead_detail_endpoint_uses_cache_on_repeat_lookup(monkeypatch):
    calls = []

    async def fake_fetch(beads_env, bd_bin, actor, bead_id, timeout=20.0, beads_dir=""):
        calls.append(bead_id)
        return {"id": bead_id, "title": "fake bead", "status": "open"}

    monkeypatch.setattr(main_mod, "fetch_bead_detail", fake_fetch)
    main_mod._bead_detail_cache.clear()
    client = TestClient(main_mod.app)
    client.get("/api/bead/test-endpoint-cached-1")
    client.get("/api/bead/test-endpoint-cached-1")
    assert calls == ["test-endpoint-cached-1"]  # second lookup served from cache
