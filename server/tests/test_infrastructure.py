"""Infrastructure module: record parser, BookStack book reader, endpoint gate.

Every wiki payload below is SYNTHETIC (wiki.example.com, host-a,
example-org/repo). The parser tests pass a fixed clock; none reads the time.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_knowledge import BASE, TOKEN, _enabled, _make_client

from critdash import knowledge as knowledge_mod
from critdash.infrastructure import (
    compute_status,
    parse_provenance_table,
    parse_record,
    parse_tags,
    parse_facts,
    parse_when,
    merge_status as _merge_status,
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


D = ("unknown", True)  # a status the page declares as unknown


def merge_status(table, *tags):
    """Bare strings are readable-text statuses; D is a declared unknown."""
    return _merge_status(*((v if isinstance(v, tuple) else (v, False)) for v in (table, *tags)))


# ---------------- backticks, notes and facts (context endpoint) ----------------

LIVE_SHAPE = """\
| Field | Value |
|---|---|
| Source repo | `example-org/repo-a` (`main`) |
| Source path | `projects/proj-a.md` |
| Source git SHA | `0123abc\u2026` (blob `4567def`) |
| Class | project (projection) |
| Status | `current` |
"""


def test_backticks_are_removed_from_every_value():
    out = parse_provenance_table(LIVE_SHAPE)
    assert out["source_repo"] == "example-org/repo-a" and out["source_repo_note"] == "main"
    assert out["source_path"] == "projects/proj-a.md"
    assert out["status"] == "current"
    assert "`" not in "".join(out.values())


def test_sha_parenthetical_moves_to_its_own_note_field():
    out = parse_provenance_table(LIVE_SHAPE)
    assert out["source_git_sha"] == "0123abc\u2026" and out["source_git_sha_note"] == "blob 4567def"


def test_repo_parenthetical_moves_to_its_own_note_field_and_keeps_the_link():
    md = "| Field | Value |\n|---|---|\n| Source repo | `https://github.com/example-org/repo-a` (`main`) |\n"
    assert parse_provenance_table(md)["source_repo_note"] == "main"
    src = _record(markdown=md)["source"]
    assert src["repo"] == "https://github.com/example-org/repo-a" and src["repo_note"] == "main"
    assert src["repo_href"] == "https://github.com/example-org/repo-a"
    assert "repo_note" not in _record()["source"]


_NOTE_ROWS = (("Source repo", "source_repo", "repo", "example-org/old-repo", "example-org/new-repo"),
              ("Source path", "source_path", "path", "projects/old.md", "projects/new.md"),
              ("Source git SHA", "source_git_sha", "git_sha", "0123abc", "89abcde"))
_REPLACEMENTS = (("{new}", "{new}"), ("{new} ()", "{new}"), ("unknown", "unknown"), ("{new} ( )", "{new}"))


def _replaced(label, old, newcell):
    return f"| Field | Value |\n|---|---|\n| {label} | {old} (main) |\n| {label} | {newcell} |\n"


@pytest.mark.parametrize("label,pkey,skey,old,new", _NOTE_ROWS)
@pytest.mark.parametrize("cell,want", _REPLACEMENTS)
def test_replacing_a_row_drops_the_earlier_rows_note(label, pkey, skey, old, new, cell, want):
    md = _replaced(label, old, cell.format(new=new))
    out = parse_provenance_table(md)
    assert out[pkey] == want.format(new=new) and pkey + "_note" not in out
    src = _record(markdown=md)["source"]
    assert src[skey] == want.format(new=new) and skey + "_note" not in src


@pytest.mark.parametrize("label,pkey,skey,old,new", _NOTE_ROWS)
def test_replacing_a_row_with_a_noted_one_keeps_only_the_new_note(label, pkey, skey, old, new):
    md = f"| Field | Value |\n|---|---|\n| {label} | {old} (main) |\n| {label} | {new} (dev) |\n"
    assert parse_provenance_table(md)[pkey + "_note"] == "dev"
    src = _record(markdown=md)["source"]  # the record carries the new note too
    assert src[skey + "_note"] == "dev" and src[skey] == new


@pytest.mark.parametrize("cell", ["n/a (z)", "tbd (z)", "unknown (z)", "- (z)"])
@pytest.mark.parametrize("label,pkey,skey", [("Source repo", "source_repo", "repo"),
                                             ("Source path", "source_path", "path"),
                                             ("Source git SHA", "source_git_sha", "git_sha")])
def test_an_unknown_value_carries_no_note(cell, label, pkey, skey):
    md = f"| Field | Value |\n|---|---|\n| {label} | {cell} |\n"
    out = parse_provenance_table(md)
    assert out[pkey] == "unknown" and pkey + "_note" not in out
    src = _record(markdown=md)["source"]
    assert src[skey] == "unknown" and skey + "_note" not in src


@pytest.mark.parametrize("url", ["https://en.wikipedia.org/wiki/Foo_(bar)", "http://example.com/a_(b)", "HTTPS://X/Y(z)"])
def test_a_url_repo_ending_in_a_parenthetical_is_not_split(url):
    md = f"| Field | Value |\n|---|---|\n| Source repo | {url} |\n"
    out = parse_provenance_table(md)
    assert out["source_repo"] == url and "source_repo_note" not in out
    src = _record(markdown=md)["source"]
    assert src["repo_href"] == url and "repo_note" not in src


def test_a_url_with_a_spaced_parenthetical_still_splits():
    md = "| Field | Value |\n|---|---|\n| Source repo | https://github.com/example-org/repo-a (main) |\n"
    out = parse_provenance_table(md)
    assert out["source_repo"] == "https://github.com/example-org/repo-a" and out["source_repo_note"] == "main"


def test_path_parenthetical_moves_to_its_own_note_field():
    md = "| Field | Value |\n|---|---|\n| Source path | `docs/a.md` (copy of `docs/b.md`) |\n"
    out = parse_provenance_table(md)
    assert out["source_path"] == "docs/a.md" and out["source_path_note"] == "copy of docs/b.md"


def test_no_parenthetical_means_no_note_key():
    out = parse_provenance_table(TABLE)
    assert "source_git_sha_note" not in out and "source_path_note" not in out


@pytest.mark.parametrize("cell,value", [
    ("(only a note)", "(only a note)"),          # nothing before it: not split
    ("abc (a (nested) note)", "abc (a (nested) note)"),  # nested parentheses: left alone
    ("abc ()", "abc"),                           # empty note: dropped, no note key
    ("abc (note) tail", "abc (note) tail"),      # not trailing
])
def test_only_a_plain_trailing_parenthetical_is_split(cell, value):
    md = f"| Field | Value |\n|---|---|\n| Source git SHA | {cell} |\n"
    out = parse_provenance_table(md)
    assert out["source_git_sha"] == value and "source_git_sha_note" not in out


def test_record_carries_the_notes_in_source_and_omits_them_when_absent():
    r = _record(markdown=LIVE_SHAPE)
    assert r["source"]["git_sha"] == "0123abc\u2026" and r["source"]["git_sha_note"] == "blob 4567def"
    assert "path_note" not in r["source"] and "path_note" not in _record()["source"]


def test_facts_come_from_any_field_value_table_and_default_to_unknown():
    md = TABLE + "\n## Facts\n\n| Field | Value |\n|---|---|\n| Canonical repo | `example-org/proj-a` |\n" \
        "| Dev/prod boundary | dev on branches |\n| Canonical location | |\n"
    assert parse_facts(md) == {"repo": "example-org/proj-a", "location": "unknown", "boundary": "dev on branches"}
    assert parse_facts(TABLE) == {"repo": "unknown", "location": "unknown", "boundary": "unknown"}
    assert parse_facts(None) == {"repo": "unknown", "location": "unknown", "boundary": "unknown"}


def test_facts_ignore_rows_outside_a_field_value_table():
    md = "| Name | Value |\n|---|---|\n| Canonical repo | example-org/x |\n"
    assert parse_facts(md)["repo"] == "unknown"


def test_record_exposes_canonical_slug_and_scope_tags():
    r = _record(markdown=TABLE + "\n| Field | Value |\n|---|---|\n| Canonical repo | example-org/r |\n",
                tags=[{"name": "project", "value": "proj-a"}, {"name": "scope", "value": "all"}])
    assert r["canonical"]["repo"] == "example-org/r" and r["slug"] == "host-a"
    assert r["projects"] == ["proj-a"] and r["scopes"] == ["all"]


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


def test_non_list_tags_are_no_tags():
    for junk in (5, "x", {"name": "kind"}, None, True):
        tags = parse_tags(junk)
        assert tags["kind"] == "unknown" and tags["related"] == []
    assert _record(tags=5)["kind"] == "unknown"


def test_tag_values_and_tag_count_are_bounded():
    long = "x" * 900
    tags = parse_tags([{"name": "purpose", "value": long}] + [{"name": "related", "value": f"r{i}"} for i in range(80)])
    assert len(tags["purpose"]) == 500
    assert len(tags["related"]) == 49  # only the first 50 tags of the page are read
    late = parse_tags([{"name": "x", "value": "1"}] * 50 + [{"name": "owner", "value": "late"}])
    assert late["owner"] == "unknown"


def test_table_cells_title_and_editor_are_bounded():
    md = "| Field | Value |\n|---|---|\n| Source repo | " + "y" * 900 + " |\n"
    r = _record(markdown=md, name="t" * 900, updated_by={"name": "e" * 900})
    assert len(r["source"]["repo"]) == 500 and len(r["title"]) == 500 and len(r["updated_by"]) == 500


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
    for bad in ("never", "", None, "2026-02-30", "10/01/2026", "2027-06-01",
                "0001-01-01T00:00:00+05:00", "9999-12-31T23:00:00-05:00"):
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


# ---------------- status and class tags ----------------

def test_status_tag_is_used_when_the_table_has_none():
    md = "| Field | Value |\n|---|---|\n| Live check | 2026-10-10 |\n"
    r = _record(markdown=md, tags=[{"name": "status", "value": "needs-verification"}])
    assert r["status"] == "needs-verification" and r["status_conflict"] is None
    r = _record(markdown="no table", tags=[{"name": "status", "value": "contradicted"}])
    assert r["status"] == "contradicted"


def test_class_tag_fills_in_when_the_table_has_no_class():
    r = _record(markdown="no table", tags=[{"name": "class", "value": "CK"}])
    assert r["source"]["class"] == "CK"
    assert _record(tags=[{"name": "class", "value": "CK"}])["source"]["class"] == "host"  # table wins


@pytest.mark.parametrize("table,tag,winner", [
    ("current", "needs-verification", "needs-verification"),
    ("needs-verification", "current", "needs-verification"),
    ("current", "contradicted", "contradicted"),
    ("contradicted", "stale", "contradicted"),
    ("stale", "needs-verification", "stale"),
])
def test_conflicting_table_and_tag_status_worse_wins_with_a_visible_note(table, tag, winner):
    assert merge_status(table, tag) == (winner, f"status conflict: table {table}, tag {tag}")


def test_repeated_status_tags_merge_worse_wins_with_a_conflict_note():
    r = _record(markdown="no table", tags=[{"name": "status", "value": "current"}, {"name": "status", "value": "contradicted"}])
    assert r["status"] == "contradicted"
    assert r["status_conflict"] == "status conflict: tag current, tag contradicted"
    r = _record(tags=[{"name": "status", "value": "stale"}, {"name": "status", "value": "needs-verification"}])
    assert r["status"] == "stale" and "table current" in r["status_conflict"]
    assert "tag stale" in r["status_conflict"] and "tag needs-verification" in r["status_conflict"]


def test_repeated_identical_or_unreadable_status_tags_are_no_conflict():
    r = _record(markdown="no table", tags=[{"name": "status", "value": "stale"}, {"name": "status", "value": "Stale"},
                                          {"name": "status", "value": "sparkly"}])
    assert r["status"] == "stale" and r["status_conflict"] is None
    assert merge_status("unknown", "current", "contradicted")[0] == "contradicted"


def test_agreeing_or_absent_statuses_have_no_conflict_note():
    assert merge_status("current", "current") == ("current", None)
    assert merge_status("unknown", "stale") == ("stale", None)  # bare "unknown" = absent at this level
    assert merge_status("stale", "unknown") == ("stale", None)
    assert merge_status("sparkly", "also-junk") == ("unknown", None)


def test_declared_unknown_is_a_real_value_worse_than_current():
    assert merge_status("current", D) == ("unknown", "status conflict: table current, tag unknown")
    assert merge_status(D, "current") == ("unknown", "status conflict: table unknown, tag current")
    assert merge_status(D, "stale")[0] == "stale"  # stale is still worse
    assert merge_status(D, D) == ("unknown", None)


def test_parse_separates_declared_unknown_from_absent_and_junk():
    md = "| Field | Value |\n|---|---|\n| Status | Unknown |\n"
    assert parse_provenance_table(md)["status_declared_unknown"] is True
    for cell in ("", "tbd", "-", "sparkly"):
        out = parse_provenance_table(f"| Field | Value |\n|---|---|\n| Status | {cell} |\n")
        assert "status_declared_unknown" not in out
    tags = parse_tags([{"name": "status", "value": " `Unknown` "}, {"name": "status", "value": ""},
                       {"name": "status", "value": "tbd"}, {"name": "status", "value": "junk"}])
    assert tags["statuses"] == [D, ("junk", False)]


def test_conflict_note_reaches_the_record_and_current_is_not_shown_when_a_tag_disagrees():
    r = _record(tags=[{"name": "status", "value": "needs-verification"}])  # table says current, Live check fresh
    assert r["status"] == "needs-verification"
    assert r["status_conflict"] == "status conflict: table current, tag needs-verification"


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


def test_updated_at_and_slug_are_bounded():
    r = _record(updated_at="2026-10-01T00:00:00Z" + "9" * 900, slug="s" * 900)
    assert len(r["updated_at"]) == 500 and len(r["url"].rsplit("/page/", 1)[1]) == 500


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
        self.fail_page = None
        self.books_404 = False
        self.listing_404 = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.mode == "down":
            raise httpx.ConnectError("synthetic connection refused", request=request)
        if self.mode == "500":
            return httpx.Response(500, json={})
        path, q = request.url.path, dict(request.url.params)
        if path == "/api/books" and self.books_404:
            return httpx.Response(404, json={})
        if path == "/api/pages" and self.listing_404:
            return httpx.Response(404, json={})
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
            if self.fail_page == pid:
                return httpx.Response(500, json={})
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
    assert body["status"]["ok"] is False and body["status"]["down_since"]


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
    body = resp.json()
    assert resp.status_code == 502 and body["error"]["code"] == "upstream_error"
    assert body["status"]["ok"] is False and body["status"]["down_since"]


def test_one_failing_page_among_several_is_not_wiped_from_status(tmp_path, monkeypatch):
    wiki = FakeWiki(n=8)
    wiki.fail_page = 3
    client, _ = _make_client(tmp_path, monkeypatch, _infra_modules)
    client.app.state.knowledge._transport = httpx.MockTransport(wiki)
    for _ in range(3):  # concurrent order varies; the later pages that answer must not clear it
        resp = client.get("/api/infrastructure")
        body = resp.json()
        assert resp.status_code == 502 and body["ok"] is False and "records" not in body
        assert body["status"]["ok"] is False and body["status"]["down_since"]
        assert body["status"]["error"]["code"] == "upstream_error"
    wiki.fail_page = None
    assert client.get("/api/infrastructure").json()["status"]["down_since"] is None


def test_deleted_page_does_not_clear_an_earlier_failure(tmp_path, monkeypatch):
    wiki = FakeWiki(n=4)
    wiki.fail_page, wiki.gone = 1, {2, 3, 4}
    client, _ = _make_client(tmp_path, monkeypatch, _infra_modules)
    client.app.state.knowledge._transport = httpx.MockTransport(wiki)
    body = client.get("/api/infrastructure").json()
    assert body["ok"] is False and body["status"]["ok"] is False and body["status"]["down_since"]


def test_pages_are_listed_with_a_stable_sort(infra, wiki):
    infra.get("/api/infrastructure")
    listing = [r for r in wiki.requests if r.url.path == "/api/pages"]
    assert len(listing) == 1 and dict(listing[0].url.params)["sort"] == "+name"


def test_unparseable_page_becomes_a_degraded_record_not_a_500(infra, wiki, monkeypatch):
    real = knowledge_mod.parse_record

    def flaky(page, **kw):
        if page["id"] == 2:
            raise RuntimeError("synthetic parser bug")
        return real(page, **kw)

    monkeypatch.setattr(knowledge_mod, "parse_record", flaky)
    resp = infra.get("/api/infrastructure")
    body = resp.json()
    assert resp.status_code == 200 and body["ok"] is True and len(body["records"]) == 3
    bad = next(r for r in body["records"] if r["id"] == 2)
    assert bad["status"] == "unknown" and bad["status_reason"] == "unreadable record"
    assert bad["title"] == "host-2" and bad["kind"] == "unknown"


def test_record_id_is_the_listings_int_not_the_page_bodys(infra, wiki):
    wiki.pages[2]["id"] = "not-an-int"
    wiki.pages[3]["id"] = {"x": 1}
    body = infra.get("/api/infrastructure").json()
    assert [r["id"] for r in body["records"]] == [1, 2, 3]


def test_records_are_parsed_off_the_event_loop(infra, monkeypatch):
    real, on_loop = knowledge_mod.parse_record, []

    def spy(page, **kw):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:  # no loop in this thread: a worker thread
            on_loop.append(False)
        return real(page, **kw)

    monkeypatch.setattr(knowledge_mod, "parse_record", spy)
    assert infra.get("/api/infrastructure").json()["ok"] is True
    assert on_loop == [False] * 3


@pytest.mark.parametrize("flag", ["books_404", "listing_404"])
def test_404_on_book_lookup_or_listing_is_book_not_found_with_status(infra, wiki, flag):
    setattr(wiki, flag, True)
    resp = infra.get("/api/infrastructure")
    body = resp.json()
    assert resp.status_code == 503 and body["ok"] is False
    assert body["error"]["code"] == "book_not_found"
    assert body["status"]["error"]["code"] == "book_not_found" and body["status"]["down_since"] is None
    setattr(wiki, flag, False)
    assert infra.get("/api/infrastructure").json()["status"]["error"] is None


def test_missing_book_is_recorded_in_status_error(infra, wiki):
    wiki.book = None
    assert infra.get("/api/infrastructure").json()["status"]["error"]["code"] == "book_not_found"


def test_raw_pages_are_not_kept_after_parsing(infra, wiki):
    body = infra.get("/api/infrastructure").json()
    assert "markdown" not in json.dumps(body)


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


# ---- declared `unknown` through the real route (recent readable Live check) ----

def _routed(infra, wiki, table_status, tags):
    day = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = "| Field | Value |\n|---|---|\n" + f"| Live check | {day} |\n"
    if table_status is not None:
        rows += f"| Status | {table_status} |\n"
    wiki.pages[1]["markdown"] = rows
    wiki.pages[1]["tags"] = [{"name": "status", "value": t} for t in tags]
    resp = infra.get("/api/infrastructure")
    assert resp.status_code == 200
    return next(r for r in resp.json()["records"] if r["id"] == 1)


@pytest.mark.parametrize("table,tags,note", [
    ("current", ["unknown"], "status conflict: table current, tag unknown"),
    ("unknown", ["current"], "status conflict: table unknown, tag current"),
    (None, ["current", "unknown"], "status conflict: tag current, tag unknown"),
    (None, ["unknown", "current"], "status conflict: tag unknown, tag current"),
])
def test_declared_unknown_beats_current_with_a_conflict_note_via_the_route(infra, wiki, table, tags, note):
    r = _routed(infra, wiki, table, tags)
    assert r["status"] == "unknown" and r["status_conflict"] == note


@pytest.mark.parametrize("table,tags,status", [
    ("current", [], "current"),
    (None, ["current"], "current"),
    ("current", ["", "tbd", "sparkly"], "current"),   # empty/invalid tags are absent, not unknown
    ("current", ["needs-verification"], "needs-verification"),
])
def test_status_controls_via_the_route(infra, wiki, table, tags, status):
    r = _routed(infra, wiki, table, tags)
    assert r["status"] == status
    assert (r["status_conflict"] is not None) == (tags == ["needs-verification"])


# ---------------- route: contradicted and stale records ----------------

def test_contradicted_record_stays_contradicted_via_the_route_even_when_old(infra, wiki):
    for check in ((datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d"), "2020-01-01"):
        wiki.pages[1]["markdown"] = f"| Field | Value |\n|---|---|\n| Live check | {check} |\n| Status | contradicted |\n"
        wiki.pages[1]["tags"] = []
        r = next(r for r in infra.get("/api/infrastructure").json()["records"] if r["id"] == 1)
        assert r["status"] == "contradicted" and r["status_reason"] is None


def test_stale_record_says_why_and_a_stale_tag_conflict_is_visible_via_the_route(infra, wiki):
    wiki.pages[1]["markdown"] = "| Field | Value |\n|---|---|\n| Live check | 2020-01-01 |\n| Status | current |\n"
    wiki.pages[1]["tags"] = [{"name": "status", "value": "stale"}]
    r = next(r for r in infra.get("/api/infrastructure").json()["records"] if r["id"] == 1)
    assert r["status"] == "stale" and "older than 90 days" in r["status_reason"]
    assert r["status_conflict"] == "status conflict: table current, tag stale"


@pytest.mark.parametrize("table,tags", [("unknown (declared)", ["current"]), (None, ["unknown (declared)", "current"])])
def test_a_status_spelled_like_the_old_sentinel_is_just_unreadable(table, tags):
    md = TABLE if table is None else TABLE.replace("| Status | current |", f"| Status | {table} |")
    r = _record(markdown=md, tags=[{"name": "status", "value": t} for t in tags])
    assert r["status_conflict"] is None


@pytest.mark.parametrize("cells,flag,merged", [
    (("unknown", "current"), False, "current"),
    (("current", "unknown"), True, "unknown"),
    (("unknown", "tbd"), False, "unknown"),
])
def test_a_later_status_row_replaces_the_declared_unknown_flag(cells, flag, merged):
    md = "| Field | Value |\n|---|---|\n" + "".join(f"| Status | {c} |\n" for c in cells)
    out = parse_provenance_table(md)
    assert ("status_declared_unknown" in out) is flag
    assert merge_status((out["status"], bool(out.get("status_declared_unknown"))))[0] == merged
