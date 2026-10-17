"""View navigation (web/js/nav.js) and Knowledge outage wording
(web/js/knowledge_text.js) are pure JS, tested by web/js/nav.test.mjs under
plain node, the same way test_banner_priority.py runs its file. Also checks
the static page: with no module enabled the new elements are inert."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent.parent / "web"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed on this host")
def test_nav_node_test_passes():
    result = subprocess.run(
        ["node", str(WEB / "js" / "nav.test.mjs")], capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "passed" in result.stdout


def _tag(html: str, element_id: str) -> str:
    m = re.search(rf"<[^>]*\bid=\"{element_id}\"[^>]*>", html)
    assert m, element_id
    return m.group(0)


def test_default_page_ships_new_elements_hidden():
    html = (WEB / "index.html").read_text()
    for element_id in ("view-nav", "view-host"):
        assert re.search(r"\bhidden\b", _tag(html, element_id)), f"#{element_id} must start hidden"


def test_existing_header_controls_still_present_and_not_hidden():
    html = (WEB / "index.html").read_text()
    for element_id in ("window-selector", "grid-wrap", "grid", "conn-indicator", "help-overlay"):
        assert not re.search(r"\bhidden\b", _tag(html, element_id)), f"#{element_id} must not start hidden"


def test_views_are_set_up_only_through_setup_views():
    # The only hook into the existing app is one awaited setupViews() call.
    app_js = (WEB / "js" / "app.js").read_text()
    assert app_js.count("setupViews(") == 1
    assert "resolveInitialHash()" in app_js
    assert app_js.index("setupViews(") < app_js.index("modal.resolveInitialHash()")


def test_workflow_view_is_mounted_by_views_js_and_has_no_placeholder_path():
    views = (WEB / "js" / "views.js").read_text()
    assert 'view === "workflow"' in views and "mountWorkflow" in views
    # the placeholder branch comes after every real mount
    assert views.index('view === "workflow"') < views.index("view-placeholder")


def test_workflow_view_never_writes_server_text_as_html():
    for name in ("workflow_view.js", "workflow_text.js"):
        text = (WEB / "js" / name).read_text()
        assert "innerHTML" not in text and "html:" not in text, name
