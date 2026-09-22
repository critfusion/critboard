"""critdash.detect: binary/dir/file resolution beyond one machine's
config/sources.example.json defaults. See that module's docstring for the
Mac bug (`bd` at /opt/homebrew/bin, sources.json baked to ~/.local/bin) this
exists to catch."""

from __future__ import annotations

from critdash import detect as detect_mod


def _neutralize_path(monkeypatch):
    """No real PATH lookup succeeds -- isolates a test from whatever this
    host (which may genuinely have bd/herdr/etc. installed) happens to have
    on PATH."""
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: None)


# -- detect_binary --------------------------------------------------------


def test_detect_binary_finds_non_path_candidate(tmp_path, monkeypatch):
    _neutralize_path(monkeypatch)
    candidate_dir = tmp_path / "opt-homebrew-bin"
    candidate_dir.mkdir()
    binp = candidate_dir / "bd"
    binp.write_text("#!/bin/sh\n")
    binp.chmod(0o755)

    result = detect_mod.detect_binary("bd", search_dirs=[str(candidate_dir)])

    assert result == str(binp)


def test_detect_binary_prefers_path_over_candidate_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: "/usr/bin/bd" if name == "bd" else None)
    candidate_dir = tmp_path / "bin"
    candidate_dir.mkdir()
    (candidate_dir / "bd").write_text("#!/bin/sh\n")
    (candidate_dir / "bd").chmod(0o755)

    assert detect_mod.detect_binary("bd", search_dirs=[str(candidate_dir)]) == "/usr/bin/bd"


def test_detect_binary_skips_non_executable_candidate(tmp_path, monkeypatch):
    _neutralize_path(monkeypatch)
    candidate_dir = tmp_path / "bin"
    candidate_dir.mkdir()
    binp = candidate_dir / "bd"
    binp.write_text("not executable")
    binp.chmod(0o644)

    assert detect_mod.detect_binary("bd", search_dirs=[str(candidate_dir)]) is None


def test_detect_binary_null_when_nothing_exists(tmp_path, monkeypatch):
    _neutralize_path(monkeypatch)
    assert detect_mod.detect_binary("bd", search_dirs=[str(tmp_path / "empty")]) is None


def test_binary_candidate_dirs_cover_required_locations():
    # Scope bullet: "at least" these five. This is a structural check (no
    # filesystem needed) that the list itself hasn't lost one.
    required = {
        "~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin",
        "/opt/local/bin", "/usr/bin",
    }
    assert required.issubset(set(detect_mod.BINARY_CANDIDATE_DIRS))


# -- Python interpreter discovery -------------------------------------------
#
# The reported bug: Homebrew's python formula installs the *versioned*
# binary (python3.12) into /opt/homebrew/bin without a `python3` symlink
# beside it, so a perfectly good interpreter meeting the floor was reported
# missing. These fake interpreters are tiny shell scripts that print a
# fixed version string regardless of the `-c` script they're handed --
# _python_version() only cares what the "interpreter" prints, not what it
# was told to run, which is enough to exercise the selection logic without
# needing a real second Python build.


def _fake_interpreter(path, version: str):
    path.write_text(f"#!/bin/sh\necho '{version}'\n")
    path.chmod(0o755)


def test_python_version_none_for_unrunnable_path(tmp_path):
    assert detect_mod._python_version(str(tmp_path / "does-not-exist")) is None


def test_python_version_none_for_unparseable_output(tmp_path):
    junk = tmp_path / "python3"
    junk.write_text("#!/bin/sh\necho 'not a version'\n")
    junk.chmod(0o755)
    assert detect_mod._python_version(str(junk)) is None


def test_find_python_candidates_finds_versioned_name_with_no_python3_present(tmp_path, monkeypatch):
    """The exact reported bug, at the candidate-scan layer: a directory
    with ONLY python3.12 (no python3 symlink), nothing on PATH."""
    _neutralize_path(monkeypatch)
    d = tmp_path / "opt-homebrew-bin"
    d.mkdir()
    _fake_interpreter(d / "python3.12", "3.12.4")

    candidates = detect_mod.find_python_candidates(search_dirs=[str(d)])

    assert candidates == [str(d / "python3.12")]


