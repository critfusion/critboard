"""Knowledge module: opt-in flag, read-only BookStack adapter, outage and
auth-failure reporting, token handling.

Every BookStack response below is SYNTHETIC: written for this test file, not
copied from any real wiki. wiki.example.com is a reserved example domain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from critdash import config as config_mod
from critdash import main as main_mod
from critdash import knowledge as knowledge_mod
from critdash.knowledge import html_to_text

BASE = "https://wiki.example.com"
TOKEN_ID = "synthetic-token-id"
TOKEN_SECRET = "synthetic-token-secret-9f3a"
TOKEN = f"{TOKEN_ID}:{TOKEN_SECRET}"

# ---- SYNTHETIC BookStack payloads ----
SYN_BOOK = {"id": 7, "name": "Synthetic Book", "slug": "synthetic-book"}
SYN_PAGE = {
    "id": 42, "book_id": 7, "slug": "synthetic-page", "name": "Synthetic Page",
    "html": "<h1>Heading</h1><p>First <b>line</b> &amp; more.</p>"
            "<script>alert('x')</script><ul><li>one</li><li>two</li></ul>",
    "updated_at": "2026-01-02T03:04:05.000000Z", "revision_count": 3,
    "created_by": {"id": 1, "name": "Synthetic Author"},
    "updated_by": {"id": 2, "name": "Synthetic Editor"},
    "tags": [{"name": "source", "value": "example-repo"}, {"name": "verified", "value": "2026-01-01"}],
}
SYN_SEARCH = {
    "total": 2,
    "data": [
        {"id": 42, "type": "page", "name": "Synthetic Page", "book": {"id": 7, "name": "Synthetic Book"},
         "updated_at": "2026-01-02T03:04:05.000000Z",
         "preview_html": {"name": "x", "content": "a <strong>match</strong> here"}},
        {"id": 8, "type": "chapter", "name": "Synthetic Chapter"},
    ],
}


class FakeBookStack:
    """Records every request; behavior switched by .mode."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.mode = "ok"  # ok | down | 401 | 403 | 500 | redirect | notfound
        self.page = dict(SYN_PAGE)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.mode == "down":
            raise httpx.ConnectError("synthetic connection refused", request=request)
        if self.mode in ("401", "403", "500"):
            return httpx.Response(int(self.mode), json={"error": {"message": "synthetic"}})
        if self.mode == "redirect":
            return httpx.Response(302, headers={"location": "https://other.example.net/steal"})
        path = request.url.path
        if path == "/api/books" and not path.rsplit("/", 1)[-1].isdigit():
            return httpx.Response(200, json={"data": [SYN_BOOK], "total": 1})
        if path == "/api/search":
            return httpx.Response(200, json=SYN_SEARCH)
        if path.startswith("/api/pages/"):
            if self.mode == "notfound":
                return httpx.Response(404, json={"error": {"message": "synthetic"}})
            return httpx.Response(200, json=self.page)
        if path == "/api/books/7":
            return httpx.Response(200, json=SYN_BOOK)
        return httpx.Response(404, json={})


def _make_client(tmp_path, monkeypatch, modules, token_mode=0o600, token_text=TOKEN, extra_sources=None):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    token_file = tmp_path / "bookstack.token"
    if token_text is not None:
        token_file.write_text(token_text + "\n")
        os.chmod(token_file, token_mode)
    sources = dict(config_mod.DEFAULT_SOURCES)
    sources["db_path"] = str(tmp_path / "test.db")
    sources.update(extra_sources or {})
    if modules == "default":
        sources.pop("modules", None)
    else:
        sources["modules"] = modules(str(token_file)) if callable(modules) else modules
    (config_dir / "sources.json").write_text(json.dumps(sources))
    (config_dir / "sources.example.json").write_text(json.dumps(config_mod.DEFAULT_SOURCES))
    (config_dir / "layout.json").write_text(json.dumps({
        "version": 1, "title": "t", "grid": {"columns": 12, "row_height": 80, "gap": 14},
        "panels": [{"id": "a", "type": "stat_row", "title": "A", "x": 0, "y": 0, "w": 4, "h": 4}],
    }))
    (config_dir / "theme.json").write_text(json.dumps({"name": "t", "colors": {"bg": "#000000"}}))
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    return TestClient(main_mod.build_app()), config_dir


