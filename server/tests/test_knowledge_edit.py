"""Phase 4: native BookStack Edit/History links and the served-revision marker.

All payloads are SYNTHETIC (wiki.example.com). The marker tests use fixed
strings for times; none reads the clock.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from test_context import PROJECTS, MultiWiki, _client, _get, _wiki

from critdash import context as ctx
from critdash import infrastructure as infra

BASE = "https://wiki.example.com"
T1 = "2026-10-12T10:00:00.000000Z"
T2 = "2026-10-13T10:00:00.000000Z"
SERVED_AT = "2026-10-17T12:00:00Z"


# ---------------- link building ----------------

def test_valid_slugs_give_edit_and_history_links():
    links = infra.page_links(BASE, "ops-book", "restore-a")
    assert links == {
        "edit_url": f"{BASE}/books/ops-book/page/restore-a/edit",
        "history_url": f"{BASE}/books/ops-book/page/restore-a/revisions",
    }


def test_trailing_slash_on_the_base_url_is_dropped():
    assert infra.page_links(BASE + "/", "b", "p")["edit_url"] == f"{BASE}/books/b/page/p/edit"
    assert infra.page_links(BASE + "///", "b", "p")["history_url"] == f"{BASE}/books/b/page/p/revisions"


@pytest.mark.parametrize("book,slug", [
    (None, "p"), ("b", None), ("", "p"), ("b", ""), (None, None), (5, "p"), ("b", ["p"]),
])
def test_a_missing_slug_gives_no_link(book, slug):
    assert infra.page_links(BASE, book, slug) == {"edit_url": None, "history_url": None}


HOSTILE = ["../x", "a/../b", "javascript:alert(1)", 'a"b', "a'b", "a b", "a<b>", "a?x=1", "a#f", "a%2f",
           "-lead", "UPPER", "a" * 192, "a\nb", "a\\b", "//evil.example.net"]


@pytest.mark.parametrize("bad", HOSTILE)
def test_a_hostile_slug_gives_no_link_in_either_position(bad):
    none = {"edit_url": None, "history_url": None}
    assert infra.page_links(BASE, "b", bad) == none
    assert infra.page_links(BASE, bad, "p") == none


@pytest.mark.parametrize("tail", ["?x=1", "/?x=1", "#frag", "/#frag", "?", "#"])
def test_a_base_url_with_a_query_or_fragment_gives_no_link(tail):
    assert infra.page_links(BASE + tail, "b", "p") == {"edit_url": None, "history_url": None}


def test_the_longest_allowed_slug_still_links():
    assert infra.page_links(BASE, "b", "a" * 191)["edit_url"] is not None


# ---------------- marker: pure ----------------

def _page_ref(pid, updated=T1):
    return {"id": pid, "updated_at": updated}


def test_never_served_page_is_pending():
    assert ctx.ServedMarker().status(10, T1) == {"state": "pending", "served_at": None, "project": None}


def test_served_page_with_the_same_revision_is_served():
    m = ctx.ServedMarker()
    m.record([_page_ref(10)], "proj-a", SERVED_AT)
    assert m.status(10, T1) == {"state": "served", "served_at": SERVED_AT, "project": "proj-a"}


def test_a_page_edited_after_serving_is_pending():
    m = ctx.ServedMarker()
    m.record([_page_ref(10, T1)], "proj-a", SERVED_AT)
    assert m.status(10, T2)["state"] == "pending"


def test_an_older_current_revision_counts_as_served():
    m = ctx.ServedMarker()
    m.record([_page_ref(10, T2)], "proj-a", SERVED_AT)
    assert m.status(10, T1)["state"] == "served"


@pytest.mark.parametrize("current", [None, "unknown", "", 5])
def test_an_unreadable_current_updated_at_is_pending(current):
    m = ctx.ServedMarker()
    m.record([_page_ref(10)], "proj-a", SERVED_AT)
    assert m.status(10, current)["state"] == "pending"


@pytest.mark.parametrize("bad", [{"id": 1, "updated_at": "unknown"}, {"id": 1}, {"id": "1", "updated_at": T1},
                                 {"id": True, "updated_at": T1}, {"id": None, "updated_at": T1}])
def test_a_page_without_an_id_or_a_real_time_is_not_recorded(bad):
    m = ctx.ServedMarker()
    m.record([bad], "proj-a", SERVED_AT)
    assert m.pages == {}


def test_the_marker_never_holds_more_than_500_pages_and_drops_the_oldest():
    m = ctx.ServedMarker()
    assert m.max_pages == 500
    m.record([_page_ref(i) for i in range(1, 502)], "proj-a", SERVED_AT)
    assert len(m.pages) == 500
    assert 1 not in m.pages and 2 in m.pages and 501 in m.pages


def test_serving_a_page_again_keeps_it_over_older_ones():
    m = ctx.ServedMarker(max_pages=3)
    m.record([_page_ref(1), _page_ref(2), _page_ref(3)], "proj-a", SERVED_AT)
    m.record([_page_ref(1, T2)], "proj-b", SERVED_AT)
    m.record([_page_ref(4)], "proj-a", SERVED_AT)
    assert list(m.pages) == [3, 1, 4]
    assert m.status(1, T2) == {"state": "served", "served_at": SERVED_AT, "project": "proj-b"}


NAIVE = "2026-10-12T10:00:00"
AWARE = "2026-10-12T10:00:00+00:00"
DATE_ONLY = "2026-10-12"


@pytest.mark.parametrize("recorded,current", [
    (AWARE, NAIVE), (NAIVE, AWARE), (NAIVE, NAIVE), (DATE_ONLY, AWARE), (AWARE, DATE_ONLY),
])
def test_mixed_naive_and_aware_times_never_raise_and_read_pending(recorded, current):
    m = ctx.ServedMarker()
    m.record([_page_ref(10, recorded)], "proj-a", SERVED_AT)
    assert m.status(10, current)["state"] == "pending"


def test_a_naive_or_date_only_time_is_not_recorded():
    m = ctx.ServedMarker()
    m.record([_page_ref(1, NAIVE), _page_ref(2, DATE_ONLY)], "proj-a", SERVED_AT)
    assert m.pages == {}


def test_an_offset_time_compares_by_instant():
    m = ctx.ServedMarker()
    m.record([_page_ref(10, "2026-10-12T12:00:00+02:00")], "proj-a", SERVED_AT)
    assert m.status(10, "2026-10-12T10:00:00Z")["state"] == "served"
    assert m.status(10, "2026-10-12T10:00:01Z")["state"] == "pending"


def test_a_naive_updated_at_from_the_wiki_gives_200_not_500(lc, lw):
    _get(lc)
    lw.pages[10]["updated_at"] = NAIVE
    resp = lc.get("/api/knowledge/page/10")
    assert resp.status_code == 200 and resp.json()["served"]["state"] == "pending"
    assert lc.get("/api/knowledge/search?q=a").status_code == 200


# ---------------- endpoints ----------------

class LinkWiki(MultiWiki):
    """MultiWiki plus the two routes the Knowledge view reads."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/books/") and path.rsplit("/", 1)[1].isdigit():
            bid = int(path.rsplit("/", 1)[1])
            book = next((b for b in self.books.values() if b["id"] == bid), None)
            return httpx.Response(200, json={"id": bid, "name": book["name"], "slug": book["slug"]}) \
                if book else httpx.Response(404, json={})
        if path == "/api/search":
            data = [{"id": p["id"], "type": "page", "name": p["name"], "slug": p["slug"],
                     "book": {"id": p["book_id"], "name": "x", "slug": p["book_slug"]},
                     "updated_at": p["updated_at"], "preview_html": {"content": "hit"}}
                    for p in self.pages.values() if "book_id" in p]
            return httpx.Response(200, json={"data": data, "total": len(data)})
        return super().__call__(request)


