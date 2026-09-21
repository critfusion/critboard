"""GET /api/settings/suggest's pure logic: the human_labels heuristic and
timezone detection. See main.py's endpoint and AGENTS.md briefing for the
full contract; provider bucket classification is tested in test_quota.py."""

from __future__ import annotations

from critdash.settings import detect_timezone, suggest_human_labels


def _bead(status, labels, repo=None, assignee=None):
    return {"status": status, "labels": labels, "repo": repo, "assignee": assignee}


def _worktree(repo):
    return {"repo": repo}


def test_person_label_qualifies_route_and_worktree_labels_excluded():
    beads_items = [
        _bead("open", ["maintainer", "critdash"], repo="critdash"),
        _bead("open", ["maintainer"], repo="otherproj"),
        _bead("in_progress", ["maintainer", "owner"], repo="critdash"),
        _bead("closed", ["maintainer"], repo="critdash"),  # closed -- excluded from the count
    ]
    dispatch_routes = [{"label": "critdash"}]
    worktrees = [_worktree("otherproj")]

    result = suggest_human_labels(beads_items, dispatch_routes, worktrees)

    # "critdash" fails (b) (route label), "otherproj" fails (c) (real
    # worktree/repo name on disk). "maintainer" appears on 3 OPEN beads (the
    # closed one doesn't count), matches no route, matches no worktree name,
    # and is never claimed -- qualifies. "owner" appears on 1 open bead, also
    # qualifies, ranked below "maintainer".
    assert result["detected"] == ["maintainer", "owner"]
    assert "3 open beads" in result["reason"]


def test_person_label_that_is_circularly_a_bead_repo_value_still_detected():
    # This is the actual bug: collectors/beads.py:guess_repo derives a bead's
    # `repo` field FROM its own labels, so a person label that carries no
    # other project label gets guessed back as `repo` on every one of its own
    # beads. The old criterion (c) checked bead `repo` values directly, which
    # made this circular and eliminated the correct answer by construction.
    # The fix drops that check in favor of the filesystem-derived worktree
    # scan, so this must still qualify.
    beads_items = [
        _bead("open", ["maintainer"], repo="maintainer"),
        _bead("open", ["maintainer"], repo="maintainer"),
        _bead("open", ["maintainer"], repo="maintainer"),
    ]
    result = suggest_human_labels(beads_items, dispatch_routes=[], worktrees=[])
    assert result["detected"] == ["maintainer"]


def test_project_label_matching_a_real_worktree_name_excluded():
    beads_items = [
        _bead("open", ["someproject"], repo="someproject"),
        _bead("open", ["someproject"], repo="someproject"),
    ]
    worktrees = [_worktree("someproject")]
    result = suggest_human_labels(beads_items, dispatch_routes=[], worktrees=worktrees)
    assert result["detected"] == []


def test_worktree_name_match_is_case_insensitive():
    beads_items = [_bead("open", ["SomeProject"]), _bead("open", ["SomeProject"])]
    worktrees = [_worktree("someproject")]
    result = suggest_human_labels(beads_items, dispatch_routes=[], worktrees=worktrees)
    assert result["detected"] == []


def test_label_matching_a_dispatch_route_never_qualifies():
    beads_items = [_bead("open", ["fleetbot"]), _bead("open", ["fleetbot"])]
    dispatch_routes = [{"label": "fleetbot", "kind": "claude"}]
    result = suggest_human_labels(beads_items, dispatch_routes)
    assert result["detected"] == []


def test_label_ever_claimed_by_an_assignee_never_qualifies():
    # Project-labelled work routinely gets claimed (by a dispatch route's
    # agent, or by hand); a person label's beads stay unclaimed because
    # nothing wakes on it and nobody runs `bd claim` against their own marker
    # label. One claimed bead among many is enough to disqualify the label.
    beads_items = [
        _bead("open", ["waiting-review"], assignee=None),
        _bead("open", ["waiting-review"], assignee=None),
        _bead("open", ["waiting-review"], assignee="agent-someproject"),
    ]
    result = suggest_human_labels(beads_items, dispatch_routes=[], worktrees=[])
    assert result["detected"] == []


def test_rare_workflow_label_does_not_outrank_high_count_person_label():
    beads_items = (
        [_bead("open", ["maintainer"], assignee=None) for _ in range(44)]
        + [_bead("open", ["waiting-review"], assignee="agent-someproject") for _ in range(15)]
        + [_bead("open", ["demo-tenant"], assignee=None) for _ in range(2)]
        + [_bead("open", ["handoff"], assignee="worker-claude") for _ in range(1)]
    )
    result = suggest_human_labels(beads_items, dispatch_routes=[], worktrees=[])
    # "waiting-review" and "handoff" are claimed by an assignee -- excluded
    # despite outranking "demo-tenant" by count. "maintainer" wins on count among
    # what remains.
    assert result["detected"] == ["maintainer", "demo-tenant"]


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


def test_worktrees_defaults_to_empty_when_omitted():
    beads_items = [_bead("open", ["maintainer"]) for _ in range(2)]
    result = suggest_human_labels(beads_items, dispatch_routes=[])
    assert result["detected"] == ["maintainer"]


def test_detect_timezone_never_raises_and_returns_a_known_source():
    detected, source = detect_timezone()
    assert isinstance(detected, str) and detected
    assert source in ("env", "system", "default")
