import json

import pytest

from critdash import config as config_mod


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    """A throwaway config dir, isolated from this repo's real config/.
    Patches the module-level CONFIG_DIR that load_config() reads -- the same
    thing CRITDASH_CONFIG_DIR does for a fresh-install smoke test, but
    direct, so a test doesn't depend on import order."""
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setattr(config_mod, "CONFIG_DIR", d)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    return d


def test_ensure_sources_file_seeds_from_example(isolated_config_dir):
    example = {"host": "localhost", "bind_host": "127.0.0.1", "bind_port": 9999}
    (isolated_config_dir / "sources.example.json").write_text(json.dumps(example))

    config_mod._ensure_sources_file(isolated_config_dir)

    target = isolated_config_dir / "sources.json"
    assert target.exists()
    assert json.loads(target.read_text()) == example


def test_ensure_sources_file_never_overwrites_existing(isolated_config_dir):
    (isolated_config_dir / "sources.example.json").write_text(json.dumps({"host": "example"}))
    (isolated_config_dir / "sources.json").write_text(json.dumps({"host": "real-machine"}))

    config_mod._ensure_sources_file(isolated_config_dir)

    assert json.loads((isolated_config_dir / "sources.json").read_text()) == {"host": "real-machine"}


def test_ensure_sources_file_noop_when_example_missing(isolated_config_dir):
    config_mod._ensure_sources_file(isolated_config_dir)
    assert not (isolated_config_dir / "sources.json").exists()


def test_load_config_bootstraps_from_example(isolated_config_dir):
    example = dict(config_mod.DEFAULT_SOURCES, repo_roots=["~/repos"])
    (isolated_config_dir / "sources.example.json").write_text(json.dumps(example))

    cfg = config_mod.load_config()

    assert (isolated_config_dir / "sources.json").exists()
    assert cfg.sources["repo_roots"] == ["~/repos"]
    assert cfg.sources["bind_host"] == "127.0.0.1"
    assert cfg.sources["bind_port"] == 9999


def test_load_config_leaves_existing_sources_untouched(isolated_config_dir):
    (isolated_config_dir / "sources.example.json").write_text(
        json.dumps(dict(config_mod.DEFAULT_SOURCES, host="from-example"))
    )
    (isolated_config_dir / "sources.json").write_text(
        json.dumps(dict(config_mod.DEFAULT_SOURCES, host="real-machine", bind_host="0.0.0.0"))
    )

    cfg = config_mod.load_config()

    assert cfg.sources["host"] == "real-machine"
    assert cfg.sources["bind_host"] == "0.0.0.0"


def test_load_config_env_overrides_bind_host_and_port(isolated_config_dir, monkeypatch):
    (isolated_config_dir / "sources.example.json").write_text(json.dumps(config_mod.DEFAULT_SOURCES))
    monkeypatch.setenv("CRITDASH_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("CRITDASH_BIND_PORT", "8123")

    cfg = config_mod.load_config()

    assert cfg.sources["bind_host"] == "0.0.0.0"
    assert cfg.sources["bind_port"] == 8123


def test_default_sources_are_host_agnostic():
    """The in-memory fallback must never bake in a specific machine's
    hostnames or absolute home-dir paths -- it's what ships to everyone."""
    for root in config_mod.DEFAULT_SOURCES["repo_roots"]:
        assert root.startswith("~"), root
    assert config_mod.DEFAULT_SOURCES["bind_host"] == "127.0.0.1"
    assert config_mod.DEFAULT_SOURCES["host"] == "localhost"
    assert config_mod.DEFAULT_SOURCES["hosts"] == [{"name": "localhost", "mode": "local", "enabled": True}]