def test_select_python_finds_versioned_only_interpreter(tmp_path, monkeypatch):
    """End to end: select_python() resolves a version-satisfying
    interpreter that only exists under its versioned name."""
    _neutralize_path(monkeypatch)
    d = tmp_path / "opt-homebrew-bin"
    d.mkdir()
    _fake_interpreter(d / "python3.12", "3.12.4")

    result = detect_mod.select_python(search_dirs=[str(d)])

    assert result.selected is not None
    assert result.selected.path == str(d / "python3.12")
    assert result.selected.version == (3, 12, 4)
    assert result.best_below_floor is None


def test_select_python_skips_below_floor_candidate(tmp_path, monkeypatch):
    """A `python3` on PATH/candidate dir may be too old -- it must be
    skipped in favor of a versioned interpreter that meets the floor, not
    trusted just because it's named `python3`."""
    _neutralize_path(monkeypatch)
    d = tmp_path / "bin"
    d.mkdir()
    _fake_interpreter(d / "python3", "3.9.18")
    _fake_interpreter(d / "python3.12", "3.12.1")

    result = detect_mod.select_python(search_dirs=[str(d)])

    assert result.selected is not None
    assert result.selected.path == str(d / "python3.12")
    assert result.selected.version == (3, 12, 1)
    assert result.best_below_floor is not None
    assert result.best_below_floor.path == str(d / "python3")
    assert result.best_below_floor.version == (3, 9, 18)


def test_select_python_prefers_newest_not_first_name_matched(tmp_path, monkeypatch):
    """Both candidates meet the floor. PYTHON_INTERPRETER_NAMES probes
    "python3.13" before "python3.11", but the python3.11-named file here
    reports the newer real version -- select_python must still pick it,
    proving selection goes by executed version, not by which name was
    checked first."""
    _neutralize_path(monkeypatch)
    d = tmp_path / "bin"
    d.mkdir()
    _fake_interpreter(d / "python3.13", "3.11.0")
    _fake_interpreter(d / "python3.11", "3.13.0")

    result = detect_mod.select_python(search_dirs=[str(d)])

    assert result.selected is not None
    assert result.selected.path == str(d / "python3.11")
    assert result.selected.version == (3, 13, 0)


def test_select_python_reports_best_below_floor_when_nothing_qualifies(tmp_path, monkeypatch):
    _neutralize_path(monkeypatch)
    d = tmp_path / "bin"
    d.mkdir()
    _fake_interpreter(d / "python3", "3.9.18")

    result = detect_mod.select_python(search_dirs=[str(d)])

    assert result.selected is None
    assert result.best_below_floor is not None
    assert result.best_below_floor.path == str(d / "python3")
    assert result.best_below_floor.version == (3, 9, 18)


def test_select_python_none_when_nothing_found(tmp_path, monkeypatch):
    _neutralize_path(monkeypatch)
    result = detect_mod.select_python(search_dirs=[str(tmp_path / "empty")])
    assert result.selected is None
    assert result.best_below_floor is None


def test_python_interpreter_names_cover_floor_through_314():
    # Scope bullet: "at least" python3.14 down to python3.11, plus the
    # bare "python3".
    required = {"python3", "python3.14", "python3.13", "python3.12", "python3.11"}
    assert required.issubset(set(detect_mod.PYTHON_INTERPRETER_NAMES))


# -- resolve_binary ---------------------------------------------------------


def test_resolve_binary_uses_configured_value_when_it_exists(tmp_path, monkeypatch):
    _neutralize_path(monkeypatch)
    binp = tmp_path / "bd"
    binp.write_text("#!/bin/sh\n")
    binp.chmod(0o755)

    assert detect_mod.resolve_binary(str(binp), "bd") == str(binp)


