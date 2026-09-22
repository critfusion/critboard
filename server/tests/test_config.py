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


def test_ensure_layout_file_seeds_from_example(isolated_config_dir):
    # Same never-overwrite bootstrap as sources.json, applied to layout.json
    # (see config.py's _ensure_config_file_from_example, shared by both).
    example = {"version": 1, "title": "CritBoard", "human_labels": [], "panels": []}
    (isolated_config_dir / "layout.example.json").write_text(json.dumps(example))

    config_mod._ensure_layout_file(isolated_config_dir)

    target = isolated_config_dir / "layout.json"
    assert target.exists()
    assert json.loads(target.read_text()) == example


def test_ensure_layout_file_never_overwrites_existing(isolated_config_dir):
    # A user's real panel arrangement/title/human_labels must survive an
    # upstream git pull that changes config/layout.example.json.
    (isolated_config_dir / "layout.example.json").write_text(
        json.dumps({"version": 1, "title": "CritBoard", "human_labels": [], "panels": []})
    )
    real_layout = {
        "version": 1,
        "title": "Real Person's Dashboard",
        "human_labels": ["realperson"],
        "panels": [{"id": "fleet"}],
    }
    (isolated_config_dir / "layout.json").write_text(json.dumps(real_layout))

    config_mod._ensure_layout_file(isolated_config_dir)

    assert json.loads((isolated_config_dir / "layout.json").read_text()) == real_layout


def test_ensure_layout_file_noop_when_example_missing(isolated_config_dir):
    config_mod._ensure_layout_file(isolated_config_dir)
    assert not (isolated_config_dir / "layout.json").exists()


def test_load_config_also_bootstraps_layout_from_example(isolated_config_dir):
    # load_config() bootstraps both sources.json and layout.json on first
    # run -- the layout dashboard's start path (see config.py's load_config)
    # not just install.sh.
    (isolated_config_dir / "sources.example.json").write_text(json.dumps(config_mod.DEFAULT_SOURCES))
    example_layout = {"version": 1, "title": "CritBoard", "human_labels": [], "panels": []}
    (isolated_config_dir / "layout.example.json").write_text(json.dumps(example_layout))

    config_mod.load_config()

    target = isolated_config_dir / "layout.json"
    assert target.exists()
    assert json.loads(target.read_text()) == example_layout


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


def test_load_config_create_false_does_not_bootstrap_files(isolated_config_dir):
    """critdash.doctor (make doctor / install.sh --doctor / --probe) must
    be side-effect-free even against a fresh clone that has neither
    config/sources.json nor config/layout.json yet -- create=False reads
    (falling back to DEFAULT_SOURCES) without ever writing either file."""
    (isolated_config_dir / "sources.example.json").write_text(
        json.dumps(dict(config_mod.DEFAULT_SOURCES, host="from-example"))
    )

    cfg = config_mod.load_config(create=False)

    assert not (isolated_config_dir / "sources.json").exists()
    assert not (isolated_config_dir / "layout.json").exists()
    # Still usable: falls back to DEFAULT_SOURCES (mirroring _load_json's
    # own missing-file fallback) rather than erroring.
    assert cfg.sources["bind_port"] == 9999


def test_load_config_create_false_still_reads_existing_sources(isolated_config_dir):
    (isolated_config_dir / "sources.json").write_text(
        json.dumps(dict(config_mod.DEFAULT_SOURCES, host="real-machine"))
    )

    cfg = config_mod.load_config(create=False)

    assert cfg.sources["host"] == "real-machine"
    assert not (isolated_config_dir / "layout.json").exists()


def test_load_config_env_overrides_bind_host_and_port(isolated_config_dir, monkeypatch):
    (isolated_config_dir / "sources.example.json").write_text(json.dumps(config_mod.DEFAULT_SOURCES))
    monkeypatch.setenv("CRITDASH_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("CRITDASH_BIND_PORT", "8123")

    cfg = config_mod.load_config()

    assert cfg.sources["bind_host"] == "0.0.0.0"
    assert cfg.sources["bind_port"] == 8123


def test_refresh_multiplier_default_is_normal_1x(isolated_config_dir):
    (isolated_config_dir / "sources.json").write_text(json.dumps(config_mod.DEFAULT_SOURCES))
    cfg = config_mod.load_config()
    assert cfg.refresh_multiplier() == 1.0
    assert cfg.interval("beads") == config_mod.DEFAULT_SOURCES["intervals_s"]["beads"]


def test_refresh_multiplier_relaxed_is_3x(isolated_config_dir):
    (isolated_config_dir / "sources.json").write_text(
        json.dumps(dict(config_mod.DEFAULT_SOURCES, refresh_preset="relaxed"))
    )
    cfg = config_mod.load_config()
    assert cfg.refresh_multiplier() == 3.0
    assert cfg.interval("beads") == config_mod.DEFAULT_SOURCES["intervals_s"]["beads"] * 3
    assert cfg.interval("usage") == config_mod.DEFAULT_SOURCES["intervals_s"]["usage"] * 3


def test_refresh_multiplier_applies_to_remote_interval(isolated_config_dir):
    (isolated_config_dir / "sources.json").write_text(
        json.dumps(dict(config_mod.DEFAULT_SOURCES, refresh_preset="relaxed", remote_interval_s=120))
    )
    cfg = config_mod.load_config()
    assert cfg.remote_interval() == 360.0


def test_refresh_multiplier_unknown_preset_falls_back_to_1x(isolated_config_dir):
    # A typo'd preset must not silently slow every collector to a stop.
    (isolated_config_dir / "sources.json").write_text(
        json.dumps(dict(config_mod.DEFAULT_SOURCES, refresh_preset="turbo"))
    )
    cfg = config_mod.load_config()
    assert cfg.refresh_multiplier() == 1.0


def test_env_interval_override_still_wins_over_relaxed_preset(isolated_config_dir, monkeypatch):
    (isolated_config_dir / "sources.json").write_text(
        json.dumps(dict(config_mod.DEFAULT_SOURCES, refresh_preset="relaxed"))
    )
    monkeypatch.setenv("CRITDASH_INTERVAL_BEADS", "7")
    cfg = config_mod.load_config()
    assert cfg.interval("beads") == 7.0


def test_default_sources_are_host_agnostic():
    """The in-memory fallback must never bake in a specific machine's
    hostnames or absolute home-dir paths -- it's what ships to everyone."""
    for root in config_mod.DEFAULT_SOURCES["repo_roots"]:
        assert root.startswith("~"), root
    assert config_mod.DEFAULT_SOURCES["bind_host"] == "127.0.0.1"
    assert config_mod.DEFAULT_SOURCES["host"] == "localhost"
    assert config_mod.DEFAULT_SOURCES["hosts"] == [{"name": "localhost", "mode": "local", "enabled": True}]


def test_default_sources_disk_mounts_no_srv():
    # Bug 1: no more hardcoded "/srv" -- just "/" by default.
    assert config_mod.DEFAULT_SOURCES["disk_mounts"] == ["/"]


def test_default_sources_collectors_empty_means_auto_detect():
    assert config_mod.DEFAULT_SOURCES["collectors"] == {}


def test_default_sources_update_repo_is_empty():
    # Bug 3: the upstream repo is private -- a fork must opt in explicitly.
    assert config_mod.DEFAULT_SOURCES["update_repo"] == ""
