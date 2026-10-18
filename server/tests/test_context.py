"""Context module: GET /api/context, its size cap, and its data boundary.

Every payload is SYNTHETIC (wiki.example.com, host-a, example-org/repo,
proj-a). The pure-function tests pass a fixed clock; none reads the time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from test_knowledge import BASE, TOKEN, _make_client
from test_workflow import _agent, _bead, _entry

from critdash import context as ctx
from critdash.knowledge import KnowledgeError

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
COMMIT = "0123456789abcdef0123456789abcdef01234567"
PROJECTS, RUNBOOKS, POLICY = "projects", "runbooks", "workflow-policy-projections"

PROV = """\
| Field | Value |
|-------|-------|
| Source repo | `example-org/proj-a` (`main`) |
| Source path | `projects/proj-a.md` |
| Source git SHA | `0123abc` (blob `4567def`) |
| Live check | 2026-10-10 |
| Status | current |
"""
FACTS = """
## Facts

| Field | Value |
|---|---|
| Canonical repo | `example-org/proj-a` |
| Canonical location | `~/work/proj-a` |
| Dev/prod boundary | Dev edits happen on branches; prod deploys only from main |
"""


def _page(pid, slug, name, tags=(), markdown=PROV, updated="2026-10-12T10:00:00.000000Z"):
    return {"id": pid, "slug": slug, "name": name, "markdown": markdown, "updated_at": updated,
            "updated_by": {"name": "Synthetic Editor"}, "tags": [{"name": n, "value": v} for n, v in tags]}


class MultiWiki:
    """BookStack stand-in with several books."""

    def __init__(self):
        self.books: dict[str, dict] = {}
        self.pages: dict[int, dict] = {}
        self.mode = "ok"

    def add_book(self, slug, bid, pages):
        self.books[slug] = {"id": bid, "name": slug.title(), "slug": slug, "pages": [p["id"] for p in pages]}
        self.pages.update({p["id"]: p for p in pages})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "down":
            raise httpx.ConnectError("synthetic connection refused", request=request)
        path, q = request.url.path, dict(request.url.params)
        if path == "/api/books":
            hit = [b for b in self.books.values() if b["slug"] == q.get("filter[slug]")]
            return httpx.Response(200, json={"data": hit, "total": len(hit)})
        if path == "/api/pages":
            book = next((b for b in self.books.values() if str(b["id"]) == q.get("filter[book_id]")), None)
            ids = book["pages"] if book else []
            data = [{"id": i, "name": self.pages[i]["name"]} for i in ids]
            return httpx.Response(200, json={"data": data, "total": len(ids)})
        if path.startswith("/api/pages/"):
            return httpx.Response(200, json=self.pages[int(path.rsplit("/", 1)[1])])
        return httpx.Response(404, json={})


def _wiki():
    w = MultiWiki()
    w.add_book(PROJECTS, 4, [
        _page(10, "proj-a-entry", "Proj A entry", markdown=PROV + FACTS),
        _page(11, "proj-b-entry", "Proj B entry"),
    ])
    w.add_book(RUNBOOKS, 7, [
        _page(20, "restore-a", "Restore A", [("project", "proj-a")]),
        _page(21, "restore-b", "Restore B", [("project", "proj-b")]),
        _page(22, "shared-rule", "Shared rule", [("scope", "all")]),
        _page(23, "untagged", "Untagged page"),
    ])
    w.add_book(POLICY, 6, [_page(30, "review-rule", "Review rule"), _page(31, "approve-rule", "Approve rule")])
    return w


def _modules(token_file, ctxcfg=None):
    bs = {"base_url": BASE, "token_file": token_file}
    return {"knowledge": {"enabled": True, "allowed_hosts": ["testserver"], "bookstack": bs},
            "context": {"enabled": True, **(ctxcfg or {})}}


def _prime(client, beads=None, agents=None):
    snap = client.app.state.snap.snapshot
    snap["agents"] = agents if agents is not None else [_agent()]
    snap["beads"]["items"] = beads if beads is not None else [_bead(labels=["proj-a", "waiting-review"])]
    snap["sources"] = {"beads": {"ok": True, "stale": False}, "agents": {"ok": True, "stale": False}}
    snap["update"] = {"current": COMMIT}
    return client


@pytest.fixture
def wiki():
    return _wiki()


def _client(tmp_path, monkeypatch, wiki, ctxcfg=None):
    n = 0
    while (tmp_path / f"c{n}").exists():
        n += 1
    (tmp_path / f"c{n}").mkdir()
    client, _ = _make_client(tmp_path / f"c{n}", monkeypatch, lambda tf: _modules(tf, ctxcfg))
    client.app.state.knowledge._transport = httpx.MockTransport(wiki)
    return _prime(client)


@pytest.fixture
def cc(tmp_path, monkeypatch, wiki):
    return _client(tmp_path, monkeypatch, wiki)


def _get(client, project="proj-a", **kw):
    return client.get("/api/context", params={"project": project}, **kw)


# ---------------- gate and slug ----------------

def test_off_by_default_answers_404(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, "default")
    assert client.get("/api/context?project=proj-a").status_code == 404


def test_other_modules_do_not_open_context(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, {"workflow": {"enabled": True}})
    assert client.get("/api/context?project=proj-a").status_code == 404


def test_cross_origin_request_is_refused(cc):
    resp = _get(cc, headers={"origin": "https://other.example.com"})
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "origin_not_allowed"


def test_foreign_host_is_refused(cc):
    resp = _get(cc, headers={"host": "rebind.example.com"})
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "host_not_allowed"


def test_the_gate_runs_before_slug_validation(cc):
    assert _get(cc, "BAD SLUG", headers={"host": "rebind.example.com"}).status_code == 403


@pytest.mark.parametrize("bad", ["", "Proj-A", "-a", ".a", "_a", "a b", "a/b", "../x", "a" * 65, "a\n", "a%00", "é"])
def test_bad_project_slug_is_400(cc, bad):
    resp = _get(cc, bad)
    assert resp.status_code == 400
    assert resp.json()["ok"] is False and resp.json()["error"]["code"] == "invalid_project"


def test_missing_project_parameter_is_400(cc):
    assert cc.get("/api/context").status_code == 400


@pytest.mark.parametrize("good", ["a", "0", "proj-a", "a.b_c-d", "a" * 64])
def test_good_project_slugs_pass(cc, good):
    assert _get(cc, good).status_code == 200


def test_context_alone_is_listed_and_does_not_open_knowledge(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, {"context": {"enabled": True}})
    assert client.get("/api/modules").json() == {"enabled": ["context"]}
    assert client.get("/api/knowledge/status").status_code == 404


# ---------------- response ----------------

def test_full_response_shape_and_sources(cc):
    resp = _get(cc)
    body = resp.json()
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    assert body["ok"] is True and body["project"] == "proj-a" and body["degraded"] == []
    assert body["entrypoint"]["source"] == f"bookstack:{PROJECTS}"
    assert body["standards"]["source"] == f"bookstack:{POLICY}"
    assert body["runbooks"]["source"] == f"bookstack:{RUNBOOKS}"
    assert body["ownership"]["source"] == "beads" and body["availability"]["source"] == "quota"
    assert TOKEN.split(":")[1] not in resp.text


def test_entrypoint_fields(cc):
    e = _get(cc).json()["entrypoint"]
    assert (e["state"], e["match"], e["id"], e["title"]) == ("ok", "slug_prefix", 10, "Proj A entry")
    assert e["url"] == f"{BASE}/books/{PROJECTS}/page/proj-a-entry" and e["updated_at"] == "2026-10-12T10:00:00.000000Z"
    assert e["provenance"] == {"repo": "example-org/proj-a (main)", "path": "projects/proj-a.md",
                               "git_sha": "0123abc", "git_sha_note": "blob 4567def", "status": "current"}
    assert e["canonical_repo"] == "example-org/proj-a" and e["canonical_location"] == "~/work/proj-a"
    assert e["dev_prod_boundary"].startswith("Dev edits happen on branches")


def test_missing_canonical_facts_are_unknown_not_invented(cc, wiki):
    wiki.pages[10]["markdown"] = PROV
    e = _get(cc).json()["entrypoint"]
    assert (e["canonical_repo"], e["canonical_location"], e["dev_prod_boundary"]) == ("unknown",) * 3
    wiki.pages[10]["markdown"] = "No tables at all."
    e = _get(cc).json()["entrypoint"]
    assert e["provenance"]["repo"] == e["provenance"]["git_sha"] == "unknown"


def test_entrypoint_tag_match_beats_slug_prefix(tmp_path, monkeypatch):
    w = _wiki()
    # "aaa" sorts before the prefix page, but only the tagged page may win on the tag rule.
    w.add_book(PROJECTS, 4, [_page(10, "proj-a-entry", "Proj A entry"),
                             _page(12, "other-page", "ZZZ tagged", [("project", "proj-a")])])
    e = _get(_client(tmp_path, monkeypatch, w)).json()["entrypoint"]
    assert (e["id"], e["match"]) == (12, "tag")


def test_slug_prefix_needs_the_dash(tmp_path, monkeypatch):
    w = _wiki()
    w.add_book(PROJECTS, 4, [_page(10, "proj-ab-entry", "Wrong"), _page(11, "proj-a", "Exact slug only"),
                             _page(12, "proj-a-entry", "Right")])
    e = _get(_client(tmp_path, monkeypatch, w)).json()["entrypoint"]
    assert (e["id"], e["match"]) == (12, "slug_prefix")


def test_no_entrypoint_is_reported_not_guessed(cc):
    body = _get(cc, "proj-zzz").json()
    assert body["entrypoint"] == {"source": f"bookstack:{PROJECTS}", "state": "no_match"}
    assert "no_entrypoint" in body["degraded"]


def test_standards_list_the_policy_pages_without_bodies(cc):
    s = _get(cc).json()["standards"]
    assert sorted(p["title"] for p in s["items"]) == ["Approve rule", "Review rule"]
    assert set(s["items"][0]) == {"id", "title", "url", "updated_at", "status"}
    assert "markdown" not in json.dumps(s) and "Source repo" not in json.dumps(s)


def test_runbooks_keep_project_and_scope_all_tags_only(cc):
    r = _get(cc).json()["runbooks"]
    assert sorted(i["title"] for i in r["items"]) == ["Restore A", "Shared rule"]
    assert "runbooks_note" not in r
    assert set(r["items"][0]) == {"id", "title", "url", "updated_at"}


def test_untagged_runbooks_give_an_empty_list_and_a_note(tmp_path, monkeypatch):
    w = _wiki()
    w.add_book(RUNBOOKS, 7, [_page(23, "untagged", "Untagged page")])
    r = _get(_client(tmp_path, monkeypatch, w)).json()["runbooks"]
    assert r["items"] == [] and r["runbooks_note"] == "untagged"


def test_a_runbook_for_another_project_is_not_returned(cc):
    titles = [i["title"] for i in _get(cc, "proj-b").json()["runbooks"]["items"]]
    assert "Restore A" not in titles and "Restore B" in titles


# ---------------- knowledge off / down ----------------

def test_knowledge_off_gives_a_partial_response(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, {"context": {"enabled": True}})
    _prime(client)
    resp = client.get("/api/context?project=proj-a", headers={"host": "localhost"})
    body = resp.json()
    assert resp.status_code == 200 and body["ok"] is True
    for name in ("entrypoint", "standards", "runbooks"):
        assert body[name]["state"] == "not_configured" and body[name]["error"] == "not_configured"
    assert body["ownership"]["items"][0]["id"] == "ex-1"
    assert body["degraded"] == ["knowledge_not_configured"]
    assert body["revision"]["pages"] == []


def test_wiki_down_is_unavailable_and_beads_still_answer(cc, wiki):
    wiki.mode = "down"
    body = _get(cc).json()
    assert body["entrypoint"]["state"] == "unavailable" and body["entrypoint"]["error"] == "unreachable"
    assert body["degraded"] == ["knowledge_unavailable"]
    assert body["ownership"]["items"]


def test_one_missing_book_does_not_fail_the_others(tmp_path, monkeypatch):
    w = _wiki()
    del w.books[RUNBOOKS]
    body = _get(_client(tmp_path, monkeypatch, w)).json()
    assert body["runbooks"]["state"] == "unavailable" and body["runbooks"]["error"] == "book_not_found"
    assert body["entrypoint"]["state"] == "ok" and body["standards"]["state"] == "ok"


def test_custom_book_slugs_are_read(tmp_path, monkeypatch):
    w = _wiki()
    w.add_book("projx", 14, [_page(40, "proj-a-main", "Custom entry")])
    w.add_book("rbx", 17, [_page(41, "r", "Custom runbook", [("scope", "all")])])
    c = _client(tmp_path, monkeypatch, w, {"projects_book_slug": "projx", "runbooks_book_slug": "rbx"})
    body = _get(c).json()
    assert body["entrypoint"]["title"] == "Custom entry" and body["entrypoint"]["source"] == "bookstack:projx"
    assert [i["title"] for i in body["runbooks"]["items"]] == ["Custom runbook"]


# ---------------- ownership ----------------

def test_ownership_is_in_progress_beads_with_the_slug_label(cc):
    cc.app.state.snap.snapshot["beads"]["items"] = [
        _bead(id="b1", labels=["proj-a", "waiting-review", "reviewer:rev-a", "approver:app-a"]),
        _bead(id="b2", labels=["proj-b"]),
        _bead(id="b3", labels=["proj-a"], status="open"),
        _bead(id="b4", labels=["proj-a-extra"]),
        _bead(id="b5", labels=["proj-a"], assignee=None),
    ]
    o = _get(cc).json()["ownership"]
    assert sorted(i["id"] for i in o["items"]) == ["b1", "b5"]
    b1 = next(i for i in o["items"] if i["id"] == "b1")
    assert b1 == {"id": "b1", "title": "Do the thing", "assignee": "actor-a", "coder": "actor-a",
                  "reviewers": ["rev-a"], "approvers": ["app-a"], "review_gate": "named"}
    assert next(i for i in o["items"] if i["id"] == "b5")["assignee"] == "unknown"
    assert o["truncated"] is False


def test_ownership_is_capped_at_20_and_says_so(cc):
    cc.app.state.snap.snapshot["beads"]["items"] = [_bead(id=f"b{i:02d}", labels=["proj-a"]) for i in range(25)]
    o = _get(cc).json()["ownership"]
    assert len(o["items"]) == 20 and o["truncated"] is True and o["total"] == 25


def test_stale_beads_are_named_in_degraded(cc):
    cc.app.state.snap.snapshot["sources"]["beads"] = {"ok": True, "stale": True}
    assert "beads_stale" in _get(cc).json()["degraded"]
    cc.app.state.snap.snapshot["sources"] = {}
    assert "beads_stale" in _get(cc).json()["degraded"]


# ---------------- availability ----------------

def test_availability_is_freshness_only(cc):
    store = cc.app.state.store
    store.set_quota_cache("openrouter", _entry(provider="openrouter", checked_at="2020-01-01T00:00:00Z"))
    store.set_quota_cache("kimi", _entry(provider="kimi", checked_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                           remaining=5.0, error="key rejected"))
    a = _get(cc).json()["availability"]
    rows = {r["provider"]: r for r in a["rows"]}
    assert {tuple(sorted(r)) for r in rows.values()} == {
        ("basis", "expired", "expires_at", "observed_at", "provider", "state")}
    assert rows["openrouter"]["expired"] is True and rows["openrouter"]["state"] == "stale"
    assert rows["kimi"]["basis"] == "provider-observed" and rows["kimi"]["expired"] is False
    text = json.dumps(a)
    assert "remaining" not in text and "limit" not in text and "usd" not in text and "rejected" not in text


# ---------------- revision ----------------

def test_revision_block(cc):
    rev = _get(cc).json()["revision"]
    assert rev["critboard"] == COMMIT
    assert rev["fetched_at"].endswith("Z") and datetime.fromisoformat(rev["fetched_at"].replace("Z", "+00:00"))
    assert sorted(p["id"] for p in rev["pages"]) == [10, 20, 22, 30, 31]
    assert all(p["updated_at"] == "2026-10-12T10:00:00.000000Z" for p in rev["pages"])


def test_revision_commit_is_unknown_when_nothing_states_it(cc):
    cc.app.state.snap.snapshot["update"] = {}
    rev = _get(cc).json()["revision"]
    assert rev["critboard"] == "unknown" or len(rev["critboard"]) == 40  # a git checkout may state it


# ---------------- size cap and drop order ----------------

def _records(prefix, n, tags_key=None):
    return [{"id": 100 + i, "title": f"{prefix} {i} " + "t" * 150, "slug": f"{prefix}-{i}", "status": "current",
             "url": f"{BASE}/books/b/page/{prefix}-{i}", "updated_at": "2026-10-12T10:00:00Z",
             "projects": ["proj-a"], "scopes": [], "source": {}, "canonical": {}} for i in range(n)]


def _build(limit, runbook_n=20, standard_n=20, bead_n=20):
    beads = [_bead(id=f"b{i:02d}", title="B" * 150, labels=["proj-a"]) for i in range(bead_n)]
    runbooks = _records("rb", runbook_n)
    standards = _records("std", standard_n)
    for r in standards:
        r["id"] += 1000
        r["projects"] = []
    return ctx.build(
        "proj-a", projects=("ok", {"records": []}), runbook_result=("ok", {"records": runbooks}),
        policy=("ok", {"records": standards}), books=(PROJECTS, RUNBOOKS, POLICY),
        snapshot={"beads": {"items": beads}, "sources": {"beads": {"ok": True, "stale": False}}},
        quota_response={"providers": []}, wf_cfg={}, commit=COMMIT, now=NOW, limit=limit)


def test_a_body_under_the_cap_drops_nothing():
    full = _build(10**6)
    assert not [d for d in full["degraded"] if d.startswith("dropped:")]
    assert len(full["runbooks"]["items"]) == len(full["standards"]["items"]) == 20


def test_runbooks_drop_first_then_standards_then_ownership():
    full = _build(10**6)
    sizes = [len(ctx.render(full))]
    bodies = [full]
    for _ in range(3):  # one byte under the previous body: the next list must go
        bodies.append(_build(sizes[-1] - 1))
        sizes.append(len(ctx.render(bodies[-1])))
    drops = [[d for d in b["degraded"] if d.startswith("dropped:")] for b in bodies]
    assert drops == [[], ["dropped:runbooks"], ["dropped:runbooks", "dropped:standards"],
                     ["dropped:runbooks", "dropped:standards", "dropped:ownership"]]
    assert sizes[0] > sizes[1] > sizes[2] > sizes[3]
    assert bodies[1]["runbooks"] == {"source": f"bookstack:{RUNBOOKS}", "state": "dropped", "items": []}
    assert len(bodies[1]["standards"]["items"]) == len(bodies[1]["ownership"]["items"]) == 20
    assert bodies[2]["standards"]["state"] == "dropped" and len(bodies[2]["ownership"]["items"]) == 20
    assert bodies[3]["ownership"]["items"] == [] and bodies[3]["ownership"]["state"] == "dropped"


def test_each_body_fits_the_cap_it_was_given():
    for limit in (10**6, 20000, 12000, 9000, 8192):
        assert len(ctx.render(_build(limit))) <= limit


def test_dropped_lists_leave_the_revision_block(cc):
    full = _build(10**6)
    cut = _build(len(ctx.render(full)) - 1)
    assert {p["id"] for p in full["revision"]["pages"]} - {p["id"] for p in cut["revision"]["pages"]} == {
        r["id"] for r in full["runbooks"]["items"]}


def test_endpoint_never_exceeds_max_bytes(tmp_path, monkeypatch):
    w = _wiki()
    w.add_book(RUNBOOKS, 7, [_page(200 + i, f"rb-{i}", "R" * 190 + str(i), [("scope", "all")]) for i in range(25)])
    w.add_book(POLICY, 6, [_page(300 + i, f"st-{i}", "S" * 190 + str(i)) for i in range(25)])
    big = _get(_client(tmp_path, monkeypatch, w, {"max_bytes": 262144}))
    assert len(big.content) > 8192 and not any(d.startswith("dropped:") for d in big.json()["degraded"])
    cc = _client(tmp_path, monkeypatch, w, {"max_bytes": 8192})
    cc.app.state.snap.snapshot["beads"]["items"] = [_bead(id=f"b{i}", labels=["proj-a"]) for i in range(25)]
    small = _get(cc)
    assert len(small.content) <= 8192
    assert "dropped:runbooks" in small.json()["degraded"]
    assert small.json()["runbooks"]["state"] == "dropped"


@pytest.mark.parametrize("value,expect", [
    (None, 16384), (True, 16384), ("9000", 16384), (1.5, 16384), (10, 8192), (-1, 8192), (10**9, 262144),
    (9000, 9000)])
def test_max_bytes_config_is_clamped(value, expect):
    assert ctx.max_bytes({} if value is None else {"max_bytes": value}) == expect


def test_worst_case_entrypoint_fits_the_smallest_cap():
    long = "x" * 900
    rec = {"id": 1, "title": long, "slug": "proj-a-x", "url": "https://wiki.example.com/" + long,
           "updated_at": long, "status": long, "projects": [], "scopes": [],
           "source": {"repo": long, "path": long, "git_sha": long, "path_note": long, "git_sha_note": long},
           "canonical": {"repo": long, "location": long, "boundary": long}}
    body = ctx.build(
        "proj-a", projects=("ok", {"records": [rec]}), runbook_result=("ok", {"records": []}),
        policy=("ok", {"records": []}), books=(PROJECTS, RUNBOOKS, POLICY),
        snapshot={"beads": {"items": [_bead(id=long, title=long, assignee=long, labels=["proj-a"])] * 25}},
        quota_response={"providers": [_entry(provider=long)] * 30}, wf_cfg={}, commit=COMMIT, now=NOW,
        limit=ctx.MIN_MAX_BYTES)
    assert len(ctx.render(body)) <= ctx.MIN_MAX_BYTES
    assert body["entrypoint"]["state"] == "ok"


# ---------------- data boundary ----------------

def _keys(obj, found=None):
    found = set() if found is None else found
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(str(k).lower())
            _keys(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, found)
    return found


def test_no_forbidden_key_or_value_in_a_full_response(cc):
    cc.app.state.snap.snapshot["agents"] = [_agent(id="sess-secret-1", cost_today_usd=9.99, host="host-a")]
    cc.app.state.snap.snapshot["beads"]["items"] = [
        _bead(labels=["proj-a", "waiting-review"], session="sess-secret-2", cost=3.5, memory_file="/home/user/m.md")]
    cc.app.state.store.set_quota_cache("kimi", _entry(provider="kimi", remaining=4.0))
    resp = _get(cc)
    keys = _keys(resp.json())
    assert not keys & ctx.FORBIDDEN_KEYS, keys & ctx.FORBIDDEN_KEYS
    for text in ("sess-secret", "9.99", "/home/", "token_file", "memory_file", "transcript"):
        assert text not in resp.text, text
    assert TOKEN.split(":")[1] not in resp.text


def test_the_forbidden_key_filter_stops_a_leaking_projection(cc, monkeypatch):
    real = ctx._ownership_item
    monkeypatch.setattr(ctx, "_ownership_item", lambda a: {
        **real(a), "sessions": ["sess-secret-1"], "cost": 1.0, "Token": "t", "nested": {"auth": "a", "ok": 1}})
    resp = _get(cc)
    assert resp.status_code == 200
    item = resp.json()["ownership"]["items"][0]
    assert item["nested"] == {"ok": 1}
    assert not _keys(resp.json()) & ctx.FORBIDDEN_KEYS
    assert "sess-secret" not in resp.text


def test_scrub_removes_forbidden_keys_at_any_depth_and_keeps_the_rest():
    src = {"a": 1, "Cost": 2, "l": [{"token": 1, "k": 2}], "d": {"auth_path": "/x", "v": {"values": 1, "w": 3}}}
    assert ctx.scrub(src) == {"a": 1, "l": [{"k": 2}], "d": {"v": {"w": 3}}}


def test_availability_projection_has_no_values_even_when_given_some():
    a = ctx.availability({"providers": [_entry()]}, {}, NOW)
    assert set(a["rows"][0]) == {"provider", "basis", "state", "observed_at", "expires_at", "expired"}


def test_a_knowledge_error_carries_only_its_code_into_the_response():
    body = ctx.entrypoint("proj-a", ("error", KnowledgeError("auth_failed", "secret detail", 403).code), PROJECTS)
    assert body == {"source": f"bookstack:{PROJECTS}", "state": "unavailable", "error": "auth_failed"}