def test_resolve_binary_falls_back_when_configured_path_is_wrong(tmp_path, monkeypatch):
    """The exact reported bug: configured bd_bin (Debian-style path) is
    wrong on this machine, but bd is installed elsewhere (simulating
    Homebrew) -- resolve_binary must find it."""
    _neutralize_path(monkeypatch)
    real_dir = tmp_path / "opt-homebrew-bin"
    real_dir.mkdir()
    real_bd = real_dir / "bd"
    real_bd.write_text("#!/bin/sh\n")
    real_bd.chmod(0o755)

    configured = str(tmp_path / "does-not-exist" / "bd")
    result = detect_mod.resolve_binary(configured, "bd", search_dirs=[str(real_dir)])

    assert result == str(real_bd)


def test_resolve_binary_null_when_nothing_exists_anywhere(tmp_path, monkeypatch):
    _neutralize_path(monkeypatch)
    configured = str(tmp_path / "does-not-exist" / "bd")
    assert detect_mod.resolve_binary(configured, "bd", search_dirs=[str(tmp_path / "empty")]) is None


def test_resolve_binary_bare_name_via_path(monkeypatch):
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: "/usr/bin/bd" if name == "bd" else None)
    assert detect_mod.resolve_binary("bd", "bd") == "/usr/bin/bd"


# -- detect_dir / macOS candidates, injected platform + fake filesystem -----


def test_detect_dir_uses_configured_when_it_exists(tmp_path):
    d = tmp_path / "overlord"
    d.mkdir()
    assert detect_mod.detect_dir(str(d), None, "overlord_dir") == str(d)


def test_detect_dir_falls_back_to_default(tmp_path):
    default_dir = tmp_path / "default-kimi"
    default_dir.mkdir()
    missing_configured = str(tmp_path / "no-such-configured-dir")
    assert detect_mod.detect_dir(missing_configured, str(default_dir), "kimi_dir") == str(default_dir)


def test_detect_dir_null_when_nothing_exists(tmp_path):
    assert detect_mod.detect_dir(
        str(tmp_path / "a"), str(tmp_path / "b"), "kimi_dir", platform_name="Linux"
    ) is None


def test_detect_dir_macos_candidate_used_only_on_darwin(tmp_path, monkeypatch):
    """macOS-style candidate, via injected platform and a fake filesystem:
    an Application Support directory exists (faked under a tmp HOME), and
    is only tried when platform_name is "Darwin" -- never on Linux, even
    though the directory really is there, since nothing in this codebase
    has confirmed any of these tools actually use that location (see
    detect.py's DATA_DIR_MACOS_CANDIDATES docstring)."""
    fake_home = tmp_path / "fake-home"
    (fake_home / "Library" / "Application Support" / "Kimi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    missing_configured = str(fake_home / "no-such-kimi-dir")
    missing_default = str(fake_home / "no-such-default-either")

    darwin_result = detect_mod.detect_dir(
        missing_configured, missing_default, "kimi_dir", platform_name="Darwin"
    )
    linux_result = detect_mod.detect_dir(
        missing_configured, missing_default, "kimi_dir", platform_name="Linux"
    )

    assert darwin_result == str(fake_home / "Library" / "Application Support" / "Kimi")
    assert linux_result is None


def test_current_platform_wraps_platform_system(monkeypatch):
    monkeypatch.setattr(detect_mod.platform, "system", lambda: "Darwin")
    assert detect_mod.current_platform() == "Darwin"


# -- detect_file --------------------------------------------------------


def test_detect_file_uses_configured_when_it_exists(tmp_path):
    f = tmp_path / "auth.json"
    f.write_text("{}")
    assert detect_mod.detect_file(str(f)) == str(f)


def test_detect_file_null_when_nothing_exists(tmp_path):
    assert detect_mod.detect_file(str(tmp_path / "no-such-file.json")) is None


def test_detect_file_macos_candidate_only_on_darwin(tmp_path):
    macos_path = tmp_path / "macos-auth.json"
    macos_path.write_text("{}")
    missing_configured = str(tmp_path / "no-such-configured.json")

    assert detect_mod.detect_file(
        missing_configured, macos_candidates=[str(macos_path)], platform_name="Darwin"
    ) == str(macos_path)
    assert detect_mod.detect_file(
        missing_configured, macos_candidates=[str(macos_path)], platform_name="Linux"
    ) is None
