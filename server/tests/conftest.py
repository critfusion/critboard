from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


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
