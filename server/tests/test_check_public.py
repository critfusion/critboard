"""scripts/check-public.sh: the leak classes added with the opt-in modules
(shared-tree /srv paths, secret-manager references, internal-only hostnames)
are caught, and a clean tree passes. Runs the real script in a throwaway git
repo."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "check-public.sh"


def _run(tmp_path, content):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "check-public.sh")
    (repo / "note.txt").write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return subprocess.run(
        ["bash", "scripts/check-public.sh"], cwd=repo, capture_output=True, text=True, timeout=60, check=False,
    )


def test_clean_tree_passes(tmp_path):
    result = _run(tmp_path, "see https://wiki.example.com and 192.0.2.7 and ~/repos/example\n")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("line,reason", [
    # Built from pieces so this file does not itself trip the guard it tests.
    ("path /srv" + "/work/example", "/srv"),
    ("ref op:" + "//vault/item/field", "secret-manager"),
    ("host box." + "internal", "internal-only hostname"),
    ("host box.example-net.ts" + ".net", "internal-only hostname"),
])
def test_new_leak_classes_are_caught(tmp_path, line, reason):
    result = _run(tmp_path, line + "\n")
    assert result.returncode == 1
    assert reason in result.stdout