def _enabled(token_file, **bookstack):
    bs = {"base_url": BASE, "token_file": token_file, **bookstack}
    # the in-process TestClient sends Host: testserver
    return {"knowledge": {"enabled": True, "allowed_hosts": ["testserver"], "bookstack": bs}}


@pytest.fixture
def fake():
    return FakeBookStack()


@pytest.fixture
def kb(tmp_path, monkeypatch, fake):
    client, _ = _make_client(tmp_path, monkeypatch, _enabled)
    client.app.state.knowledge._transport = httpx.MockTransport(fake)
    return client


# ---------------- module flag: off by default ----------------

@pytest.mark.parametrize("modules", [
    "default",
    {},
    {"knowledge": {"enabled": False}},
    {"knowledge": {"enabled": "true"}},  # a string is not the boolean true
    {"knowledge": {"enabled": 1}},
])
def test_disabled_module_has_no_endpoints_and_no_nav(tmp_path, monkeypatch, modules):
    client, _ = _make_client(tmp_path, monkeypatch, modules)
    assert client.get("/api/modules").json() == {"enabled": []}
    for url in ("/api/knowledge/status", "/api/knowledge/search?q=a", "/api/knowledge/page/42"):
        resp = client.get(url)
        assert resp.status_code == 404, url
        assert resp.json() == {"detail": "Not Found"}


def test_modules_endpoint_lists_enabled_in_nav_order(tmp_path, monkeypatch):
    modules = {
        "knowledge": {"enabled": True}, "workflow": {"enabled": True},
        "infrastructure": {"enabled": False}, "bogus": {"enabled": True},
    }
    client, _ = _make_client(tmp_path, monkeypatch, modules)
    assert client.get("/api/modules").json() == {"enabled": ["workflow", "knowledge"]}


def test_example_config_ships_every_module_disabled():
    example = json.loads((config_mod.DASHBOARD_ROOT / "config" / "sources.example.json").read_text())
    assert set(example["modules"]) == {"infrastructure", "workflow", "knowledge"}
    assert all(m["enabled"] is False for m in example["modules"].values())
    assert config_mod.DEFAULT_SOURCES["modules"]["knowledge"]["enabled"] is False


def test_module_removed_from_sources_json_turns_off_without_restart(tmp_path, monkeypatch, fake):
    client, config_dir = _make_client(tmp_path, monkeypatch, _enabled)
    client.app.state.knowledge._transport = httpx.MockTransport(fake)
    assert client.get("/api/modules").json() == {"enabled": ["knowledge"]}
    path = config_dir / "sources.json"
    doc = json.loads(path.read_text())
    doc.pop("modules")
    path.write_text(json.dumps(doc))
    assert client.get("/api/modules").json() == {"enabled": []}
    assert client.get("/api/knowledge/status").status_code == 404


# ---------------- happy path ----------------

