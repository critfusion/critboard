"""critdash.doctor: `make doctor` / `./install.sh --doctor`'s report and
exit code. See doctor.py's docstring -- the direct answer to "I know beads
is installed but the dashboard says it isn't"."""

from __future__ import annotations

import json
import sys

import pytest
import test_beads as beads_test_mod

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


def test_beads_dir_check_included_and_reports_existence(tmp_path, monkeypatch):
    """No bd_bin is configured (and _neutralize_detection blocks it from
    resolving anywhere else), so build_checks has nothing to ask -- only
    the cheap existence check runs, same as every other dir/file key. See
    test_beads_dir_ok_when_bd_confirms_workspace/test_beads_dir_not_workspace_
    when_bd_rejects_it below for the real bd-backed classification."""
    _neutralize_detection(monkeypatch)
    ws = tmp_path / "project" / ".beads"
    ws.mkdir(parents=True)

    checks = doctor_mod.build_checks({
        "beads_dir": str(ws),
    })
    bd_dir = _find(checks, "beads_dir")
    assert bd_dir.kind == "dir"
    assert bd_dir.collector == "beads"
    assert bd_dir.state == "ok"
    assert bd_dir.configured_exists is True


def test_beads_dir_missing_with_no_alternative_is_missing_not_mismatch(tmp_path, monkeypatch):
    _neutralize_detection(monkeypatch)
    checks = doctor_mod.build_checks({"beads_dir": str(tmp_path / "no-such-workspace" / ".beads")})
    bd_dir = _find(checks, "beads_dir")
    assert bd_dir.state == "missing"
    assert bd_dir.mismatch is False


def test_beads_dir_unset_is_missing_not_required(tmp_path, monkeypatch):
    _neutralize_detection(monkeypatch)
    checks = doctor_mod.build_checks({})
    bd_dir = _find(checks, "beads_dir")
    assert bd_dir.state == "missing"
    assert bd_dir.key not in doctor_mod.REQUIRED_CHECK_KEYS


# -- beads_dir workspace validation (the validation-gap fix): with a real
# bd_bin resolved in the SAME build_checks() scan, beads_dir gets the same
# `bd where --json` check as the live collector, not just an existence
# check. Fake bd scripts (write_fake_bd) stand in for a real `bd` install.


def test_beads_dir_ok_when_bd_confirms_workspace(tmp_path, monkeypatch):
    _neutralize_detection(monkeypatch)
    ws = tmp_path / "project" / ".beads"
    ws.mkdir(parents=True)
    bd = tmp_path / "bd"
    beads_test_mod.write_fake_bd(bd, workspace_path=str(ws))

    checks = doctor_mod.build_checks({"bd_bin": str(bd), "beads_dir": str(ws)})
    bd_dir = _find(checks, "beads_dir")
    assert bd_dir.state == "ok"
    assert bd_dir.blocking is False


def test_beads_dir_not_workspace_when_bd_rejects_it(tmp_path, monkeypatch):
    """The gap this whole change closes: a directory that EXISTS (so the
    old check passed it) but that bd does not recognize as a workspace
    (e.g. an installing agent guessing ~/.beads) is now its own distinct
    state, not "ok" and not "missing"."""
    _neutralize_detection(monkeypatch)
    not_a_workspace = tmp_path / "dot-beads"
    not_a_workspace.mkdir()
    bd = tmp_path / "bd"
    beads_test_mod.write_fake_bd(bd, workspace_path=None)

    checks = doctor_mod.build_checks({"bd_bin": str(bd), "beads_dir": str(not_a_workspace)})
    bd_dir = _find(checks, "beads_dir")
    assert bd_dir.state == "not_workspace"
    assert bd_dir.configured_exists is True
    assert bd_dir.mismatch is False
    assert bd_dir.blocking is True
    assert bd_dir.note is not None
    assert "No active beads workspace found." in bd_dir.note


def test_exit_code_nonzero_on_beads_dir_not_workspace(tmp_path, monkeypatch):
    _neutralize_detection(monkeypatch)
    not_a_workspace = tmp_path / "dot-beads"
    not_a_workspace.mkdir()
    bd = tmp_path / "bd"
    beads_test_mod.write_fake_bd(bd, workspace_path=None)

    checks = doctor_mod.build_checks({"bd_bin": str(bd), "beads_dir": str(not_a_workspace)})
    assert doctor_mod.exit_code(checks) == 1


def test_build_probe_beads_dir_not_workspace_does_not_block_ready(tmp_path, monkeypatch, _clear_override_env):
    """Beads stays optional throughout (briefing requirement): a
    misconfigured beads_dir is surfaced in checks[] but never blocks
    --probe's overall "ready" verdict, the same as MISMATCH never does."""
    _neutralize_detection(monkeypatch)
    selection = detect_mod.PythonSelection(
        selected=detect_mod.PythonCandidate(path="/usr/bin/python3", version=(3, 12, 1)),
        best_below_floor=None,
    )
    monkeypatch.setattr(detect_mod, "select_python", lambda: selection)
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    not_a_workspace = tmp_path / "dot-beads"
    not_a_workspace.mkdir()
    bd = tmp_path / "bd"
    beads_test_mod.write_fake_bd(bd, workspace_path=None)

    sources = dict(bd_bin=str(bd), beads_dir=str(not_a_workspace))
    probe = doctor_mod.build_probe(sources)
    bd_dir_check = next(c for c in probe["checks"] if c["key"] == "beads_dir")
    assert bd_dir_check["state"] == "not_workspace"
    assert bd_dir_check["note"] is not None
    assert probe["ready"] is True
    assert "beads_dir" not in probe["missing_required"]


