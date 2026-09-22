"""Defect 3 (macOS install report): #reload-banner and #update-banner must
never both show at once. The mutual-exclusion decision itself lives in pure,
DOM-free JS (web/js/banner_priority.js) specifically so it's testable without
a browser -- this repo has no frontend test harness (no package.json, no
Playwright/Jest/Vitest; see web/js/banner_priority.test.mjs's own docstring).
This just shells out to `node` to run that test file and asserts it passed,
so `pytest server/tests -q` covers the frontend logic too without inventing a
JS test runner."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB_JS_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "js"
TEST_FILE = WEB_JS_DIR / "banner_priority.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed on this host")
def test_banner_priority_node_test_passes():
    result = subprocess.run(
        ["node", str(TEST_FILE)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "passed" in result.stdout
