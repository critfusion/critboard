"""`make doctor` / `./install.sh --doctor` -- the direct answer to "I know
beads is installed but the dashboard says it isn't".

For every configurable tool/data path this dashboard uses, prints the
configured value, whether it exists, what critdash.detect finds instead if
that differs, and the resulting collector state. A configured path that is
missing while detect.py found a working alternative is a MISMATCH -- the
exact shape of the Mac bug this feature exists to catch -- and makes this
command exit non-zero, so it can gate an install script or a CI step, not
just be read by a human.

A missing OPTIONAL tool with no working alternative anywhere is not an
error: that collector's panel just stays inactive, same as it always has.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import detect
from .config import DEFAULT_SOURCES, load_config

# (key, kind, collector, canonical binary name for kind == "binary")
PATH_SPECS: list[tuple[str, str, str, str | None]] = [
    ("bd_bin", "binary", "beads", "bd"),
    ("beads_env", "file", "beads", None),
    ("herdr_bin", "binary", "agents (pane detection) / remote", "herdr"),
    ("claude_projects_dir", "dir", "agents / usage / analytics", None),
    ("kimi_dir", "dir", "kimi / quota (kimi)", None),
    ("overlord_dir", "dir", "dispatch", None),
    ("opencode_auth_path", "file", "quota (openrouter / opencode / google)", None),
    ("grok_auth_path", "file", "quota (xai)", None),
    ("codex_auth_path", "file", "quota (openai)", None),
]

# Prerequisites that aren't sources.json keys at all -- always looked up by
# their canonical name, reported for completeness (install.sh already warns
# about these individually; doctor puts them in the same table).
PREREQ_BINARIES: list[tuple[str, str]] = [
    ("ssh", "remote (multi-host fleet collection)"),
    ("git", "worktrees / productivity"),
    ("uv", "dev tooling / self-update"),
]


@dataclass
class PathCheck:
    key: str
    kind: str  # "binary" | "dir" | "file"
    collector: str
    configured: str | None
    configured_exists: bool
    detected: str | None
    state: str  # "ok" | "mismatch" | "missing"

    @property
    def mismatch(self) -> bool:
        return self.state == "mismatch"


def _check_binary(key: str, collector: str, name: str, configured: str | None) -> PathCheck:
    configured_exists = bool(configured) and _binary_literally_present(configured)
    detected = detect.resolve_binary(configured, name)
    return _finish(key, "binary", collector, configured, configured_exists, detected)


def _binary_literally_present(configured: str) -> bool:
    """Whether the CONFIGURED value itself (not a candidate) resolves --
    mirrors detect.resolve_binary's own "try configured first" half,
    factored out so the doctor table can show configured_exists
    independently of whatever detect() finds overall."""
    if "/" in configured:
        p = Path(configured).expanduser()
        return p.is_file() and os.access(p, os.X_OK)
    return shutil.which(configured) is not None


def _check_dir(key: str, collector: str, configured: str | None) -> PathCheck:
    configured_exists = bool(configured) and Path(configured).expanduser().is_dir()
    detected = detect.detect_dir(configured, DEFAULT_SOURCES.get(key), key)
    return _finish(key, "dir", collector, configured, configured_exists, detected)


def _check_file(key: str, collector: str, configured: str | None) -> PathCheck:
    configured_exists = bool(configured) and Path(configured).expanduser().is_file()
    detected = detect.detect_file(configured)
    return _finish(key, "file", collector, configured, configured_exists, detected)


def _finish(
    key: str, kind: str, collector: str, configured: str | None,
    configured_exists: bool, detected: str | None,
) -> PathCheck:
    if configured_exists:
        state = "ok"
    elif detected is not None:
        state = "mismatch"
    else:
        state = "missing"
    return PathCheck(
        key=key, kind=kind, collector=collector, configured=configured,
        configured_exists=configured_exists, detected=detected, state=state,
    )


def build_checks(sources: dict) -> list[PathCheck]:
    checks: list[PathCheck] = []
    for key, kind, collector, bin_name in PATH_SPECS:
        configured = sources.get(key)
        configured = configured if isinstance(configured, str) and configured else None
        if kind == "binary":
            checks.append(_check_binary(key, collector, bin_name, configured))
        elif kind == "dir":
            checks.append(_check_dir(key, collector, configured))
        else:
            checks.append(_check_file(key, collector, configured))
    for name, collector in PREREQ_BINARIES:
        checks.append(_check_binary(name, collector, name, name))
    return checks


def exit_code(checks: list[PathCheck]) -> int:
    return 1 if any(c.mismatch for c in checks) else 0


def format_table(checks: list[PathCheck]) -> str:
    headers = ("KEY", "KIND", "CONFIGURED", "EXISTS", "DETECTED", "STATE", "COLLECTOR")
    rows = []
    for c in checks:
        rows.append((
            c.key,
            c.kind,
            c.configured or "(unset)",
            "yes" if c.configured_exists else "no",
            c.detected or "-",
            c.state.upper(),
            c.collector,
        ))
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    lines = []

    def fmt(row):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    lines.append(fmt(headers))
    lines.append("  ".join("-" * w for w in widths))
    for r in rows:
        lines.append(fmt(r))
    return "\n".join(lines)


def main() -> int:
    cfg = load_config()
    checks = build_checks(cfg.sources)
    print(format_table(checks))
    code = exit_code(checks)
    if code != 0:
        mismatched = [c.key for c in checks if c.mismatch]
        print()
        print(
            "doctor: MISMATCH -- config/sources.json has a configured path that is "
            "missing, but a working one was detected elsewhere: " + ", ".join(mismatched)
        )
        print("Update config/sources.json's \"DETECTED\" column values above to fix this.")
    return code


if __name__ == "__main__":
    sys.exit(main())
