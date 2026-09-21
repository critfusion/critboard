import json

import pytest

from critdash.collectors import beads as beads_mod
from critdash.collectors.beads import (
    BeadsCollector,
    build_dependency_maps,
    guess_repo,
    is_review_lane,
    transform_item,
)


def load_fixture(fixtures_dir, name):
    with open(fixtures_dir / name) as f:
        return json.load(f)


def test_guess_repo_from_label():
    item = {"labels": ["demo-web"], "title": "whatever"}
    assert guess_repo(item) == "demo-web"


def test_guess_repo_skips_intent_labels():
    item = {"labels": ["needs-codex", "grok-review"], "title": "demo-lms-content: fix thing"}
    assert guess_repo(item) == "demo-lms-content"


def test_guess_repo_none_when_unresolvable():
    item = {"labels": [], "title": "just a plain sentence with no colon"}
    assert guess_repo(item) is None


def test_is_review_lane():
    assert is_review_lane({"labels": ["needs-review"]}) is True
    assert is_review_lane({"labels": ["needs-codex"]}) is False


def test_build_dependency_maps_direction():
    # issue jx11 depends on d81w (type "blocks") -> jx11 is blocked_by d81w,
    # and d81w blocks jx11.
    items = [
        {
            "id": "jx11",
            "dependencies": [
                {"issue_id": "jx11", "depends_on_id": "d81w", "type": "blocks"},
                {"issue_id": "jx11", "depends_on_id": "parent1", "type": "parent-child"},
            ],
        },
        {"id": "d81w", "dependencies": []},
    ]
    blocked_by, blocks = build_dependency_maps(items)
    assert blocked_by["jx11"] == ["d81w"]
    assert blocks["d81w"] == ["jx11"]
    # parent-child dependency must not show up as a blocker
    assert "parent1" not in blocked_by.get("jx11", [])


def test_transform_item_shape(fixtures_dir):
    items_raw = load_fixture(fixtures_dir, "bd_list.json")
    blocked_by, blocks = build_dependency_maps(items_raw)
    item = transform_item(items_raw[0], blocked_by, blocks)
    for key in (
        "id", "title", "status", "priority", "type", "assignee", "labels",
        "created_at", "updated_at", "closed_at", "age_s", "blocked_by",
        "blocks", "parent", "repo", "url",
    ):
        assert key in item
    assert item["type"] in ("task", "bug", "feature", "epic", "chore")
    assert isinstance(item["labels"], list)


@pytest.mark.asyncio
async def test_collect_end_to_end(fixtures_dir, monkeypatch):
    list_text = (fixtures_dir / "bd_list.json").read_text()
    stats_text = (fixtures_dir / "bd_stats.json").read_text()
    ready_text = (fixtures_dir / "bd_ready.json").read_text()

    async def fake_run(cmd, timeout=20.0):
        if " list " in cmd:
            return list_text
        if " stats " in cmd:
            return stats_text
        if " ready " in cmd:
            return ready_text
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(beads_mod, "_run", fake_run)

    collector = BeadsCollector(bd_bin="bd")
    result = await collector.collect()

    assert "beads" in result
    b = result["beads"]
    assert set(b["stats"].keys()) == {"open", "in_progress", "blocked", "closed_today", "ready"}
    assert isinstance(b["items"], list) and len(b["items"]) == len(json.loads(list_text))
    assert set(b["lanes"].keys()) == {"ready", "in_progress", "blocked", "review"}


@pytest.mark.asyncio
async def test_pagination_notice_after_json_is_stripped(fixtures_dir, monkeypatch):
    """bd ready --json can print a trailing plain-text pagination notice on
    stdout after the JSON array when truncated; the collector must not choke
    on it (regression test for a real bug hit while building this)."""
    list_text = (fixtures_dir / "bd_list.json").read_text()
    stats_text = (fixtures_dir / "bd_stats.json").read_text()
    ready_with_notice = (
        '[{"id": "x1", "status": "open", "title": "t"}]\n'
        "Showing 1 of 5 ready issues. Use --limit 0 for all.\n"
    )

    async def fake_run(cmd, timeout=20.0):
        if " list " in cmd:
            return list_text
        if " stats " in cmd:
            return stats_text
        if " ready " in cmd:
            return ready_with_notice
        raise AssertionError(cmd)

    monkeypatch.setattr(beads_mod, "_run", fake_run)
    collector = BeadsCollector(bd_bin="bd")
    result = await collector.collect()
    assert result["beads"]["lanes"]["ready"] == ["x1"]


def test_status_transition_events(tmp_store):
    collector = BeadsCollector(bd_bin="bd", store=tmp_store)
    item_v1 = {
        "id": "abc", "title": "t", "status": "open", "priority": 1, "type": "task",
        "assignee": None, "labels": [], "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z", "closed_at": None, "age_s": 10,
        "blocked_by": [], "blocks": [], "parent": None, "repo": None, "url": None,
    }
    collector._detect_transitions([item_v1])
    events = tmp_store.recent_events(10)
    assert any(e["kind"] == "bead_created" for e in events)

    # status change + a fresh claim in the same poll: bead_claimed is emitted
    # (the more specific, informative event) and a redundant bead_status is
    # suppressed since "claimed" already implies the open -> in_progress move.
    item_v2 = dict(item_v1, status="in_progress", assignee="localhost-claude")
    collector._detect_transitions([item_v2])
    events = tmp_store.recent_events(10)
    kinds = {e["kind"] for e in events}
    assert "bead_claimed" in kinds
    assert "bead_status" not in kinds

    # a pure status change with no assignee change does emit bead_status
    item_v2b = dict(item_v2, status="blocked")
    collector._detect_transitions([item_v2b])
    events = tmp_store.recent_events(10)
    assert any(e["kind"] == "bead_status" for e in events)

    item_v3 = dict(item_v2b, status="closed")
    collector._detect_transitions([item_v3])
    events = tmp_store.recent_events(10)
    assert any(e["kind"] == "bead_closed" for e in events)
