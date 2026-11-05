"""One owner per helper (issue #15). Each test fails if a copy comes back."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from critdash import collectors, remote_probe, state
from critdash.collectors import analytics

WEB = Path(__file__).resolve().parents[2] / "web"


def test_collectors_now_iso_is_the_state_helper():
    assert collectors.now_iso is state.now_iso


@pytest.mark.parametrize(
    "name",
    [
        "classify_error",
        "classify_api_error",
        "redact_secrets",
        "make_example",
        "tool_result_text",
        "extract_file_path",
    ],
)
def test_analytics_uses_the_probe_copy(name):
    # remote_probe.py must stay stdlib-only and is piped alone to remote
    # hosts, so it owns these rules and analytics.py imports them.
    assert getattr(analytics, name) is getattr(remote_probe, name)


def test_widgets_do_not_define_their_own_storage_key():
    for path in sorted((WEB / "js" / "widgets").glob("*.js")):
        assert not re.search(r"function\s+storageKey\b", path.read_text()), path.name


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed on this host")
def test_storage_key_node_test_passes():
    result = subprocess.run(
        ["node", str(WEB / "js" / "storage_key.test.mjs")],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "passed" in result.stdout


def test_upsert_remote_returns_row_count_and_replaces(tmp_path):
    # Every upsert_remote_* method goes through Store._replace_rows: it returns
    # the number of rows sent (0 for none) and a second call overwrites.
    from critdash.store import Store

    store = Store(tmp_path / "t.db")
    row = {"host": "h1", "day": "2026-01-02", "kind": "rate_limit", "count": 3, "last_seen": "t1"}
    assert store.upsert_remote_api_error_buckets([]) == 0
    assert store.upsert_remote_api_error_buckets([row]) == 1
    assert store.upsert_remote_api_error_buckets([{**row, "count": 5}, {**row, "kind": "timeout"}]) == 2
    counts = {r["kind"]: r["count"] for r in store.remote_api_errors("2026-01-01")}
    assert counts == {"rate_limit": 5, "timeout": 3}
