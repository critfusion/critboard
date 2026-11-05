"""The usage history widget's drawChart (web/js/widgets/usage_history.js) is
pinned by web/js/usage_history.test.mjs under plain node, the same way
test_nav.py runs its file. See that file's header for what it records."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent.parent / "web"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed on this host")
def test_usage_history_node_test_passes():
    result = subprocess.run(
        ["node", str(WEB / "js" / "usage_history.test.mjs")],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "passed" in result.stdout
