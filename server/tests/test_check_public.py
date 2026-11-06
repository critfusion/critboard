"""scripts/check-public.sh: the leak classes added with the opt-in modules
(shared-tree /srv paths, secret-manager references, internal-only hostnames)
are caught, and a clean tree passes. Runs the real script in a throwaway git
repo."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "check-public.sh"


def _run(tmp_path, content, local_overrides=None, args=(), env=None):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "check-public.sh")
    if local_overrides is not None:
        (repo / "scripts" / "check-public.local").write_text(local_overrides)
    (repo / "note.txt").write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return subprocess.run(
        ["bash", "scripts/check-public.sh", *args], cwd=repo, env=env,
        capture_output=True, text=True, timeout=60, check=False,
    )


def _isolated_env(tmp_path, repo_roots):
    # PATH holds only the tools the script needs, so ground-truth mode cannot
    # reach `gh` or `bd` on the host and sees exactly the roots given here.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("bash", "git", "grep", "sed", "tr", "sort", "awk", "comm", "ls",
                 "mkdir", "stat", "date", "mktemp", "rm", "mv", "cat", "dirname",
                 "hostname", "uname", "find", "head", "wc", "cut", "xargs"):
        found = shutil.which(tool)
        if found:
            (bindir / tool).symlink_to(found)
    (tmp_path / "home").mkdir()
    (tmp_path / "cache").mkdir()
    # Deliberately not merged with os.environ: the real HOME and repo roots
    # must not leak into the scan.
    return {
        "PATH": str(bindir),
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "CHECK_PUBLIC_REPO_ROOTS": str(repo_roots),
    }


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


# A private name that is not in the stop-list must be flagged when it arrives
# through either documented channel. The name below is synthetic.
PRIVATE_NAME = "zorblaxsynth"


def test_name_in_local_overrides_is_flagged(tmp_path):
    result = _run(
        tmp_path, f"owner is {PRIVATE_NAME} here\n",
        local_overrides=f"{PRIVATE_NAME}|private name from local overrides\n",
    )
    assert result.returncode == 1
    assert "private name from local overrides" in result.stdout


def test_name_from_ground_truth_is_flagged(tmp_path):
    roots = tmp_path / "roots"
    (roots / PRIVATE_NAME).mkdir(parents=True)
    result = _run(
        tmp_path, f"owner is {PRIVATE_NAME} here\n", args=("--ground-truth",),
        env=_isolated_env(tmp_path, roots),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "--ground-truth mode" in result.stdout


def test_stop_list_is_the_reviewed_set():
    # Every STOP_LIST entry exempts a name from ground-truth mode, so an added
    # entry weakens the scanner. Adding one must be a deliberate edit here too.
    text = SCRIPT.read_text()
    assigns = re.findall(r"^\s*(?:declare -a )?STOP_LIST\+?=", text, re.MULTILINE)
    assert len(assigns) == 1, "STOP_LIST must be assigned exactly once (no += appends)"
    match = re.search(r"^\s*declare -a STOP_LIST=\(([^)]*)\)", text, re.MULTILINE)
    assert match, "STOP_LIST array definition not found in check-public.sh"
    assert sorted(match.group(1).split()) == sorted([
        "dashboard", "fleet", "medium", "profile", "smoke", "handoff", "critboard",
        "__pycache__", "readme.md",
        "needs-codex", "needs-grok", "needs-claude", "needs-opencode",
        "needs-cursor", "needs-kimi", "needs-review", "approved",
        "grok-review", "waiting-review",
        "security",
    ])


def test_name_exemption_filter_is_the_reviewed_set():
    # The grep -vE filter drops names from the ground-truth list before the scan.
    # A pattern added to it hides a name from the scanner, so changing it must
    # be a deliberate edit here too.
    text = SCRIPT.read_text()
    filters = re.findall(r"grep -vE '([^']*)'", text)
    assert filters == [r"^$|^\.|^wt-|^repo$"]
