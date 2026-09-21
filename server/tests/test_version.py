import time

from critdash.version import VersionTracker


def _make_tree(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("<html>v1</html>")
    (tmp_path / "web" / "js").mkdir()
    (tmp_path / "web" / "js" / "app.js").write_text("console.log(1)")
    (tmp_path / "web" / "screenshots").mkdir()
    (tmp_path / "web" / "screenshots" / "shot.png").write_bytes(b"not-really-a-png")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "layout.json").write_text("{}")
    return tmp_path


def test_build_hash_is_stable_across_calls(tmp_path):
    root = _make_tree(tmp_path)
    t1 = VersionTracker(root)
    t2 = VersionTracker(root)
    assert t1.build == t2.build
    assert len(t1.build) == 8
    assert all(c in "0123456789abcdef" for c in t1.build)


def test_build_hash_changes_when_a_file_changes(tmp_path):
    root = _make_tree(tmp_path)
    tracker = VersionTracker(root)
    before = tracker.build

    time.sleep(0.01)  # ensure mtime_ns advances on coarse-grained filesystems
    (root / "web" / "js" / "app.js").write_text("console.log(2)")

    changed = tracker.refresh(force=True)
    assert changed is True
    assert tracker.build != before


def test_refresh_is_a_noop_when_nothing_changed(tmp_path):
    root = _make_tree(tmp_path)
    tracker = VersionTracker(root)
    changed = tracker.refresh()  # cheap mtime+size precheck, no force
    assert changed is False


def test_screenshots_dir_is_excluded_from_the_hash(tmp_path):
    root = _make_tree(tmp_path)
    tracker = VersionTracker(root)
    before = tracker.build

    (root / "web" / "screenshots" / "new_shot.png").write_bytes(b"another one")
    changed = tracker.refresh(force=True)
    assert changed is False
    assert tracker.build == before


def test_dotfiles_are_excluded_from_the_hash(tmp_path):
    root = _make_tree(tmp_path)
    tracker = VersionTracker(root)
    before = tracker.build

    (root / "web" / ".DS_Store").write_bytes(b"junk")
    changed = tracker.refresh(force=True)
    assert changed is False
    assert tracker.build == before


def test_web_config_symlink_is_not_followed_or_double_counted(tmp_path):
    root = _make_tree(tmp_path)
    without_symlink_build = VersionTracker(root).build

    # web/config -> ../config, matching the real deployment layout
    (root / "web" / "config").symlink_to(root / "config", target_is_directory=True)

    # adding the symlink must not change the hash -- it must not be walked
    tracker = VersionTracker(root)
    assert tracker.build == without_symlink_build


def test_started_at_is_set_once_and_survives_refresh(tmp_path):
    root = _make_tree(tmp_path)
    tracker = VersionTracker(root)
    started = tracker.started_at
    tracker.refresh(force=True)
    assert tracker.started_at == started


def test_to_dict_shape(tmp_path):
    root = _make_tree(tmp_path)
    tracker = VersionTracker(root)
    d = tracker.to_dict()
    assert set(d.keys()) == {"build", "started_at", "commit", "branch", "dirty"}
    assert d["build"] == tracker.build
    # tmp_path is not a git checkout -- git identity degrades to None, not an error
    assert d["commit"] is None
    assert d["branch"] is None
    assert d["dirty"] is None
