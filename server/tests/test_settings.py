"""GET /api/settings/suggest's pure logic: the human_labels heuristic and
timezone detection. See main.py's endpoint and AGENTS.md briefing for the
full contract; provider bucket classification is tested in test_quota.py."""

from __future__ import annotations

from critdash.settings import detect_timezone, suggest_human_labels


def _bead(status, labels, repo=None):
    return {"status": status, "labels": labels, "repo": repo}


def test_person_label_qualifies_project_label_and_route_label_excluded():
    beads_items = [
        _bead("open", ["bryan", "critdash"], repo="critdash"),
        _bead("open", ["bryan"], repo="otherproj"),
        _bead("in_progress", ["bryan", "owner"], repo="critdash"),
        _bead("closed", ["bryan"], repo="critdash"),  # closed -- excluded from the count
    ]
    dispatch_routes = [{"label": "critdash"}, {"label": "otherproj"}]

    result = suggest_human_labels(beads_items, dispatch_routes)

    # "critdash"/"otherproj" fail (b) (they're route labels) and (c) (they're
    # repo values). "bryan" appears on 3 OPEN beads (the closed one doesn't
    # count), matches no route, and is never a repo value -- qualifies.
    # "owner" appears on 1 open bead, also qualifies, ranked below "bryan".
    assert result["detected"] == ["bryan", "owner"]
    assert "3 open beads" in result["reason"]


def test_label_that_is_a_bead_repo_value_never_qualifies():
    beads_items = [
        _bead("open", ["someproject"], repo="someproject"),
        _bead("open", ["someproject"], repo="someproject"),
    ]
    result = suggest_human_labels(beads_items, dispatch_routes=[])
    assert result["detected"] == []


def test_label_matching_a_dispatch_route_never_qualifies():
    beads_items = [_bead("open", ["fleetbot"]), _bead("open", ["fleetbot"])]
    dispatch_routes = [{"label": "fleetbot", "kind": "claude"}]
    result = suggest_human_labels(beads_items, dispatch_routes)
    assert result["detected"] == []


def test_no_qualifying_label_returns_empty_list_not_a_guess():
    result = suggest_human_labels([], [])
    assert result["detected"] == []
    assert "no label found" in result["reason"]


def test_ranked_by_open_bead_count_capped_at_three():
    beads_items = (
        [_bead("open", ["alice"]) for _ in range(5)]
        + [_bead("open", ["bob"]) for _ in range(3)]
        + [_bead("open", ["carol"]) for _ in range(2)]
        + [_bead("open", ["dave"]) for _ in range(1)]
    )
    result = suggest_human_labels(beads_items, [])
    assert result["detected"] == ["alice", "bob", "carol"]


def test_detect_timezone_never_raises_and_returns_a_known_source():
    detected, source = detect_timezone()
    assert isinstance(detected, str) and detected
    assert source in ("env", "system", "default")
