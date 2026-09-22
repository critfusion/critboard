"""critdash.doctor: `make doctor` / `./install.sh --doctor`'s report and
exit code. See doctor.py's docstring -- the direct answer to "I know beads
is installed but the dashboard says it isn't"."""

from __future__ import annotations

import json

import pytest

from critdash import config as config_mod
from critdash import detect as detect_mod
from critdash import doctor as doctor_mod


def _neutralize_detection(monkeypatch):
    """No real PATH lookup, no real candidate directory match, and no
    DEFAULT_SOURCES fallback resolves either -- isolates a test from
    whatever this host (which genuinely has bd/herdr/git/ssh/uv installed,
    and real ~/.kimi-code / ~/.overlord directories) would otherwise be
    found by the doctor's PREREQ_BINARIES/PATH_SPECS scan."""
    monkeypatch.setattr(detect_mod, "BINARY_CANDIDATE_DIRS", [])
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor_mod, "DEFAULT_SOURCES", {})


def _find(checks, key):
    return next(c for c in checks if c.key == key)


def test_configured_binary_that_exists_is_ok(tmp_path, monkeypatch):
    _neutralize_detection(monkeypatch)
    binp = tmp_path / "bd"
    binp.write_text("#!/bin/sh\n")
    binp.chmod(0o755)

    checks = doctor_mod.build_checks({"bd_bin": str(binp)})

    bd = _find(checks, "bd_bin")
    assert bd.state == "ok"
    assert bd.configured_exists is True
    assert bd.mismatch is False


def test_missing_binary_with_no_alternative_is_missing_not_mismatch(tmp_path, monkeypatch):
    _neutralize_detection(monkeypatch)
    checks = doctor_mod.build_checks({"bd_bin": str(tmp_path / "no-such-bd")})

    bd = _find(checks, "bd_bin")
    assert bd.state == "missing"
    assert bd.mismatch is False


def test_wrong_configured_path_with_real_alternative_is_mismatch(tmp_path, monkeypatch):
    """The reported bug, in the doctor's own terms: sources.json's bd_bin
    doesn't exist, but bd IS installed at a candidate location."""
    real_dir = tmp_path / "opt-homebrew-bin"
    real_dir.mkdir()
    real_bd = real_dir / "bd"
    real_bd.write_text("#!/bin/sh\n")
    real_bd.chmod(0o755)
    monkeypatch.setattr(detect_mod, "BINARY_CANDIDATE_DIRS", [str(real_dir)])
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: None)

    checks = doctor_mod.build_checks({"bd_bin": str(tmp_path / "debian-style" / "bd")})

    bd = _find(checks, "bd_bin")
    assert bd.state == "mismatch"
    assert bd.mismatch is True
    assert bd.configured_exists is False
    assert bd.detected == str(real_bd)


def test_dir_and_file_checks_report_configured_existence(tmp_path, monkeypatch):
    _neutralize_detection(monkeypatch)
    overlord = tmp_path / "overlord"
    overlord.mkdir()
    env_file = tmp_path / "beads-env"
    env_file.write_text("")

    checks = doctor_mod.build_checks({
        "overlord_dir": str(overlord),
        "beads_env": str(env_file),
        "kimi_dir": str(tmp_path / "no-such-kimi"),
    })

    assert _find(checks, "overlord_dir").state == "ok"
    assert _find(checks, "beads_env").state == "ok"
    assert _find(checks, "kimi_dir").state == "missing"


def test_prereq_binaries_are_included(tmp_path, monkeypatch):
    _neutralize_detection(monkeypatch)
    checks = doctor_mod.build_checks({})
    keys = {c.key for c in checks}
    assert {"ssh", "git", "uv"}.issubset(keys)


def test_exit_code_zero_when_nothing_mismatched(tmp_path, monkeypatch):
    _neutralize_detection(monkeypatch)
    checks = doctor_mod.build_checks({"bd_bin": str(tmp_path / "no-such-bd")})
    assert doctor_mod.exit_code(checks) == 0