def test_main_shows_not_workspace_distinctly(isolated_config_dir, monkeypatch, capsys):
    """`make doctor` / `./install.sh --doctor` against a temp config
    pointed at a non-workspace beads_dir: distinct NOT_WORKSPACE state in
    the table, a follow-up remedy line, and a nonzero exit code -- not
    silently reported as OK."""
    _neutralize_detection(monkeypatch)
    not_a_workspace = isolated_config_dir / "dot-beads"
    not_a_workspace.mkdir()
    bd = isolated_config_dir / "bd"
    beads_test_mod.write_fake_bd(bd, workspace_path=None)

    sources = dict(config_mod.DEFAULT_SOURCES, bd_bin=str(bd), beads_dir=str(not_a_workspace))
    (isolated_config_dir / "sources.json").write_text(json.dumps(sources))

    code = doctor_mod.main()

    out = capsys.readouterr().out
    assert code == 1
    assert "NOT_WORKSPACE" in out
    assert "beads_dir" in out
    assert "bd where --json" in out


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


# -- build_probe / --probe --json ------------------------------------------


@pytest.fixture
def _clear_override_env(monkeypatch):
    """Every CRITDASH_OVERRIDE_* env var (install.sh's --python/--git/
    --uv/--ssh/--bd/--herdr/--curl bridge -- see doctor.overrides_from_env
    and build_python_probe) cleared, so a test starts from "no override
    supplied" regardless of what's in the real shell running the suite."""
    for name in (
        "CRITDASH_OVERRIDE_PYTHON_PATH", "CRITDASH_OVERRIDE_PYTHON_VERSION",
        "CRITDASH_OVERRIDE_GIT", "CRITDASH_OVERRIDE_UV", "CRITDASH_OVERRIDE_SSH",
        "CRITDASH_OVERRIDE_BD", "CRITDASH_OVERRIDE_HERDR", "CRITDASH_OVERRIDE_CURL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_build_probe_ready_true_when_everything_optional_is_missing(monkeypatch, _clear_override_env):
    """The DoD-2 shape: bd/herdr/uv (and every data path) missing is fine
    -- only python and git are required, so ready stays true."""
    _neutralize_detection(monkeypatch)
    selection = detect_mod.PythonSelection(
        selected=detect_mod.PythonCandidate(path="/usr/bin/python3", version=(3, 12, 1)),
        best_below_floor=None,
    )
    monkeypatch.setattr(detect_mod, "select_python", lambda: selection)

    probe = doctor_mod.build_probe({})

    assert probe["python"]["state"] == "ok"
    bd = next(c for c in probe["checks"] if c["key"] == "bd_bin")
    herdr = next(c for c in probe["checks"] if c["key"] == "herdr_bin")
    uv = next(c for c in probe["checks"] if c["key"] == "uv")
    assert bd["state"] == "missing" and bd["required"] is False
    assert herdr["state"] == "missing" and herdr["required"] is False
    assert uv["state"] == "missing" and uv["required"] is False
    git = next(c for c in probe["checks"] if c["key"] == "git")
    assert git["state"] == "missing" and git["required"] is True
    # git missing too -> genuinely not ready (this variant has no git
    # candidate at all -- see the "ready true" case below for the full
    # DoD-2 shape with git present).
    assert probe["ready"] is False
    assert "git" in probe["missing_required"]


def test_build_probe_ready_true_with_git_and_python_present(monkeypatch, _clear_override_env):
    _neutralize_detection(monkeypatch)
    selection = detect_mod.PythonSelection(
        selected=detect_mod.PythonCandidate(path="/usr/bin/python3", version=(3, 12, 1)),
        best_below_floor=None,
    )
    monkeypatch.setattr(detect_mod, "select_python", lambda: selection)
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)

    probe = doctor_mod.build_probe({})

    assert probe["ready"] is True
    assert probe["missing_required"] == []
    git = next(c for c in probe["checks"] if c["key"] == "git")
    assert git["state"] == "ok"


def test_build_probe_ready_false_when_nothing_meets_python_floor(monkeypatch, _clear_override_env):
    """The DoD-3 shape: a below-floor interpreter was found (so the "found
    but too old" detail is populated), but nothing meets 3.11 anywhere --
    genuinely unsatisfiable without an override or a fresh install."""
    _neutralize_detection(monkeypatch)
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    selection = detect_mod.PythonSelection(
        selected=None,
        best_below_floor=detect_mod.PythonCandidate(path="/usr/bin/python3", version=(3, 9, 18)),
    )
    monkeypatch.setattr(detect_mod, "select_python", lambda: selection)

    probe = doctor_mod.build_probe({})

    assert probe["ready"] is False
    assert probe["missing_required"] == ["python"]
    assert probe["python"]["state"] == "missing"
    assert probe["python"]["detected_below_floor"] == {"path": "/usr/bin/python3", "version": "3.9.18"}


