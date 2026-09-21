import pytest

from critdash.collectors.dispatch import DispatchCollector, parse_log_line, parse_routes_conf


def test_parse_routes_conf(fixtures_dir):
    text = (fixtures_dir / "routes.conf").read_text()
    routes = parse_routes_conf(text)
    labels = {r["label"] for r in routes}
    assert "needs-codex" in labels
    assert "grok-review" in labels
    for r in routes:
        assert r["kind"]
    # comment-only / commented-out lines (e.g. "# android-test ...") must not appear
    assert "android-test" not in labels


def test_parse_routes_conf_ignores_comments_and_blank_lines():
    text = "\n# just a comment\n\nneeds-grok grok\n"
    routes = parse_routes_conf(text)
    assert routes == [{"label": "needs-grok", "kind": "grok", "precheck": None}]


def test_parse_routes_conf_with_precheck():
    text = "android-test claude adb devices | grep -q '^ABC device'\n"
    routes = parse_routes_conf(text)
    assert routes[0]["label"] == "android-test"
    assert routes[0]["kind"] == "claude"
    assert "adb devices" in routes[0]["precheck"]


def test_parse_log_line():
    parsed = parse_log_line("2026-09-18T13:30:01Z PAUSED(ALL)")
    assert parsed["t"] == "2026-09-18T13:30:01Z"
    assert parsed["line"] == "PAUSED(ALL)"


@pytest.mark.asyncio
async def test_collect_with_real_paths(tmp_path):
    overlord = tmp_path / "overlord"
    (overlord / "pause").mkdir(parents=True)
    (overlord / "routes.conf").write_text("needs-codex codex\nneeds-grok grok\n")
    (overlord / "pause" / "needs-grok").touch()
    (overlord / "fleet-dispatch.log").write_text("2026-09-18T13:00:00Z woke codex for x\n" * 3)

    collector = DispatchCollector(overlord_dir=str(overlord))
    result = await collector.collect()
    d = result["dispatch"]
    assert d["paused_all"] is False
    by_label = {r["label"]: r for r in d["routes"]}
    assert by_label["needs-grok"]["paused"] is True
    assert by_label["needs-codex"]["paused"] is False
    assert len(d["recent"]) == 3


@pytest.mark.asyncio
async def test_collect_paused_all(tmp_path):
    overlord = tmp_path / "overlord2"
    (overlord / "pause").mkdir(parents=True)
    (overlord / "routes.conf").write_text("needs-codex codex\n")
    (overlord / "pause" / "ALL").touch()

    collector = DispatchCollector(overlord_dir=str(overlord))
    result = await collector.collect()
    d = result["dispatch"]
    assert d["paused_all"] is True
    assert d["routes"][0]["paused"] is True
