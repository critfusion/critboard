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