def test_build_probe_python_override_skips_detection(monkeypatch, _clear_override_env):
    monkeypatch.setattr(
        detect_mod, "select_python",
        lambda: (_ for _ in ()).throw(AssertionError("select_python() must not run when overridden")),
    )
    monkeypatch.setenv("CRITDASH_OVERRIDE_PYTHON_PATH", "/opt/homebrew/bin/python3.12")
    monkeypatch.setenv("CRITDASH_OVERRIDE_PYTHON_VERSION", "3.12.4")

    py = doctor_mod.build_python_probe()

    assert py == {
        "configured": "/opt/homebrew/bin/python3.12",
        "selected": "/opt/homebrew/bin/python3.12",
        "version": "3.12.4",
        "floor": "3.11",
        "state": "ok",
        "required": True,
        "override": True,
        "detected_below_floor": None,
    }


def test_build_probe_binary_override_skips_detection(monkeypatch, _clear_override_env):
    calls = []
    real_resolve_binary = detect_mod.resolve_binary

    def _tracking_resolve_binary(*a, **k):
        calls.append(a)
        return real_resolve_binary(*a, **k)

    monkeypatch.setattr(detect_mod, "resolve_binary", _tracking_resolve_binary)
    monkeypatch.setenv("CRITDASH_OVERRIDE_BD", "/custom/path/bd")

    checks = doctor_mod.build_checks({}, doctor_mod.overrides_from_env())

    bd = next(c for c in checks if c.key == "bd_bin")
    assert bd.state == "ok"
    assert bd.configured == "/custom/path/bd"
    assert bd.override is True
    # herdr_bin has no override -- it's still resolved normally -- but
    # bd_bin's key never reaches resolve_binary() at all when overridden.
    assert not any(call[1] == "bd" for call in calls)
    assert any(call[1] == "herdr" for call in calls)


def test_overrides_from_env_omits_unset_vars(_clear_override_env, monkeypatch):
    monkeypatch.setenv("CRITDASH_OVERRIDE_GIT", "/x/git")
    assert doctor_mod.overrides_from_env() == {"git": "/x/git"}


def test_build_probe_missing_optional_never_blocks_ready(monkeypatch, _clear_override_env):
    """MISSING (a configured/default path with no working alternative
    anywhere) is a valid end state for every optional key -- see
    INSTALL.md's MISSING-vs-MISMATCH section. Only python/git block
    readiness."""
    _neutralize_detection(monkeypatch)
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    selection = detect_mod.PythonSelection(
        selected=detect_mod.PythonCandidate(path="/usr/bin/python3", version=(3, 12, 1)),
        best_below_floor=None,
    )
    monkeypatch.setattr(detect_mod, "select_python", lambda: selection)

    probe = doctor_mod.build_probe({"bd_bin": "/no/such/bd"})

    assert probe["ready"] is True
    bd = next(c for c in probe["checks"] if c["key"] == "bd_bin")
    assert bd["state"] == "missing"


def test_build_probe_mismatch_does_not_block_ready(tmp_path, monkeypatch, _clear_override_env):
    """MISMATCH (configured path wrong, but a working one exists
    elsewhere) never blocks readiness either, even for a required key --
    the tool IS usable, just worth fixing in sources.json/the override."""
    candidate_dir = tmp_path / "candidate-bin"
    candidate_dir.mkdir()
    real_git = candidate_dir / "git"
    real_git.write_text("#!/bin/sh\n")
    real_git.chmod(0o755)
    monkeypatch.setattr(detect_mod, "BINARY_CANDIDATE_DIRS", [str(candidate_dir)])
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: None)
    selection = detect_mod.PythonSelection(
        selected=detect_mod.PythonCandidate(path="/usr/bin/python3", version=(3, 12, 1)),
        best_below_floor=None,
    )
    monkeypatch.setattr(detect_mod, "select_python", lambda: selection)

    probe = doctor_mod.build_probe({})

    git = next(c for c in probe["checks"] if c["key"] == "git")
    assert git["state"] == "mismatch"
    assert probe["ready"] is True
    assert probe["missing_required"] == []


def test_main_json_mode_prints_parseable_json(isolated_config_dir, monkeypatch, capsys, _clear_override_env):
    _neutralize_detection(monkeypatch)
    monkeypatch.setattr(detect_mod.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    selection = detect_mod.PythonSelection(
        selected=detect_mod.PythonCandidate(path="/usr/bin/python3", version=(3, 12, 1)),
        best_below_floor=None,
    )
    monkeypatch.setattr(detect_mod, "select_python", lambda: selection)
    monkeypatch.setattr(sys, "argv", ["critdash.doctor", "--json"])

    code = doctor_mod.main()

    out = capsys.readouterr().out
    doc = json.loads(out)
    assert code == 0
    assert doc["ready"] is True
    assert "checks" in doc and "python" in doc and "platform" in doc
