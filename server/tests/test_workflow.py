"""Workflow module: intended policy, observed runtime, assignments, availability.

Every payload is SYNTHETIC (wiki.example.com, host-a, example-org/repo). The
pure-function tests pass a fixed clock; none reads the time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from test_infrastructure import FakeWiki, _page
from test_knowledge import BASE, TOKEN, _enabled, _make_client

from critdash import workflow as wf

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
SLUG = "workflow-policy-projections"


def _agent(**over):
    a = {"id": "s1", "kind": "claude", "model": "claude-opus-5", "host": "host-a", "status": "working",
         "title": "role:orchestrator example-org/repo", "repo": "repo", "bead": "ex-1", "subagents_active": 2,
         "last_activity": "2026-10-17T11:59:00Z", "cost_today_usd": 1.23}
    a.update(over)
    return a


def _bead(**over):
    b = {"id": "ex-1", "title": "Do the thing", "status": "in_progress", "assignee": "actor-a",
         "labels": ["waiting-review"]}
    b.update(over)
    return b


# ---------------- observed: separate fields ----------------

def test_role_runner_model_provider_account_host_are_separate_fields():
    [s] = wf.observed_sessions([_agent()])
    assert (s["role"], s["runner"], s["model"], s["provider"], s["host"]) == (
        "orchestrator", "claude", "claude-opus-5", "anthropic", "host-a")
    assert s["account"] == "unknown" and s["coordinated"] == "unknown"


@pytest.mark.parametrize("missing,expect", [
    ("title", {"role": "unknown"}),
    ("kind", {"runner": "unknown"}),
    ("model", {"model": "unknown", "provider": "unknown"}),
    ("host", {"host": "unknown"}),
])
def test_one_missing_field_does_not_collapse_the_others(missing, expect):
    full = wf.observed_sessions([_agent()])[0]
    gone = wf.observed_sessions([_agent(**{missing: None})])[0]
    for key, value in expect.items():
        assert gone[key] == value
    for key in ("role", "runner", "model", "provider", "host"):
        if key not in expect:
            assert gone[key] == full[key], key


def test_role_comes_only_from_an_explicit_title_marker():
    assert wf.observed_sessions([_agent(title="role:reviewer pass")])[0]["role"] == "reviewer"
    assert wf.observed_sessions([_agent(title="  Role: Coder repo-a")])[0]["role"] == "coder"
    for title in ("Reviewer pass", "Orchestrator for repo", "coder and reviewer", "fix the reviewer bug",
                  "role:boss", "x role:coder", "CritBoard", None):
        assert wf.observed_sessions([_agent(title=title)])[0]["role"] == "unknown", title


def test_provider_comes_only_from_the_model_id():
    assert wf.observed_sessions([_agent(model="someprovider/some-model")])[0]["provider"] == "someprovider"
    assert wf.observed_sessions([_agent(model="mystery-1")])[0]["provider"] == "unknown"


def test_cost_is_an_estimate_never_spend():
    [s] = wf.observed_sessions([_agent()])
    assert s["cost_estimate_today_usd"] == 1.23 and s["cost_basis"] == "estimate"
    assert not any("spend" in k or "remaining" in k for k in s)
    note = wf.build({"agents": []}, {"providers": []}, {}, wf.policy_unavailable("x", "y"), NOW)["observed"]["cost_note"]
    assert "estimate" in note and "not spend" in note


# ---------------- assignments: the review gate ----------------

def test_review_needed_with_no_reviewer_is_missing_not_fine():
    [a] = wf.assignments([_bead()], [])
    assert a["needs_review"] is True and a["review_gate"] == "missing"
    assert a["gate_missing"] == ["reviewer", "approver"]


def test_reviewer_is_never_invented_from_the_actor():
    [a] = wf.assignments([_bead(assignee="actor-a")], [])
    assert a["reviewers"] == [] and a["approvers"] == [] and a["coder"] == "actor-a"
    assert a["coder_basis"] == "assignee"


def test_reviewer_and_approver_labels_make_the_gate_named():
    [a] = wf.assignments([_bead(labels=["waiting-review", "reviewer:rev-a", "approver:app-a", "reviewed:abc1234"])], [])
    assert a["review_gate"] == "named" and a["gate_missing"] == []
    assert a["reviewers"] == ["rev-a"] and a["approvers"] == ["app-a"] and a["reviewed_refs"] == ["abc1234"]


def test_reviewer_without_approver_is_still_missing():
    [a] = wf.assignments([_bead(labels=["waiting-review", "reviewer:rev-a"])], [])
    assert a["review_gate"] == "missing" and a["gate_missing"] == ["approver"]


def test_the_coder_cannot_be_the_independent_reviewer():
    [a] = wf.assignments([_bead(labels=["waiting-review", "reviewer:Actor-A", "approver:app-a"])], [])
    assert a["review_gate"] == "missing" and a["gate_missing"] == ["independent_reviewer"]


def test_no_review_label_is_not_marked_as_passed():
    [a] = wf.assignments([_bead(labels=["bug"])], [])
    assert a["review_gate"] == "no_review_marked" and a["needs_review"] is False


def test_people_labels_alone_do_not_mark_a_bead_as_needing_review():
    [a] = wf.assignments([_bead(labels=["reviewer:rev-a"])], [])
    assert a["needs_review"] is False


def test_only_in_progress_beads_and_missing_gates_sort_first():
    items = wf.assignments([
        _bead(id="b", labels=["bug"]), _bead(id="a", labels=["waiting-review"]),
        _bead(id="c", status="open"), _bead(id="d", status="closed")], [])
    assert [i["id"] for i in items] == ["a", "b"]


def test_assignment_lists_the_sessions_bound_to_the_bead():
    [a] = wf.assignments([_bead()], [_agent(id="s9", bead="ex-1"), _agent(id="s8", bead="other")])
    assert a["sessions"] == ["s9"]


# ---------------- availability: basis and expiry on a fixed clock ----------------

def _entry(**over):
    e = {"provider": "prov-a", "ok": True, "source": "provider_api", "remaining": 5.0, "limit": 10.0,
         "used": 5.0, "unit": "usd", "period": "month", "resets_at": None, "error": None,
         "checked_at": "2026-10-17T11:45:00Z"}
    e.update(over)
    return e


def _avail(entries, owner=None, ttl=1800):
    return wf.availability({"providers": entries}, owner, ttl, NOW)["rows"]


def test_fresh_provider_reading_is_observed_with_timestamps():
    [r] = _avail([_entry()])
    assert r["basis"] == "provider-observed" and r["state"] == "observed" and r["expired"] is False
    assert r["observed_at"] == "2026-10-17T11:45:00Z" and r["expires_at"] == "2026-10-17T12:15:00Z"
    assert r["values"]["remaining"] == 5.0


def test_expired_reading_is_stale_and_drops_its_numbers():
    [r] = _avail([_entry(checked_at="2026-10-17T11:30:00Z")])  # expires exactly at NOW
    assert r["expired"] is True and r["state"] == "stale" and r["values"] is None
    assert r["observed_at"] == "2026-10-17T11:30:00Z" and r["expires_at"] == "2026-10-17T12:00:00Z"


def test_one_second_before_expiry_is_still_observed():
    [r] = _avail([_entry(checked_at="2026-10-17T11:30:01Z")])
    assert r["expired"] is False and r["state"] == "observed"


def test_ttl_is_configurable_and_bad_values_fall_back():
    assert wf.availability_ttl_s({"availability_ttl_s": 600}) == 600
    for bad in (True, "600", 5, None, -1, 1.5):
        assert wf.availability_ttl_s({"availability_ttl_s": bad}) == wf.DEFAULT_TTL_S
    assert wf.availability_ttl_s({"availability_ttl_s": 10**9}) == wf.MAX_TTL_S
    [r] = _avail([_entry(checked_at="2026-10-17T11:45:00Z")], ttl=600)
    assert r["expired"] is True


def test_failed_or_never_run_check_is_unknown_not_unavailable():
    rows = _avail([_entry(ok=False, source="provider_api", error="401 key rejected", remaining=None),
                   _entry(provider="prov-b", ok=None, source=None, checked_at=None),
                   _entry(provider="prov-c", ok=False, source=None, error="no endpoint")])
    assert [r["basis"] for r in rows] == ["unknown"] * 3
    assert [r["state"] for r in rows] == ["unknown"] * 3
    assert rows[0]["detail"] == "401 key rejected" and rows[1]["observed_at"] is None


def test_future_dated_reading_is_unknown():
    [r] = _avail([_entry(checked_at="2026-10-18T12:00:00Z")])
    assert r["basis"] == "unknown" and r["observed_at"] is None


def test_owner_report_has_its_own_basis_and_expires_too():
    rows = _avail([], owner=[
        {"provider": "prov-a", "state": "available", "observed_at": "2026-10-17T11:50:00Z", "note": "told by owner"},
        {"provider": "prov-b", "state": "available", "observed_at": "2026-10-17T09:00:00Z"},
        {"provider": "prov-c", "state": "available"},
        "junk"])
    assert [(r["provider"], r["basis"], r["state"]) for r in rows] == [
        ("prov-a", "owner-reported", "available"), ("prov-b", "owner-reported", "stale"),
        ("prov-c", "unknown", "unknown")]
    assert rows[0]["detail"] == "told by owner"


def test_every_row_has_a_basis_from_the_closed_set():
    rows = _avail([_entry(), _entry(ok=None)], owner=[{"provider": "p", "state": "up", "observed_at": "2026-10-17T11:59:00Z"}])
    assert {r["basis"] for r in rows} <= set(wf.BASES)


# ---------------- endpoint ----------------

SNAP_AGENTS = [_agent()]
SNAP_BEADS = [_bead()]


def _wf_modules(token_file, wfcfg=None, **bookstack):
    mods = _enabled(token_file, **bookstack)
    mods["workflow"] = {"enabled": True, **(wfcfg or {})}
    return mods


@pytest.fixture
def wiki():
    w = FakeWiki(n=2)
    w.book = {"id": 4, "name": "Workflow policy", "slug": SLUG}
    w.pages[1] = _page(id=1, slug="review-rule", name="Review rule")
    return w


def _load(client):
    snap = client.app.state.snap.snapshot
    snap["agents"] = SNAP_AGENTS
    snap["beads"]["items"] = SNAP_BEADS
    snap["generated_at"] = "2026-10-17T12:00:00Z"
    return client


@pytest.fixture
def wfc(tmp_path, monkeypatch, wiki):
    client, _ = _make_client(tmp_path, monkeypatch, _wf_modules)
    client.app.state.knowledge._transport = httpx.MockTransport(wiki)
    return _load(client)


def test_four_sections_stay_separate(wfc):
    body = wfc.get("/api/workflow").json()
    assert body["ok"] is True
    assert body["policy"]["label"] == "intended policy" and body["policy"]["state"] == "ok"
    assert [p["title"] for p in body["policy"]["pages"]] == ["host-2", "Review rule"]  # the wiki read sorts by kind, then title
    page = body["policy"]["pages"][1]
    assert page["source"]["repo"] == "example-org/repo" and page["source"]["git_sha"] == "0123abc"
    assert page["url"] == f"{BASE}/books/{SLUG}/page/review-rule"
    assert body["observed"]["sessions"][0]["runner"] == "claude"
    assert body["assignments"]["items"][0]["review_gate"] == "missing"
    assert "rows" in body["availability"]
    assert "sessions" not in body["policy"] and "pages" not in body["observed"]
    assert wfc.get("/api/workflow").headers["cache-control"] == "no-store"
    assert TOKEN.split(":")[1] not in json.dumps(body)


def test_policy_status_unknown_and_needs_verification_pass_through(wfc, wiki):
    wiki.pages[2]["markdown"] = "No provenance table here."
    wiki.pages[1]["markdown"] = wiki.pages[1]["markdown"].replace("| Status | current |", "| Status | needs-verification |")
    by = {p["title"]: p for p in wfc.get("/api/workflow").json()["policy"]["pages"]}
    assert by["host-2"]["status"] == "unknown" and by["host-2"]["source"]["repo"] == "unknown"
    assert by["Review rule"]["status"] == "needs-verification"


def test_policy_not_configured_while_observed_half_works(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, {"workflow": {"enabled": True}})
    _load(client)
    resp = client.get("/api/workflow", headers={"host": "localhost"})
    body = resp.json()
    assert resp.status_code == 200 and body["ok"] is True
    assert body["policy"]["state"] == "not_configured" and body["policy"]["pages"] == []
    assert body["observed"]["sessions"][0]["id"] == "s1"
    assert body["assignments"]["items"][0]["id"] == "ex-1"


def test_wiki_down_fails_only_the_policy_half(wfc, wiki):
    wiki.mode = "down"
    body = wfc.get("/api/workflow").json()
    assert body["ok"] is True and body["policy"]["state"] == "unreachable"
    assert body["observed"]["sessions"] and body["assignments"]["items"]


def test_missing_policy_book_is_reported_in_the_policy_half(wfc, wiki):
    wiki.book = None
    body = wfc.get("/api/workflow").json()
    assert body["policy"]["state"] == "book_not_found" and body["observed"]["sessions"]


def test_custom_policy_book_slug_is_read(tmp_path, monkeypatch, wiki):
    wiki.book = {"id": 9, "name": "Other", "slug": "other-book"}
    client, _ = _make_client(tmp_path, monkeypatch, lambda tf: _wf_modules(tf, {"policy_book_slug": "other-book"}))
    client.app.state.knowledge._transport = httpx.MockTransport(wiki)
    assert client.get("/api/workflow").json()["policy"]["book"]["slug"] == "other-book"


def test_expired_quota_reading_is_stale_through_the_endpoint(wfc):
    store = wfc.app.state.store
    store.set_quota_cache("openrouter", _entry(provider="openrouter", checked_at="2020-01-01T00:00:00Z"))
    store.set_quota_cache("kimi", _entry(provider="kimi", checked_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")))
    rows = {r["provider"]: r for r in wfc.get("/api/workflow").json()["availability"]["rows"]}
    assert rows["openrouter"]["state"] == "stale" and rows["openrouter"]["values"] is None
    assert rows["kimi"]["state"] == "observed" and rows["kimi"]["basis"] == "provider-observed"


def test_owner_reported_config_flows_through(tmp_path, monkeypatch, wiki):
    owner = [{"provider": "prov-z", "state": "available", "observed_at": "2020-01-01T00:00:00Z"}]
    client, _ = _make_client(tmp_path, monkeypatch, lambda tf: _wf_modules(tf, {"owner_reported": owner, "availability_ttl_s": 120}))
    client.app.state.knowledge._transport = httpx.MockTransport(wiki)
    av = client.get("/api/workflow").json()["availability"]
    row = next(r for r in av["rows"] if r["provider"] == "prov-z")
    assert av["ttl_s"] == 120 and row["basis"] == "owner-reported" and row["state"] == "stale"


# ---------------- gate ----------------

def test_off_by_default_answers_404(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, "default")
    assert client.get("/api/modules").json() == {"enabled": []}
    assert client.get("/api/workflow").status_code == 404


def test_other_modules_do_not_open_workflow(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, _enabled)
    assert client.get("/api/workflow").status_code == 404


def test_workflow_flag_alone_lists_the_module_and_keeps_knowledge_closed(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, {"workflow": {"enabled": True}})
    assert client.get("/api/modules").json() == {"enabled": ["workflow"]}
    assert client.get("/api/knowledge/status").status_code == 404


def test_cross_origin_request_is_refused(wfc):
    resp = wfc.get("/api/workflow", headers={"origin": "https://other.example.com"})
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "origin_not_allowed"
    assert resp.json()["ok"] is False


def test_foreign_host_is_refused(wfc):
    resp = wfc.get("/api/workflow", headers={"host": "rebind.example.com"})
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "host_not_allowed"


# ---------------- review round 2 ----------------

def _gate(labels, **bead):
    [a] = wf.assignments([_bead(labels=labels, **bead)], [])
    return a


def test_approver_equal_to_the_coder_is_self_approval():
    a = _gate(["waiting-review", "reviewer:rev-a", "approver:ACTOR-A"])
    assert a["review_gate"] == "missing" and a["gate_missing"] == ["independent_approver"]


def test_reviewer_and_approver_are_checked_against_label_coder_and_assignee():
    # coder label differs from the assignee: either name is a coder
    a = _gate(["waiting-review", "coder:coder-b", "reviewer:actor-a", "approver:app-a"])
    assert a["gate_missing"] == ["independent_reviewer"]
    a = _gate(["waiting-review", "coder:coder-b", "reviewer:rev-a", "approver:coder-b"])
    assert a["gate_missing"] == ["independent_approver"]
    a = _gate(["waiting-review", "coder:coder-b", "reviewer:rev-a", "approver:actor-a"])
    assert a["gate_missing"] == ["independent_approver"]


def test_unknown_coder_is_never_badged_named():
    a = _gate(["waiting-review", "reviewer:rev-a", "approver:app-a"], assignee=None)
    assert a["coder"] == "unknown" and a["review_gate"] == "missing" and a["gate_missing"] == ["coder_unknown"]
    assert wf.assignments([_bead(assignee="", labels=["waiting-review", "reviewer:r", "approver:a"])], [])[0]["review_gate"] == "missing"


def test_comma_separated_label_values_split_and_each_is_checked():
    a = _gate(["waiting-review", "reviewer:rev-a,rev-b", "approver:app-a, app-b"])
    assert a["reviewers"] == ["rev-a", "rev-b"] and a["approvers"] == ["app-a", "app-b"]
    assert a["review_gate"] == "named"
    a = _gate(["waiting-review", "reviewer:rev-a,actor-a", "approver:app-a"])
    assert a["gate_missing"] == ["independent_reviewer"]
    a = _gate(["waiting-review", "reviewer:rev-a", "approver:app-a,Actor-A"])
    assert a["gate_missing"] == ["independent_approver"]
    assert _gate(["waiting-review", "reviewer:,", "approver:app-a"])["gate_missing"] == ["reviewer"]


def test_provider_reading_needs_the_provider_api_source():
    [r] = _avail([_entry(ok=True, source="manual-entry")])
    assert r["basis"] == "unknown" and r["state"] == "unknown" and r["values"] is None


def test_owner_reports_are_capped():
    many = [{"provider": f"p{i}", "state": "up", "observed_at": "2026-10-17T11:59:00Z"}
            for i in range(60)]
    assert wf.MAX_OWNER_REPORTS == 50
    assert len(_avail([], owner=many)) == 50


# ---------------- review round 3 ----------------

def test_every_coder_label_value_counts_as_a_coder():
    a = _gate(["waiting-review", "coder:cb,cc", "reviewer:cc", "approver:app-a"])
    assert a["review_gate"] == "missing" and a["gate_missing"] == ["independent_reviewer"]
    a = _gate(["waiting-review", "coder:cb", "coder:cc", "reviewer:rev-a", "approver:CC"])
    assert a["gate_missing"] == ["independent_approver"]
    a = _gate(["waiting-review", "coder:cb", "coder:cc", "reviewer:cb", "approver:app-a"])
    assert a["gate_missing"] == ["independent_reviewer"]
    ok = _gate(["waiting-review", "coder:cb,cc", "reviewer:rev-a", "approver:app-a"])
    assert ok["coder"] == "cb, cc" and ok["coder_basis"] == "label"


def test_assignee_is_stripped_and_blank_means_no_coder():
    a = _gate(["waiting-review", "reviewer:actor-a", "approver:app-a"], assignee=" actor-a ")
    assert a["gate_missing"] == ["independent_reviewer"] and a["coder"] == "actor-a" and a["assignee"] == "actor-a"
    for blank in ("   ", "\t", ""):
        a = _gate(["waiting-review", "reviewer:rev-a", "approver:app-a"], assignee=blank)
        assert a["gate_missing"] == ["coder_unknown"] and a["coder"] == "unknown" and a["assignee"] == "unknown"
        assert a["coder_basis"] == "none"


def test_role_marker_must_end_at_whitespace_or_end_of_title():
    assert wf.observed_sessions([_agent(title="role:coder")])[0]["role"] == "coder"
    assert wf.observed_sessions([_agent(title="role:coder\trepo")])[0]["role"] == "coder"
    for title in ("role:coder-x", "role:reviewer_2", "role:worker.v2", "role:coders"):
        assert wf.observed_sessions([_agent(title=title)])[0]["role"] == "unknown", title


# ---------------- review round 4: bounds fail closed ----------------

def _names(n, prefix="coder"):
    return ",".join(f"{prefix}{i:03d}" for i in range(n))


def test_long_coder_list_with_the_reviewer_in_the_tail_fails_closed():
    a = _gate(["waiting-review", f"coder:{_names(30)}", "reviewer:coder029", "approver:app-a"])
    assert a["review_gate"] == "missing" and "too_many_names" in a["gate_missing"]


def test_long_reviewer_and_approver_lists_fail_closed():
    a = _gate(["waiting-review", f"reviewer:{_names(30, 'rev')}", "approver:app-a"])
    assert a["review_gate"] == "missing" and a["gate_missing"] == ["too_many_names"]
    a = _gate(["waiting-review", "reviewer:rev-a", f"approver:{_names(30, 'app')}"])
    assert a["review_gate"] == "missing" and a["gate_missing"] == ["too_many_names"]


def test_names_are_split_before_any_cap_and_the_limit_is_exact():
    ok = _gate(["waiting-review", f"coder:{_names(wf.MAX_NAMES, 'c')}", "reviewer:rev-a", "approver:app-a"])
    assert wf.MAX_NAMES == 20 and ok["review_gate"] == "named"
    assert len(ok["coder"].split(", ")) == 20
    over = _gate(["waiting-review", f"coder:{_names(21, 'c')}", "reviewer:rev-a", "approver:app-a"])
    assert over["gate_missing"] == ["too_many_names"]
    # a long value with its names well inside the limit is still read in full
    spread = _gate(["waiting-review", "coder:" + ",".join("n" * 40 + str(i) for i in range(10)),
                    "reviewer:" + "n" * 40 + "9", "approver:app-a"])
    assert spread["gate_missing"] == ["independent_reviewer"]


def test_a_name_over_the_length_cap_fails_closed():
    a = _gate(["waiting-review", "coder:" + "x" * 300, "reviewer:rev-a", "approver:app-a"])
    assert a["review_gate"] == "missing" and a["gate_missing"] == ["too_many_names"]


def test_reviewed_ref_overflow_is_cut_without_changing_the_gate():
    # SPEC assignment bounds / INSTALL 9c: a ref is evidence, not a person (#8)
    base = ["waiting-review", "coder:cod-a", "reviewer:rev-a", "approver:app-a"]
    over = _gate(base + [f"reviewed:{_names(21, 'ref')}"])
    assert over["review_gate"] == "named" and over["gate_missing"] == []
    assert len(over["reviewed_refs"]) == wf.MAX_NAMES
    long_ref = _gate(base + ["reviewed:" + "r" * 300])
    assert long_ref["review_gate"] == "named" and long_ref["gate_missing"] == []
    assert long_ref["reviewed_refs"] == []


def test_overflow_does_not_mark_a_bead_that_needs_no_review():
    a = _gate(["bug", f"coder:{_names(30)}"])
    assert a["review_gate"] == "no_review_marked" and a["gate_missing"] == []


def test_name_length_boundary_is_200_kept_201_fails_closed():
    assert wf.MAX_NAME_CHARS == 200
    kept = _gate(["waiting-review", "coder:" + "x" * 200, "reviewer:rev-a", "approver:app-a"])
    assert kept["review_gate"] == "named" and kept["coder"] == "x" * 200
    cut = _gate(["waiting-review", "coder:" + "x" * 201, "reviewer:rev-a", "approver:app-a"])
    assert cut["review_gate"] == "missing" and cut["gate_missing"] == ["too_many_names"]


def test_name_cap_is_cumulative_across_repeated_labels():
    def labels(k, per):
        return ["waiting-review", *(f"coder:{_names(per, f'c{j}_')}" for j in range(k)),
                "reviewer:rev-a", "approver:app-a"]
    ok = _gate(labels(2, 10))
    assert ok["review_gate"] == "named" and len(ok["coder"].split(", ")) == 20
    over = _gate(labels(3, 7))
    assert over["review_gate"] == "missing" and over["gate_missing"] == ["too_many_names"]


def test_oversized_raw_label_sets_overflow_before_any_split():
    cap = wf.MAX_LABEL_CHARS
    # duplicates collapse after the split, so only a raw-size guard can see this one
    a = _gate(["waiting-review", "coder:" + "a," * cap, "reviewer:rev-a", "approver:app-a"])
    assert a["review_gate"] == "missing" and "too_many_names" in a["gate_missing"]
    a = _gate(["waiting-review", "coder:cod-a", "reviewer:" + "a," * cap, "approver:app-a"])
    assert a["review_gate"] == "missing" and "too_many_names" in a["gate_missing"]


def test_raw_label_exactly_at_the_cap_is_still_read():
    cap = wf.MAX_LABEL_CHARS
    value = "a," + " " * (cap - 3) + "b"
    assert len(value) == cap
    a = _gate(["waiting-review", f"coder:{value}", "reviewer:rev-a", "approver:app-a"])
    assert a["review_gate"] == "named" and a["coder"] == "a, b"


def test_oversized_raw_label_is_cut_before_the_split():
    value = "a," + " " * wf.MAX_LABEL_CHARS + ",tail-name"
    a = _gate(["waiting-review", f"coder:{value}", "reviewer:rev-a", "approver:app-a"])
    assert a["coder"] == "a" and a["gate_missing"] == ["too_many_names"]
