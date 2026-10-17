"""Infrastructure module: record parser, BookStack book reader, endpoint gate.

Every wiki payload below is SYNTHETIC (wiki.example.com, host-a,
example-org/repo). The parser tests pass a fixed clock; none reads the time.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from test_knowledge import BASE, TOKEN, _enabled, _make_client

from critdash import knowledge as knowledge_mod
from critdash.infrastructure import (
    compute_status,
    parse_provenance_table,
    parse_record,
    parse_tags,
    parse_when,
)

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)

TABLE = """\
# Provenance

| Field | Value |
|-------|-------|
| Source repo | `example-org/repo` |
| Source path | `hosts/host-a.md` |
| Source git SHA | `0123abc` |
| Live check | 2026-10-01 |
| Class | host |
| Status | current |

Body text.
"""


def _page(**over):
    page = {
        "id": 11, "book_id": 3, "slug": "host-a", "name": "host-a", "markdown": TABLE,
        "updated_at": "2026-10-02T03:04:05.000000Z", "updated_by": {"id": 2, "name": "Synthetic Editor"},
        "tags": [
            {"name": "kind", "value": "host"}, {"name": "owner", "value": "team-a"},
            {"name": "environment", "value": "prod"}, {"name": "purpose", "value": "runs service-x"},
            {"name": "runbook", "value": "https://wiki.example.com/books/infrastructure/page/host-a-runbook"},
            {"name": "runbook", "value": "/books/infrastructure/page/restore"},
            {"name": "backup", "value": "nightly snapshot"},
            {"name": "related", "value": "service-x"}, {"name": "related", "value": "example-org/repo"},
        ],
    }
    page.update(over)
    return page


def _record(**over):
    return parse_record(_page(**over), base_url=BASE, book_slug="infrastructure", now=NOW, stale_days=90)


# ---------------- provenance table ----------------

def test_table_present_gives_every_field():
    assert parse_provenance_table(TABLE) == {
        "source_repo": "example-org/repo", "source_path": "hosts/host-a.md", "source_git_sha": "0123abc",
        "live_check": "2026-10-01", "class": "host", "status": "current",
    }


def test_table_absent_is_all_unknown():
    out = parse_provenance_table("# Just prose\n\nNo table here.\n")
    assert set(out.values()) == {"unknown"} and len(out) == 6


def test_empty_markdown_and_none_are_all_unknown():
    assert set(parse_provenance_table("").values()) == {"unknown"}
    assert set(parse_provenance_table(None).values()) == {"unknown"}


def test_malformed_table_keeps_good_rows_and_unknowns_the_rest():
    md = "| Field | Value |\n| --- | --- |\n| Source repo | example-org/repo |\n| lonely cell\n| Status | \n| Class |  TBD |\n"
    out = parse_provenance_table(md)
    assert out["source_repo"] == "example-org/repo"
    assert out["status"] == "unknown" and out["class"] == "unknown" and out["live_check"] == "unknown"


def test_a_different_first_table_is_not_the_provenance_table():
    md = "| Name | Size |\n|---|---|\n| a | 1 |\n\n| Field | Value |\n|---|---|\n| Status | current |\n"
    assert parse_provenance_table(md)["status"] == "unknown"


def test_field_names_ignore_case_and_markup_and_underscores_in_values_survive():
    md = "| Field | Value |\n|---|---|\n| **source repo** | example-org/host_a_repo |\n"
    assert parse_provenance_table(md)["source_repo"] == "example-org/host_a_repo"


# ---------------- tags ----------------

def test_tags_repeat_and_scalars():
    tags = parse_tags(_page()["tags"])
    assert tags["kind"] == "host" and tags["owner"] == "team-a" and tags["environment"] == "prod"
    assert tags["runbooks"] == [
        "https://wiki.example.com/books/infrastructure/page/host-a-runbook", "/books/infrastructure/page/restore",
    ]
    assert tags["backups"] == ["nightly snapshot"]
    assert tags["related"] == ["service-x", "example-org/repo"]


def test_missing_tags_are_unknown_and_lists_empty():
    tags = parse_tags([{"name": "owner", "value": ""}, {"name": "kind"}, "junk", None])
    assert tags["kind"] == tags["owner"] == tags["environment"] == tags["purpose"] == "unknown"
    assert tags["runbooks"] == tags["backups"] == tags["related"] == []


def test_duplicate_repeats_collapse_and_scalar_first_wins():
    tags = parse_tags([
        {"name": "Kind", "value": "host"}, {"name": "kind", "value": "service"},
        {"name": "related", "value": "x"}, {"name": "RELATED", "value": "x"},
    ])
    assert tags["kind"] == "host" and tags["related"] == ["x"]


# ---------------- time and stale math (fixed clock) ----------------

def test_parse_when_forms():
    assert parse_when("2026-10-01", NOW) == datetime(2026, 10, 1, tzinfo=UTC)
    assert parse_when("2026-10-01T10:00:00Z", NOW) == datetime(2026, 10, 1, 10, tzinfo=UTC)
    assert parse_when("2026-10-01 (checked by hand)", NOW) == datetime(2026, 10, 1, tzinfo=UTC)
    for bad in ("never", "", None, "2026-02-30", "10/01/2026", "2027-06-01"):
        assert parse_when(bad, NOW) is None, bad


def _status(declared="current", live="2026-10-01", updated="2026-10-02T00:00:00Z", days=90):
    return compute_status(declared, live, updated, NOW, days)


def test_fresh_live_check_current_stays_current():
    st = _status()
    assert st["status"] == "current" and st["verified_basis"] == "live_check"
    assert st["last_verified"] == "2026-10-01T00:00:00Z"


def test_live_check_exactly_at_the_limit_is_not_stale_one_day_over_is():
    at_limit = _status(live="2026-07-19T12:00:00Z")  # NOW - 90 days exactly
    over = _status(live="2026-07-19T11:59:59Z")      # one second older
    assert at_limit["status"] == "current"
    assert over["status"] == "stale" and "older than 90 days" in over["status_reason"]


def test_stale_threshold_follows_the_configured_days():
    assert _status(live="2026-10-10", days=10)["status"] == "current"
    assert _status(live="2026-10-01", days=10)["status"] == "stale"


def test_stale_counts_from_live_check_not_from_a_fresh_page_edit():
    st = _status(live="2026-01-01", updated="2026-10-16T00:00:00Z")
    assert st["status"] == "stale" and st["verified_basis"] == "live_check"


def test_no_live_check_falls_back_to_updated_at_for_age():
    old = _status(declared="needs-verification", live="unknown", updated="2026-01-01T00:00:00Z")
    assert old["status"] == "stale" and old["verified_basis"] == "page_updated"
    assert old["last_verified"] == "unknown"  # an edit is not a verification
    fresh = _status(declared="needs-verification", live="unknown", updated="2026-10-10T00:00:00Z")
    assert fresh["status"] == "needs-verification"


def test_current_without_a_readable_live_check_is_not_shown_as_current():
    for live in ("unknown", "never", "soon"):
        st = _status(live=live)
        assert st["status"] == "needs-verification", live
        assert "no readable Live check" in st["status_reason"]
        assert st["last_verified"] == "unknown"


def test_contradicted_is_never_overwritten_by_age():
    assert _status(declared="contradicted", live="2020-01-01")["status"] == "contradicted"


def test_stale_overrides_unknown_and_needs_verification_when_old():
    assert _status(declared="unknown", live="2025-01-01")["status"] == "stale"
    assert _status(declared="needs verification", live="2025-01-01")["status"] == "stale"


def test_unrecognised_or_missing_status_is_unknown_and_no_dates_stay_unknown():
    st = compute_status("sparkly", "unknown", None, NOW, 90)
    assert st["status"] == "unknown" and st["verified_basis"] == "none" and st["age_from"] == "unknown"
    assert _status(declared="Needs_Verification")["status"] == "needs-verification"


# ---------------- whole record ----------------

def test_record_fields():
    r = _record()
    assert r["title"] == "host-a" and r["kind"] == "host" and r["status"] == "current"
    assert r["url"] == f"{BASE}/books/infrastructure/page/host-a"
    assert r["updated_by"] == "Synthetic Editor"
    assert r["source"]["repo"] == "example-org/repo" and r["source"]["repo_href"] is None
    assert r["runbooks"][0]["href"].startswith("https://wiki.example.com/")
    assert r["runbooks"][1] == {"text": "/books/infrastructure/page/restore",
                                "href": f"{BASE}/books/infrastructure/page/restore"}
    assert r["related"] == ["service-x", "example-org/repo"]


def test_bare_page_is_all_explicit_unknown():
    r = parse_record({"id": 1}, base_url=BASE, book_slug="infrastructure", now=NOW, stale_days=90)
    for key in ("kind", "owner", "environment", "purpose", "updated_at", "updated_by", "last_verified", "status"):
        assert r[key] == "unknown", key
    assert r["url"] is None and r["runbooks"] == [] and r["backups"] == [] and r["related"] == []
    assert set(r["source"][k] for k in ("repo", "path", "git_sha", "class", "live_check")) == {"unknown"}


def test_only_http_urls_and_wiki_paths_become_links():
    tags = [{"name": "runbook", "value": v} for v in (
        "javascript:alert(1)", "//evil.example.net/x", "data:text/html,x", "ftp://x.example.net/a", "plain words")]
    r = _record(tags=tags)
    assert [x["href"] for x in r["runbooks"]] == [None] * 5
    assert _record(markdown=TABLE.replace("example-org/repo", "https://git.example.com/example-org/repo"))[
        "source"]["repo_href"] == "https://git.example.com/example-org/repo"


def test_page_slug_is_quoted_in_the_url():
    assert _record(slug="a b/../c")["url"].endswith("/page/a%20b%2F..%2Fc")


# ---------------- adapter + endpoint ----------------

SYN_INFRA_BOOK = {"id": 3, "name": "Infrastructure", "slug": "infrastructure"}


class FakeWiki:
    """BookStack stand-in: one 'infrastructure' book of N pages."""

    def __init__(self, n=3):
        self.requests: list[httpx.Request] = []
        self.pages = {i: _page(id=i, slug=f"host-{i}", name=f"host-{i}") for i in range(1, n + 1)}
        self.mode = "ok"       # ok | down | 500
        self.book = SYN_INFRA_BOOK
        self.big_page = None
        self.gone: set[int] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.mode == "down":
            raise httpx.ConnectError("synthetic connection refused", request=request)
        if self.mode == "500":
            return httpx.Response(500, json={})
        path, q = request.url.path, dict(request.url.params)
        if path == "/api/books":
            hit = [self.book] if self.book and q.get("filter[slug]") == self.book["slug"] else []
            return httpx.Response(200, json={"data": hit, "total": len(hit)})
        if path == "/api/pages":
            ids = sorted(self.pages)
            data = [{"id": i, "name": self.pages[i]["name"]} for i in ids[: int(q.get("count", 100))]]
            return httpx.Response(200, json={"data": data, "total": len(ids)})
        if path.startswith("/api/pages/"):
            pid = int(path.rsplit("/", 1)[1])
            if pid in self.gone:
                return httpx.Response(404, json={})
            if self.big_page == pid:
                return httpx.Response(200, content=b'{"markdown": "' + b"x" * (knowledge_mod.MAX_PAGE_BYTES + 10) + b'"}')
            return httpx.Response(200, json=self.pages[pid])
        return httpx.Response(404, json={})

    def page_reads(self):
        return [r for r in self.requests if r.url.path.startswith("/api/pages/")]


def _infra_modules(token_file, infra=None, **bookstack):
    mods = _enabled(token_file, **bookstack)
    mods["infrastructure"] = {"enabled": True, **(infra or {})}
    return mods


@pytest.fixture
def wiki():
    return FakeWiki()


@pytest.fixture
def infra(tmp_path, monkeypatch, wiki):
    client, _ = _make_client(tmp_path, monkeypatch, _infra_modules)
    client.app.state.knowledge._transport = httpx.MockTransport(wiki)
    return client


def test_infrastructure_lists_records_with_status_and_no_secrets(infra, wiki):
    resp = infra.get("/api/infrastructure")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["truncated"] is False and body["total"] == 3
    assert [r["title"] for r in body["records"]] == ["host-1", "host-2", "host-3"]
    assert body["book"] == {"name": "Infrastructure", "slug": "infrastructure", "url": f"{BASE}/books/infrastructure"}
    assert body["stale_after_days"] == 90 and body["fetched_at"]
    assert body["status"]["ok"] is True and body["status"]["last_success"]
    assert "token" not in resp.text.lower() and TOKEN.split(":")[1] not in resp.text
    assert "Authorization" not in resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert all(r.headers["authorization"] == f"Token {TOKEN}" for r in wiki.requests)


def test_records_are_sorted_by_kind_then_title(infra, wiki):
    wiki.pages[1]["tags"] = [{"name": "kind", "value": "service"}]
    wiki.pages[2]["tags"] = [{"name": "kind", "value": "host"}]
    wiki.pages[3]["tags"] = []
    titles = [r["title"] for r in infra.get("/api/infrastructure").json()["records"]]
    assert titles == ["host-2", "host-1", "host-3"]  # host, service, unknown ("unknown" sorts last here)


def test_custom_book_slug_and_stale_after_days_are_used(tmp_path, monkeypatch, wiki):
    wiki.book = {"id": 9, "name": "Ops", "slug": "ops-book"}
    client, _ = _make_client(
        tmp_path, monkeypatch, lambda tf: _infra_modules(tf, {"book_slug": "ops-book", "stale_after_days": 5}))
    client.app.state.knowledge._transport = httpx.MockTransport(wiki)
    body = client.get("/api/infrastructure").json()
    assert body["ok"] is True and body["book"]["slug"] == "ops-book" and body["stale_after_days"] == 5
    assert all(r["status"] == "stale" for r in body["records"])  # Live check 2026-10-01 vs real clock, 5 days


def test_bad_stale_after_days_falls_back_to_90():
    from critdash.infrastructure import stale_after_days
    assert [stale_after_days({"stale_after_days": b}) for b in (0, -3, "30", True, None)] == [90] * 5
    assert stale_after_days({"stale_after_days": 30}) == 30 and stale_after_days({}) == 90


def test_book_not_found_is_a_setup_error_not_an_outage(infra, wiki):
    wiki.book = None
    resp = infra.get("/api/infrastructure")
    body = resp.json()
    assert resp.status_code == 503 and body["ok"] is False
    assert body["error"]["code"] == "book_not_found" and "infrastructure" in body["error"]["message"]
    assert body["status"]["down_since"] is None  # BookStack answered
    assert wiki.page_reads() == []


def test_page_cap_reads_at_most_50_pages_and_says_truncated(tmp_path, monkeypatch):
    wiki = FakeWiki(n=60)
    client, _ = _make_client(tmp_path, monkeypatch, _infra_modules)
    client.app.state.knowledge._transport = httpx.MockTransport(wiki)
    body = client.get("/api/infrastructure").json()
    assert len(body["records"]) == 50 and len(wiki.page_reads()) == 50
    assert body["truncated"] is True and body["total"] == 60


def test_page_deleted_between_list_and_read_is_skipped(infra, wiki):
    wiki.gone = {2}
    body = infra.get("/api/infrastructure").json()
    assert body["ok"] is True and [r["id"] for r in body["records"]] == [1, 3]


def test_oversize_page_fails_the_whole_read(infra, wiki):
    wiki.big_page = 2
    resp = infra.get("/api/infrastructure")
    assert resp.status_code == 502
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "upstream_too_large"
    assert "records" not in body


def test_outage_reports_down_since_and_last_success_and_no_records(infra, wiki):
    assert infra.get("/api/infrastructure").json()["ok"] is True
    wiki.mode = "down"
    resp = infra.get("/api/infrastructure")
    body = resp.json()
    assert resp.status_code == 502 and body["ok"] is False and "records" not in body
    assert body["error"]["code"] == "unreachable"
    st = body["status"]
    assert st["ok"] is False and st["down_since"] and st["last_success"] and st["error"]["code"] == "unreachable"
    wiki.mode = "ok"
    assert infra.get("/api/infrastructure").json()["status"]["down_since"] is None


def test_upstream_500_is_reported(infra, wiki):
    wiki.mode = "500"
    resp = infra.get("/api/infrastructure")
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "upstream_error"


def test_whole_read_shares_one_overall_deadline(tmp_path, monkeypatch):
    wiki = FakeWiki()

    async def slow(request):
        await asyncio.sleep(0.7)  # each call inside timeout_s, the chain far over 2x timeout_s
        return wiki(request)

    client, _ = _make_client(tmp_path, monkeypatch, lambda tf: _infra_modules(tf, timeout_s=1))
    client.app.state.knowledge._transport = httpx.MockTransport(slow)

    async def run():
        with pytest.raises(knowledge_mod.KnowledgeError) as ei:
            await asyncio.wait_for(client.app.state.knowledge.infrastructure(), 8)
        return ei.value.code
    assert asyncio.run(run()) == "unreachable"
    assert client.app.state.knowledge.status()["down_since"]


def test_stale_flows_through_the_endpoint(infra, wiki):
    wiki.pages[1]["markdown"] = TABLE.replace("2026-10-01", "2020-01-01")
    by_id = {r["id"]: r for r in infra.get("/api/infrastructure").json()["records"]}
    assert by_id[1]["status"] == "stale"


# ---------------- module flag, knowledge dependency, gate ----------------

def test_off_by_default_and_listed_in_nav_order_when_on(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, "default")
    assert client.get("/api/modules").json() == {"enabled": []}
    resp = client.get("/api/infrastructure")
    assert resp.status_code == 404 and resp.json() == {"detail": "Not Found"}


def test_infrastructure_flag_alone_does_not_open_knowledge_routes(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, {"infrastructure": {"enabled": True}})
    assert client.get("/api/modules").json() == {"enabled": ["infrastructure"]}
    assert client.get("/api/knowledge/status").status_code == 404


def test_knowledge_flag_alone_does_not_open_infrastructure(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, _enabled)
    assert client.get("/api/infrastructure").status_code == 404


@pytest.mark.parametrize("mods", [
    lambda tf: {"infrastructure": {"enabled": True}},
    lambda tf: {"infrastructure": {"enabled": True}, "knowledge": {"enabled": False, "bookstack": {"base_url": BASE, "token_file": tf}}},
    lambda tf: {"infrastructure": {"enabled": True}, "knowledge": {"enabled": True, "allowed_hosts": ["testserver"]}},
])
def test_knowledge_not_configured_reports_not_configured(tmp_path, monkeypatch, mods):
    client, _ = _make_client(tmp_path, monkeypatch, mods)
    resp = client.get("/api/infrastructure", headers={"Host": "localhost"})
    body = resp.json()
    assert resp.status_code == 503 and body["ok"] is False and body["error"]["code"] == "not_configured"
    assert "token" not in resp.text.lower() or "token_file" in body["error"]["message"]


def test_cross_origin_request_is_refused(infra, wiki):
    resp = infra.get("/api/infrastructure", headers={"Origin": "https://evil.example.net"})
    assert resp.status_code == 403
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "origin_not_allowed" and "status" in body
    assert wiki.requests == []


def test_foreign_host_is_refused(infra, wiki):
    resp = infra.get("/api/infrastructure", headers={"Host": "evil.example.net", "Origin": "http://evil.example.net"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "host_not_allowed"
    resp = infra.get("/api/infrastructure", headers={"Host": "evil.example.net"})
    assert resp.status_code == 403 and resp.json()["ok"] is False
    assert wiki.requests == []


def test_same_origin_request_is_allowed(infra):
    assert infra.get("/api/infrastructure", headers={"Origin": "http://testserver"}).status_code == 200


def test_example_config_ships_infrastructure_off_with_defaults():
    from critdash import config as config_mod
    example = json.loads((config_mod.DASHBOARD_ROOT / "config" / "sources.example.json").read_text())
    assert example["modules"]["infrastructure"] == {"enabled": False, "book_slug": "infrastructure", "stale_after_days": 90}
    assert config_mod.DEFAULT_SOURCES["modules"]["infrastructure"] == example["modules"]["infrastructure"]