def test_exit_code_nonzero_on_mismatch(tmp_path, monkeypatch):
    real_dir = tmp_path / "opt-homebrew-bin"
    real_dir.mkdir()
    (real_dir / "bd").write_text("#!/bin/sh\n")
    (real_dir / "bd").chmod(0o755)
    monkeypatch.setattr(detect_mod, "BINARY_CANDIDATE_DIRS", [str(real_dir)])
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: None)

    checks = doctor_mod.build_checks({"bd_bin": str(tmp_path / "debian-style" / "bd")})
    assert doctor_mod.exit_code(checks) == 1


def test_format_table_lists_every_check(tmp_path, monkeypatch):
    _neutralize_detection(monkeypatch)
    checks = doctor_mod.build_checks({"bd_bin": str(tmp_path / "no-such-bd")})
    table = doctor_mod.format_table(checks)
    assert "bd_bin" in table
    assert "MISSING" in table
    for name in ("ssh", "git", "uv"):
        assert name in table


# -- main(): the CLI entry point used by `make doctor` / `install.sh --doctor` --


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setattr(config_mod, "CONFIG_DIR", d)
    for key in ("CRITDASH_BIND_HOST", "CRITDASH_BIND_PORT", "CRITDASH_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    return d


def test_main_exit_code_zero_when_configured_paths_are_real(isolated_config_dir, monkeypatch, capsys):
    _neutralize_detection(monkeypatch)
    binp = isolated_config_dir / "bd"
    binp.write_text("#!/bin/sh\n")
    binp.chmod(0o755)
    sources = dict(config_mod.DEFAULT_SOURCES, bd_bin=str(binp))
    (isolated_config_dir / "sources.json").write_text(json.dumps(sources))

    code = doctor_mod.main()

    out = capsys.readouterr().out
    assert code == 0
    assert "bd_bin" in out


# -- format_python_line: `make doctor`'s interpreter-selection line -------


def test_format_python_line_reports_selection():
    selection = detect_mod.PythonSelection(
        selected=detect_mod.PythonCandidate(path="/opt/homebrew/bin/python3.12", version=(3, 12, 4)),
        best_below_floor=None,
    )
    line = doctor_mod.format_python_line(selection)
    assert "/opt/homebrew/bin/python3.12" in line
    assert "3.12.4" in line


def test_format_python_line_reports_below_floor():
    selection = detect_mod.PythonSelection(
        selected=None,
        best_below_floor=detect_mod.PythonCandidate(path="/usr/bin/python3", version=(3, 9, 18)),
    )
    line = doctor_mod.format_python_line(selection)
    assert "BELOW FLOOR" in line
    assert "/usr/bin/python3" in line
    assert "3.9.18" in line


def test_format_python_line_reports_not_found():
    selection = detect_mod.PythonSelection(selected=None, best_below_floor=None)
    line = doctor_mod.format_python_line(selection)
    assert "NOT FOUND" in line


def test_main_prints_python_selection_line(isolated_config_dir, monkeypatch, capsys):
    _neutralize_detection(monkeypatch)
    sources = dict(config_mod.DEFAULT_SOURCES)
    (isolated_config_dir / "sources.json").write_text(json.dumps(sources))

    doctor_mod.main()

    out = capsys.readouterr().out
    assert "python3:" in out


def test_main_exit_code_nonzero_on_mismatch(isolated_config_dir, monkeypatch, capsys):
    """Simulates the owner's Mac failure end to end through the real CLI
    entry point: a temp config dir with a wrong bd_bin, a real `bd`
    reachable only via the candidate scan."""
    real_dir = isolated_config_dir / "opt-homebrew-bin"
    real_dir.mkdir()
    real_bd = real_dir / "bd"
    real_bd.write_text("#!/bin/sh\n")
    real_bd.chmod(0o755)
    monkeypatch.setattr(detect_mod, "BINARY_CANDIDATE_DIRS", [str(real_dir)])
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: None)

    sources = dict(
        config_mod.DEFAULT_SOURCES,
        bd_bin=str(isolated_config_dir / "debian-style" / "bd"),
    )
    (isolated_config_dir / "sources.json").write_text(json.dumps(sources))

    code = doctor_mod.main()

    out = capsys.readouterr().out
    assert code == 1
    assert "MISMATCH" in out
    assert "bd_bin" in out
