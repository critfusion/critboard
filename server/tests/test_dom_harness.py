"""Browser test of the frontend DOM wiring (q733.8). Starts a real uvicorn
server on a free port with an isolated config dir, then runs
web/js/dom_harness.test.mjs, which drives index.html?fixture=1 in headless
Chromium through Playwright.

Playwright is not a declared dependency: this skips when `node` or the
node `playwright` package is not available, the same way the other
`node`-backed tests skip when `node` is missing."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from critdash import config as config_mod

ROOT = Path(__file__).resolve().parent.parent.parent
TEST_FILE = ROOT / "web" / "js" / "dom_harness.test.mjs"


def _playwright_resolvable() -> bool:
    if shutil.which("node") is None:
        return False
    # fetch() in the .mjs needs Node >= 18; below that, skip instead of failing.
    probe = subprocess.run(
        ["node", "-e", "if (+process.versions.node.split('.')[0] < 18) process.exit(1);"
         "require.resolve('playwright')"],
        capture_output=True, timeout=30, check=False,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _playwright_resolvable(), reason="node >= 18 or the node 'playwright' package is not installed",
)


@pytest.fixture
def live_server(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    sources = dict(config_mod.DEFAULT_SOURCES)
    sources["db_path"] = str(tmp_path / "test.db")
    sources["update_repo"] = ""  # no outbound update check from a test
    (config_dir / "sources.json").write_text(json.dumps(sources))
    (config_dir / "sources.example.json").write_text(json.dumps(config_mod.DEFAULT_SOURCES))
    for name in ("layout.example.json", "theme.json", "pricing.json"):
        shutil.copy(ROOT / "config" / name, config_dir / name)
    shutil.copy(ROOT / "config" / "layout.example.json", config_dir / "layout.json")

    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("CRITDASH_")
    }
    env["CRITDASH_CONFIG_DIR"] = str(config_dir)
    env["CRITDASH_DB_PATH"] = str(tmp_path / "test.db")
    # Bind the port here and hand the listening socket to uvicorn (--fd), so
    # no other process can take the port between picking it and serving.
    sock = socket.socket()
    proc = None
    log = (tmp_path / "server.log").open("w")
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "critdash.main:app", "--fd", str(sock.fileno())],
            cwd=ROOT / "server", env=env, stdout=log, stderr=subprocess.STDOUT,
            pass_fds=(sock.fileno(),),
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(f"{base}/api/healthz", timeout=2):
                    break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(
                        f"server did not start: {(tmp_path / 'server.log').read_text()}"
                    ) from None
                time.sleep(0.2)
        yield base
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        sock.close()
        log.close()


def test_dom_harness_passes(live_server):
    result = subprocess.run(
        ["node", str(TEST_FILE), live_server], capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode == 77:  # mjs: chromium binary not installed
        pytest.skip("playwright is installed but its Chromium browser is not")
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "6 passed" in result.stdout