def _link_wiki():
    w = _wiki()
    w.__class__ = LinkWiki
    page = w.pages[10]
    page.update(book_id=4, book_slug=PROJECTS)
    return w


@pytest.fixture
def lw():
    return _link_wiki()


@pytest.fixture
def lc(tmp_path, monkeypatch, lw):
    c = _client(tmp_path, monkeypatch, lw, {"cache_ttl_s": 0})
    # the Knowledge routes need the knowledge module on: _client turned it on
    return c


def test_page_detail_has_edit_and_history_links(lc):
    body = lc.get("/api/knowledge/page/10").json()
    assert body["edit_url"] == f"{BASE}/books/{PROJECTS}/page/proj-a-entry/edit"
    assert body["history_url"] == f"{BASE}/books/{PROJECTS}/page/proj-a-entry/revisions"


def test_page_detail_with_a_hostile_slug_has_no_links(lc, lw):
    lw.pages[10]["slug"] = "../../admin"
    body = lc.get("/api/knowledge/page/10").json()
    assert body["ok"] is True and body["edit_url"] is None and body["history_url"] is None


def test_search_results_carry_links_and_a_hostile_one_is_skipped(lc, lw):
    row = lc.get("/api/knowledge/search?q=a").json()["results"][0]
    assert row["edit_url"].endswith("/page/proj-a-entry/edit") and row["history_url"].endswith("/revisions")
    lw.pages[10]["slug"] = "javascript:alert(1)"
    row = lc.get("/api/knowledge/search?q=a").json()["results"][0]
    assert row["edit_url"] is None and row["history_url"] is None


