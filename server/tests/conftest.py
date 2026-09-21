from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# The real, live config files this repo ships with (server/tests/ -> two
# parents up is the dashboard root, same math as critdash.config.SERVER_DIR/
# DASHBOARD_ROOT). Tests must build their own isolated Config (see
# `isolated_app` / `_write_isolated_config` in test_main_helpers.py,
# CONFIG_DIR monkeypatched) rather than ever writing here -- this fixture is
# the safety net that catches a regression of that isolation, not a
# guarantee by itself.
_LIVE_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_LIVE_CONFIG_FILES = [_LIVE_CONFIG_DIR / "layout.json", _LIVE_CONFIG_DIR / "theme.json"]


def _hash_live_config_files() -> dict[Path, str | None]:
    hashes: dict[Path, str | None] = {}
    for path in _LIVE_CONFIG_FILES:
        if path.exists():
            hashes[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            hashes[path] = None
    return hashes


@pytest.fixture(scope="session", autouse=True)
def _live_config_files_untouched():
    """Session-wide guardrail: hash config/layout.json and config/theme.json
    before the suite runs and again after, and fail loudly if either changed
    -- no test in this suite is allowed to write to the real config
    directory, only to an isolated tmp_path one (CRITDASH_CONFIG_DIR /
    monkeypatched config.CONFIG_DIR)."""
    before = _hash_live_config_files()
    yield
    after = _hash_live_config_files()
    assert after == before, (
        "a test wrote to the live config/*.json files -- tests must use an "
        "isolated config dir (see isolated_app / _write_isolated_config)"
    )


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def tmp_store(tmp_path):
    from critdash.store import Store

    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def make_kimi_root(tmp_path):
    """Materializes tests/fixtures/kimi (a captured, redaction-free copy of
    the real verified ~/.kimi-code format -- session_index.jsonl,
    workspaces.json, per-session state.json/wire.jsonl) into a real
    directory under tmp_path, substituting its SESSIONS_ROOT/UPDATED_AT_MS_*
    placeholders so every test gets a self-contained tree with deterministic
    session ages instead of depending on wall-clock time at fixture-authoring
    time."""
    def _make(age_s_1: float = 300.0, age_s_2: float = 30.0) -> Path:
        dest = tmp_path / "kimi-code"
        shutil.copytree(FIXTURES / "kimi", dest)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        updated_1 = now_ms - int(age_s_1 * 1000)
        updated_2 = now_ms - int(age_s_2 * 1000)
        for path in dest.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".jsonl"):
                text = path.read_text()
                text = text.replace("SESSIONS_ROOT", str(dest))
                text = text.replace("UPDATED_AT_MS_1", str(updated_1))
                text = text.replace("UPDATED_AT_MS_2", str(updated_2))
                path.write_text(text)
        return dest
    return _make