def test_search_returns_pages_as_plain_text(kb, fake):
    resp = kb.get("/api/knowledge/search", params={"q": "synthetic"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["results"] == [{
        "id": 42, "name": "Synthetic Page", "book": "Synthetic Book",
        "updated_at": "2026-01-02T03:04:05.000000Z", "preview": "a match here",
    }]
    assert body["fetched_at"]
    assert resp.headers["cache-control"] == "no-store"
    sent = fake.requests[0]
    assert sent.headers["authorization"] == f"Token {TOKEN}"
    assert "synthetic" in sent.url.params["query"]


def test_blank_search_does_not_call_bookstack(kb, fake):
    body = kb.get("/api/knowledge/search", params={"q": "   "}).json()
    assert body["results"] == []
    assert fake.requests == []


def test_search_query_over_200_chars_is_rejected(kb):
    assert kb.get("/api/knowledge/search", params={"q": "a" * 201}).status_code == 422


def test_page_has_text_book_provenance_and_edit_link(kb):
    body = kb.get("/api/knowledge/page/42").json()
    assert body["ok"] is True
    assert body["name"] == "Synthetic Page"
    assert body["book"] == {"id": 7, "name": "Synthetic Book"}
    assert body["text"] == "Heading\nFirst line & more.\none\ntwo"
    assert "alert" not in body["text"]
    assert body["updated_at"] == "2026-01-02T03:04:05.000000Z"
    assert body["provenance"]["tags"] == [
        {"name": "source", "value": "example-repo"}, {"name": "verified", "value": "2026-01-01"},
    ]
    assert body["provenance"]["updated_by"] == "Synthetic Editor"
    assert body["edit_url"] == f"{BASE}/books/synthetic-book/page/synthetic-page/edit"
    assert body["open_url"] == f"{BASE}/link/42"


def test_page_with_no_tags_reports_empty_provenance(kb, fake):
    fake.page = {**SYN_PAGE, "tags": []}
    assert kb.get("/api/knowledge/page/42").json()["provenance"]["tags"] == []


def test_page_id_must_be_a_positive_integer(kb):
    assert kb.get("/api/knowledge/page/0").status_code == 404
    assert kb.get("/api/knowledge/page/abc").status_code == 422


def test_no_content_cache_a_changed_page_is_refetched(kb, fake):
    assert kb.get("/api/knowledge/page/42").json()["name"] == "Synthetic Page"
    fake.page = {**SYN_PAGE, "name": "Renamed Synthetic Page"}
    assert kb.get("/api/knowledge/page/42").json()["name"] == "Renamed Synthetic Page"


def test_html_to_text_drops_markup_and_script():
    assert html_to_text("<p>a</p><style>p{}</style><p>b<br>c</p>") == "a\nb\nc"
    assert html_to_text("") == ""


# ---------------- outage ----------------

def test_outage_reports_down_since_and_last_success_never_cached_data(kb, fake):
    ok = kb.get("/api/knowledge/page/42").json()
    last_success = ok["status"]["last_success"]
    assert last_success and ok["status"]["down_since"] is None

    fake.mode = "down"
    resp = kb.get("/api/knowledge/page/42")
    assert resp.status_code == 502
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "unreachable"
    assert body["status"]["ok"] is False
    assert body["status"]["down_since"]
    assert body["status"]["last_success"] == last_success
    # the earlier page content must not come back with the error
    assert "text" not in body and "name" not in body and "Synthetic" not in json.dumps(body)
    first_down = body["status"]["down_since"]

    again = kb.get("/api/knowledge/search", params={"q": "x"}).json()
    assert again["status"]["down_since"] == first_down  # the outage clock does not restart

    fake.mode = "ok"
    recovered = kb.get("/api/knowledge/status").json()
    assert recovered["ok"] is True
    assert recovered["status"]["down_since"] is None


def test_outage_before_any_success_has_no_last_success(kb, fake):
    fake.mode = "down"
    body = kb.get("/api/knowledge/status").json()
    assert body["status"]["last_success"] is None
    assert body["status"]["down_since"]


def test_real_connection_refused_is_unreachable(tmp_path, monkeypatch):
    def mods(token_file):
        return _enabled(token_file, base_url="http://127.0.0.1:1")
    client, _ = _make_client(tmp_path, monkeypatch, mods)
    resp = client.get("/api/knowledge/status")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "unreachable"


@pytest.mark.parametrize("code", ["401", "403"])
def test_auth_failure_is_reported_as_auth_failed(kb, fake, code):
    fake.mode = code
    resp = kb.get("/api/knowledge/search", params={"q": "x"})
    assert resp.status_code == 502
    err = resp.json()["error"]
    assert err["code"] == "auth_failed"
    assert err["http_status"] == int(code)
    assert code in err["message"]
    assert resp.json()["status"]["down_since"]


def test_upstream_500_is_reported(kb, fake):
    fake.mode = "500"
    resp = kb.get("/api/knowledge/status")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"


def test_missing_page_is_404_and_not_an_outage(kb, fake):
    fake.mode = "notfound"
    resp = kb.get("/api/knowledge/page/42")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert resp.json()["status"]["down_since"] is None


def test_redirects_are_not_followed_so_the_token_stays_put(kb, fake):
    fake.mode = "redirect"
    resp = kb.get("/api/knowledge/status")
    assert resp.status_code == 502
    assert len(fake.requests) == 1
    assert all(r.url.host == "wiki.example.com" for r in fake.requests)


# ---------------- token handling ----------------

def _every_failure(kb, fake):
    bodies = []
    for mode in ("ok", "down", "401", "403", "500", "redirect", "notfound"):
        fake.mode = mode
        for url in ("/api/knowledge/status", "/api/knowledge/search?q=x", "/api/knowledge/page/42"):
            bodies.append(kb.get(url).text)
    return bodies


def test_token_never_in_any_response_or_log(kb, fake, caplog):
    caplog.set_level(logging.DEBUG)
    bodies = _every_failure(kb, fake)
    blob = "\n".join(bodies) + "\n" + caplog.text
    assert TOKEN_SECRET not in blob
    assert TOKEN_ID not in blob


def test_token_never_in_log_when_token_file_is_bad(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    client, _ = _make_client(tmp_path, monkeypatch, _enabled, token_mode=0o644)
    resp = client.get("/api/knowledge/status")
    assert TOKEN_SECRET not in resp.text + caplog.text


def test_token_file_readable_by_others_is_refused_and_bookstack_not_called(tmp_path, monkeypatch, fake):
    client, _ = _make_client(tmp_path, monkeypatch, _enabled, token_mode=0o644)
    client.app.state.knowledge._transport = httpx.MockTransport(fake)
    resp = client.get("/api/knowledge/status")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "token_file_permissions"
    assert fake.requests == []
    assert resp.json()["status"]["down_since"] is None  # operator error, not a BookStack outage


def test_missing_token_file(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, _enabled, token_text=None)
    resp = client.get("/api/knowledge/status")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "token_file_missing"


@pytest.mark.parametrize("text", ["no-colon-here", ":only-secret", "only-id:", "a:b\nc:d"])
def test_malformed_token_file(tmp_path, monkeypatch, text):
    client, _ = _make_client(tmp_path, monkeypatch, _enabled, token_text=text)
    resp = client.get("/api/knowledge/status")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "token_file_invalid"


def test_enabled_without_base_url_is_not_configured(tmp_path, monkeypatch):
    modules = {"knowledge": {"enabled": True, "allowed_hosts": ["testserver"],
                          "bookstack": {"base_url": "", "token_file": ""}}}
    client, _ = _make_client(tmp_path, monkeypatch, modules)
    resp = client.get("/api/knowledge/status")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "not_configured"
    assert client.get("/api/modules").json() == {"enabled": ["knowledge"]}


# ---------------- cross-origin ----------------

def test_cross_origin_request_is_refused(kb, fake):
    resp = kb.get("/api/knowledge/page/42", headers={"Origin": "https://evil.example.net"})
    assert resp.status_code == 403
    assert fake.requests == []
    assert "Synthetic" not in resp.text
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "origin_not_allowed"
    assert body["error"]["http_status"] == 403 and "status" in body


def test_same_origin_request_is_allowed(kb):
    resp = kb.get("/api/knowledge/status", headers={"Origin": "http://testserver"})
    assert resp.status_code == 200


# ---------------- DNS rebinding: Host allowlist ----------------

def test_foreign_host_with_matching_origin_is_refused(kb, fake):
    resp = kb.get("/api/knowledge/page/42",
                  headers={"Host": "evil.example.net", "Origin": "http://evil.example.net"})
    assert resp.status_code == 403
    assert fake.requests == []
    assert resp.json()["ok"] is False
    assert resp.json()["error"]["code"] == "host_not_allowed"


def test_foreign_host_without_origin_is_refused(kb, fake):
    resp = kb.get("/api/knowledge/status", headers={"Host": "evil.example.net"})
    assert resp.status_code == 403
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "host_not_allowed"
    assert body["error"]["http_status"] == 403 and "status" in body
    assert fake.requests == []


def _no_allowed_hosts(tf):
    m = _enabled(tf)
    del m["knowledge"]["allowed_hosts"]
    return m


def test_bind_host_set_to_a_specific_address_allows_that_host(tmp_path, monkeypatch, fake):
    client, _ = _make_client(tmp_path, monkeypatch, _no_allowed_hosts,
                             extra_sources={"bind_host": "192.0.2.10"})
    client.app.state.knowledge._transport = httpx.MockTransport(fake)
    assert client.get("/api/knowledge/status", headers={"Host": "192.0.2.10:9999"}).status_code == 200
    assert client.get("/api/knowledge/status", headers={"Host": "192.0.2.11"}).status_code == 403


def test_hosts_entry_name_allows_that_host(tmp_path, monkeypatch, fake):
    client, _ = _make_client(tmp_path, monkeypatch, _no_allowed_hosts,
                             extra_sources={"hosts": [{"name": "box.example.net"}]})
    client.app.state.knowledge._transport = httpx.MockTransport(fake)
    assert client.get("/api/knowledge/status", headers={"Host": "box.example.net:9999"}).status_code == 200
    assert client.get("/api/knowledge/status", headers={"Host": "other.example.net"}).status_code == 403


def test_wildcard_bind_host_does_not_allow_host_0_0_0_0(tmp_path, monkeypatch, fake):
    client, _ = _make_client(tmp_path, monkeypatch, _no_allowed_hosts,
                             extra_sources={"bind_host": "0.0.0.0"})
    client.app.state.knowledge._transport = httpx.MockTransport(fake)
    resp = client.get("/api/knowledge/status", headers={"Host": "0.0.0.0:9999"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "host_not_allowed"


@pytest.mark.parametrize("host", ["localhost:9999", "127.0.0.1:9999", "[::1]:9999", "LOCALHOST"])
def test_loopback_hosts_are_allowed(kb, host):
    assert kb.get("/api/knowledge/status", headers={"Host": host}).status_code == 200


def test_allowed_hosts_option_admits_a_configured_name(tmp_path, monkeypatch, fake):
    def mods(tf):
        m = _enabled(tf)
        m["knowledge"]["allowed_hosts"] = ["board.example.net"]
        return m
    client, _ = _make_client(tmp_path, monkeypatch, mods)
    client.app.state.knowledge._transport = httpx.MockTransport(fake)
    assert client.get("/api/knowledge/status", headers={"Host": "board.example.net:9999"}).status_code == 200
    assert client.get("/api/knowledge/status", headers={"Host": "evil.example.net"}).status_code == 403


def test_host_check_is_scoped_to_knowledge_routes(kb):
    assert kb.get("/api/modules", headers={"Host": "evil.example.net"}).status_code == 200


# ---------------- upstream size / time bounds ----------------

def test_oversize_search_body_is_upstream_error_fast(kb, fake):
    big = b'{"data": [], "pad": "' + b"x" * (knowledge_mod.MAX_SEARCH_BYTES + 10) + b'"}'
    kb.app.state.knowledge._transport = httpx.MockTransport(lambda r: httpx.Response(200, content=big))
    resp = kb.get("/api/knowledge/search?q=x")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_too_large"


def test_oversize_page_body_is_upstream_error(kb):
    big = b'{"html": "' + b"x" * (knowledge_mod.MAX_PAGE_BYTES + 10) + b'"}'
    kb.app.state.knowledge._transport = httpx.MockTransport(lambda r: httpx.Response(200, content=big))
    resp = kb.get("/api/knowledge/page/42")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_too_large"


def test_body_is_not_read_past_the_ceiling(kb):
    """A body with no Content-Length is cut off at the ceiling, not buffered whole."""
    pulled = []

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(10_000):
                pulled.append(1)
                yield b"x" * 100_000

    kb.app.state.knowledge._transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=Endless()))
    resp = kb.get("/api/knowledge/search?q=x")
    assert resp.json()["error"]["code"] == "upstream_too_large"
    assert len(pulled) <= knowledge_mod.MAX_SEARCH_BYTES // 100_000 + 2


def test_slow_drip_is_bounded_by_an_overall_deadline(tmp_path, monkeypatch):
    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(1000):
                await asyncio.sleep(0.3)  # each op well inside any per-op timeout
                yield b" "

    client, _ = _make_client(tmp_path, monkeypatch, lambda tf: _enabled(tf, timeout_s=1))
    client.app.state.knowledge._transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=Drip()))
    # Bounded here so a missing deadline fails the test instead of hanging it.
    async def run():
        with pytest.raises(knowledge_mod.KnowledgeError) as ei:
            await asyncio.wait_for(client.app.state.knowledge.search("x"), 4)
        return ei.value.code
    start = time.monotonic()
    assert asyncio.run(run()) == "unreachable"
    assert time.monotonic() - start < 3


def test_page_two_calls_share_one_overall_deadline(tmp_path, monkeypatch):
    """page() makes two sequential calls; each is slower than timeout_s but
    inside its own 2x deadline. Together they exceed 2x timeout_s."""
    async def handler(request):
        await asyncio.sleep(1.5)
        if request.url.path.startswith("/api/pages/"):
            return httpx.Response(200, json=SYN_PAGE)
        return httpx.Response(200, json=SYN_BOOK)

    client, _ = _make_client(tmp_path, monkeypatch, lambda tf: _enabled(tf, timeout_s=1))
    client.app.state.knowledge._transport = httpx.MockTransport(handler)

    async def run():
        with pytest.raises(knowledge_mod.KnowledgeError) as ei:
            await asyncio.wait_for(client.app.state.knowledge.page(42), 6)
        return ei.value.code
    assert asyncio.run(run()) == "unreachable"


def test_event_loop_stays_responsive_during_a_slow_parse(tmp_path, monkeypatch, fake):
    client, _ = _make_client(tmp_path, monkeypatch, _enabled)
    client.app.state.knowledge._transport = httpx.MockTransport(fake)
    real = knowledge_mod.html_to_text

    def slow(html):
        time.sleep(1.0)
        return real(html)

    monkeypatch.setattr(knowledge_mod, "html_to_text", slow)

    async def run():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            t0 = time.monotonic()
            done = {}

            async def other():
                await asyncio.sleep(0.2)  # let the page request reach the parse
                r = await ac.get("/api/modules")
                done["other"] = time.monotonic() - t0
                return r

            page, o = await asyncio.gather(ac.get("/api/knowledge/page/42"), other())
            done["page"] = time.monotonic() - t0
            return page, o, done

    page, o, done = asyncio.run(run())
    assert page.status_code == 200 and o.status_code == 200
    assert done["page"] >= 1.0
    assert done["other"] < 0.8  # answered while the 1 s parse was still running


def test_parser_input_is_capped_before_parsing(kb, fake, monkeypatch):
    seen = []
    real = knowledge_mod.html_to_text
    monkeypatch.setattr(knowledge_mod, "html_to_text", lambda h: (seen.append(len(h)), real(h))[1])
    fake.page = {**SYN_PAGE, "html": "<p>" + "a" * (knowledge_mod.HTML_INPUT_MAX_CHARS * 2) + "</p>"}
    data = kb.get("/api/knowledge/page/42").json()
    assert max(seen) <= knowledge_mod.HTML_INPUT_MAX_CHARS
    assert data["truncated"] is True


# ---------------- token file / type edge cases ----------------

def test_binary_token_file_is_token_file_invalid_not_500(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, _enabled)
    (tmp_path / "bookstack.token").write_bytes(b"\xff\xfe\x80\x81:\xc3\x28")
    resp = client.get("/api/knowledge/status")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "token_file_invalid"


def test_boolean_ids_are_not_integers(kb, fake):
    fake.page = {**SYN_PAGE, "book_id": True}
    data = kb.get("/api/knowledge/page/42").json()
    assert data["book"]["id"] is None
    assert not any(r.url.path.startswith("/api/books/") for r in fake.requests)
    kb.app.state.knowledge._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "total": 1, "data": [{"id": True, "type": "page", "name": "bool id"}]}))
    assert kb.get("/api/knowledge/search?q=x").json()["results"] == []


