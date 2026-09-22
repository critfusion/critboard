"""install.sh's own PATH-extended binary lookup (Bug 2: a non-interactive
shell on macOS often lacks Homebrew's directories on PATH even though a
tool is right there) has to duplicate critdash.detect.BINARY_CANDIDATE_DIRS
in shell, since install.sh runs before Python is confirmed to work at all.
This is the one test that keeps the two lists from drifting apart -- see
install.sh's BINARY_CANDIDATE_DIRS comment and its `--print-candidate-dirs`
debug flag.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from critdash import detect

INSTALL_SH = Path(__file__).resolve().parent.parent.parent / "install.sh"


def test_install_sh_candidate_dirs_match_detect_py():
    assert INSTALL_SH.is_file(), f"install.sh not found at {INSTALL_SH}"
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--print-candidate-dirs"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    shell_dirs = [line for line in result.stdout.splitlines() if line]
    assert shell_dirs == detect.BINARY_CANDIDATE_DIRS


def test_install_sh_python_interpreter_names_match_detect_py():
    """Same drift check, for the Python interpreter probe order -- see
    install.sh's PYTHON_INTERPRETER_NAMES comment and its
    --print-python-names debug flag, and detect.py's
    PYTHON_INTERPRETER_NAMES docstring."""
    assert INSTALL_SH.is_file(), f"install.sh not found at {INSTALL_SH}"
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--print-python-names"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    shell_names = [line for line in result.stdout.splitlines() if line]
    assert shell_names == detect.PYTHON_INTERPRETER_NAMES