def _parse(slug):
    return infra.parse_record({"id": 1, "slug": slug, "name": "Svc", "markdown": "", "updated_at": T1},
                              base_url=BASE, book_slug="infra", now=datetime(2026, 10, 17, tzinfo=UTC),
                              stale_days=30)


def test_infrastructure_style_records_carry_links():
    rec = _parse("svc-a")
    assert rec["edit_url"] == f"{BASE}/books/infra/page/svc-a/edit"
    assert rec["history_url"] == f"{BASE}/books/infra/page/svc-a/revisions"
    bad = _parse("a b")
    assert bad["edit_url"] is None and bad["history_url"] is None


def _served(client):
    return client.app.state.served.pages


def test_context_200_records_the_pages_it_returned(lc):
    assert _served(lc) == {}
    assert _get(lc).status_code == 200
    pages = _served(lc)
    assert set(pages) == {10, 20, 22, 30, 31}
    assert pages[10][0] == T1 and pages[10][2] == "proj-a"


@pytest.mark.parametrize("project", ["", "Bad Slug", "../x"])
def test_a_refused_project_records_nothing(lc, project):
    assert _get(lc, project=project).status_code == 400
    assert _served(lc) == {}


def test_a_refused_host_records_nothing(lc):
    resp = lc.get("/api/context", params={"project": "proj-a"}, headers={"host": "evil.example.net"})
    assert resp.status_code == 403
    assert _served(lc) == {}


def test_a_200_with_the_wiki_down_records_nothing(lc, lw):
    lw.mode = "down"
    resp = _get(lc)
    assert resp.status_code == 200 and "knowledge_unavailable" in resp.json()["degraded"]
    assert _served(lc) == {}


def test_an_over_cap_stub_records_nothing(lc, monkeypatch):
    monkeypatch.setattr(ctx, "MIN_MAX_BYTES", 1)
    monkeypatch.setattr(ctx, "DEFAULT_MAX_BYTES", 50)
    lc.app.state.served.pages.clear()
    resp = lc.get("/api/context", params={"project": "proj-a"})
    assert resp.json()["state"] == "over_cap"
    assert _served(lc) == {}


def test_a_section_dropped_for_size_is_not_recorded(tmp_path, monkeypatch, lw):
    size = len(_get(_client(tmp_path, monkeypatch, lw, {"cache_ttl_s": 0})).content)
    monkeypatch.setattr(ctx, "MIN_MAX_BYTES", 1)
    tight = _client(tmp_path, monkeypatch, lw, {"cache_ttl_s": 0, "max_bytes": size - 1})
    body = _get(tight).json()
    assert "dropped:runbooks" in body["degraded"] and "dropped:standards" not in body["degraded"]
    assert set(_served(tight)) == {10, 30, 31}


def test_page_detail_is_pending_until_served_then_pending_again_after_an_edit(lc, lw):
    assert lc.get("/api/knowledge/page/10").json()["served"]["state"] == "pending"
    _get(lc)
    served = lc.get("/api/knowledge/page/10").json()["served"]
    assert served["state"] == "served" and served["project"] == "proj-a" and served["served_at"]
    lw.pages[10]["updated_at"] = T2
    assert lc.get("/api/knowledge/page/10").json()["served"]["state"] == "pending"


def test_search_results_show_the_marker(lc):
    assert lc.get("/api/knowledge/search?q=a").json()["results"][0]["served"]["state"] == "pending"
    _get(lc)
    assert lc.get("/api/knowledge/search?q=a").json()["results"][0]["served"]["state"] == "served"


def test_marker_is_per_process_and_starts_empty(tmp_path, monkeypatch, lw):
    a = _client(tmp_path, monkeypatch, lw, {"cache_ttl_s": 0})
    _get(a)
    b = _client(tmp_path, monkeypatch, lw, {"cache_ttl_s": 0})
    assert _served(a) and _served(b) == {}


def test_a_refused_request_gets_no_marker(lc):
    resp = lc.get("/api/knowledge/page/10", headers={"host": "evil.example.net"})
    assert resp.status_code == 403 and "served" not in resp.text