# ---------------- text cap ----------------

def test_page_text_over_the_cap_is_truncated_and_flagged(kb, fake):
    n = knowledge_mod.PAGE_TEXT_MAX_CHARS
    fake.page = {**SYN_PAGE, "html": "<p>" + "a" * (n + 500) + "</p>"}
    data = kb.get("/api/knowledge/page/42").json()
    assert data["truncated"] is True
    assert len(data["text"]) == n


def test_page_text_at_or_under_the_cap_is_not_flagged(kb, fake):
    fake.page = {**SYN_PAGE, "html": "<p>" + "a" * knowledge_mod.PAGE_TEXT_MAX_CHARS + "</p>"}
    data = kb.get("/api/knowledge/page/42").json()
    assert data["truncated"] is False
    assert len(data["text"]) == knowledge_mod.PAGE_TEXT_MAX_CHARS


def test_declared_content_length_over_the_ceiling_is_refused_unread(kb):
    class Boom(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("body must not be read when Content-Length is over the ceiling")
            yield b""

    headers = {"content-length": str(knowledge_mod.MAX_SEARCH_BYTES + 1)}
    kb.app.state.knowledge._transport = httpx.MockTransport(
        lambda r: httpx.Response(200, headers=headers, stream=Boom()))
    resp = kb.get("/api/knowledge/search?q=x")
    assert resp.json()["error"]["code"] == "upstream_too_large"
