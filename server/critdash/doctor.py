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

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import detect
from .collectors.beads import NO_WORKSPACE_REMEDY, check_beads_workspace, check_sync_remote, resolve_any_workspace
from .config import DEFAULT_SOURCES, load_config

# (key, kind, collector, canonical binary name for kind == "binary")
PATH_SPECS: list[tuple[str, str, str, str | None]] = [
    ("bd_bin", "binary", "beads", "bd"),
    ("beads_env", "file", "beads", None),
    ("beads_dir", "dir", "beads", None),
    ("beads_workspace", "workspace", "beads", None),
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
    ("curl", "--start healthz smoke check"),
]

# Which check keys are load-bearing for --probe's "ready" verdict (see
# build_probe below) -- everything else degrades to "that panel stays
# inactive" per this project's whole collector-availability design (see
# INSTALL.md's "MISSING is a valid end state"). "python" is handled
# separately in build_probe (it isn't a single-path PATH_SPECS/
# PREREQ_BINARIES entry -- see PythonSelection).
REQUIRED_CHECK_KEYS: frozenset[str] = frozenset({"git"})

# install.sh's --python/--git/--uv/--ssh/--bd/--herdr/--curl overrides are
# threaded through to this process as CRITDASH_OVERRIDE_* env vars (empty
# when that flag wasn't passed) -- see install.sh's run_probe(). Binary
# check key -> env var name.
_OVERRIDE_ENV_BY_KEY = {
    "git": "CRITDASH_OVERRIDE_GIT",
    "uv": "CRITDASH_OVERRIDE_UV",
    "ssh": "CRITDASH_OVERRIDE_SSH",
    "curl": "CRITDASH_OVERRIDE_CURL",
    "bd_bin": "CRITDASH_OVERRIDE_BD",
    "herdr_bin": "CRITDASH_OVERRIDE_HERDR",
}


def overrides_from_env() -> dict[str, str]:
    """{check_key: override_path} for every override actually supplied on
    this run (empty/unset env vars are omitted, not returned as "")."""
    result = {}
    for key, env_name in _OVERRIDE_ENV_BY_KEY.items():
        val = os.environ.get(env_name, "").strip()
        if val:
            result[key] = val
    return result


@dataclass
class PathCheck:
    key: str
    kind: str  # "binary" | "dir" | "file"
    collector: str
    configured: str | None
    configured_exists: bool
    detected: str | None
    state: str  # "ok" | "mismatch" | "missing" | "not_workspace" | "no_workspace"
    override: bool = False
    # Extra human-readable detail for a state the other fields don't fully
    # explain: beads_dir's "not_workspace" state (bd's own reason it
    # rejected the directory), beads_workspace's "not_workspace"/
    # "no_workspace" states (same idea, see _check_beads_workspace), and
    # beads_workspace's "ok" state when a sync.remote WARNING applies
    # (never blocking -- state stays "ok"). None everywhere else.
    note: str | None = None

    @property
    def mismatch(self) -> bool:
        return self.state == "mismatch"

    @property
    def blocking(self) -> bool:
        """Whether this check's state represents a real, fixable problem
        that should make doctor/CI fail -- mismatch (configured path is
        stale), not_workspace (beads_dir exists but bd rejects it), or
        no_workspace (beads_dir is unset AND bd resolves no workspace
        anywhere either -- the beads_workspace check, issue #3: the beads
        panel flatly cannot work, which is worth more than a quiet
        "missing"). "missing" alone never blocks: an unconfigured optional
        tool is a normal end state (see INSTALL.md's MISSING vs
        MISMATCH)."""
        return self.state in ("mismatch", "not_workspace", "no_workspace")


def _check_binary(
    key: str, collector: str, name: str, configured: str | None, override: str | None = None
) -> PathCheck:
    if override:
        # --python/--git/--uv/--ssh/--bd/--herdr/--curl: used verbatim,
        # already validated (exists + executable, see install.sh's
        # validate_binary_override) before this process ever ran --
        # detection is skipped entirely, not merely preferred.
        return PathCheck(
            key=key, kind="binary", collector=collector, configured=override,
            configured_exists=True, detected=None, state="ok", override=True,
        )
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


def _check_beads_dir(configured: str | None, bd_bin_resolved: str | None) -> PathCheck:
    """Like _check_dir, but for beads_dir specifically: existing isn't
    enough (that's the validation gap this exists to close -- see
    critdash.collectors.beads.check_beads_workspace). If the configured
    directory exists AND a real `bd` binary was resolved elsewhere in this
    same build_checks() run, ask bd itself whether it's a workspace; a
    directory that exists but that bd rejects is reported as the distinct
    "not_workspace" state, never as "ok". With no resolved bd_bin (bd isn't
    installed on this machine at all), only the cheap existence check runs
    -- there's nothing to ask."""
    configured_exists = bool(configured) and Path(configured).expanduser().is_dir()
    detected = detect.detect_dir(configured, DEFAULT_SOURCES.get("beads_dir"), "beads_dir")
    if configured_exists and bd_bin_resolved and configured:
        ok, detail = check_beads_workspace(bd_bin_resolved, str(Path(configured).expanduser()))
        if not ok:
            return PathCheck(
                key="beads_dir", kind="dir", collector="beads", configured=configured,
                configured_exists=True, detected=detected, state="not_workspace", note=detail,
            )
    return _finish("beads_dir", "dir", "beads", configured, configured_exists, detected)


def _check_beads_workspace(bd_bin_resolved: str | None, beads_dir_configured: str | None) -> PathCheck:
    """Issue #3's main gap, as its own row: "bd binary installed" is not
    "a valid workspace exists", and the plain beads_dir row (MISSING when
    unset) doesn't say which one is true. This asks bd directly whether a
    workspace resolves AT ALL on this host, distinct from validating one
    specific configured beads_dir:

    - No `bd` resolved anywhere -- MISSING; bd_bin's own row already
      explains why, so this one stays terse.
    - beads_dir IS configured -- reuses the exact same
      check_beads_workspace() call the beads_dir row makes (deliberately
      redundant with it -- this row's job is "does beads work at all",
      the beads_dir row's job is "is THIS path right").
    - beads_dir is NOT configured -- runs `bd where --json` with no
      BEADS_DIR override (resolve_any_workspace), i.e. exactly what a real
      collector run resolves today. If that finds a workspace, this is a
      genuinely OK, non-blocking state (an installing agent can copy
      `detected` straight into beads_dir) -- never "mismatch", since
      nothing was configured wrong. If it finds nothing, that's
      "no_workspace": a real, blocking problem (see PathCheck.blocking)
      with the same remedy _run()'s config_missing classification uses,
      because the beads panel flatly cannot work.

    Either way a resolved workspace exists, also checks for an inherited
    `sync.remote` (issue #3's other report -- see
    critdash.collectors.beads.check_sync_remote) and surfaces it as a
    WARNING via `note` on an otherwise-OK row -- never blocking."""
    if not bd_bin_resolved:
        return PathCheck(
            key="beads_workspace", kind="workspace", collector="beads",
            configured=None, configured_exists=False, detected=None,
            state="missing", note="bd itself is not installed -- see the bd_bin row above",
        )
    if beads_dir_configured:
        expanded = str(Path(beads_dir_configured).expanduser())
        ok, detail = check_beads_workspace(bd_bin_resolved, expanded)
        if not ok:
            return PathCheck(
                key="beads_workspace", kind="workspace", collector="beads",
                configured=beads_dir_configured, configured_exists=False,
                detected=None, state="not_workspace", note=detail,
            )
        resolved_dir = expanded
    else:
        ok, detail_or_path = resolve_any_workspace(bd_bin_resolved)
        if not ok:
            return PathCheck(
                key="beads_workspace", kind="workspace", collector="beads",
                configured=None, configured_exists=False, detected=None,
                state="no_workspace", note=detail_or_path,
            )
        resolved_dir = detail_or_path

    note = None
    remote = check_sync_remote(bd_bin_resolved, resolved_dir)
    if remote:
        note = (
            f"WARNING: this workspace has a sync.remote configured ({remote}) -- "
            "likely inherited from the checkout's git remote at `bd init` time. "
            "A local-only install should remove it: bd config unset sync.remote"
        )
    return PathCheck(
        key="beads_workspace", kind="workspace", collector="beads",
        configured=beads_dir_configured, configured_exists=bool(beads_dir_configured),
        detected=resolved_dir, state="ok", note=note,
    )


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


def build_checks(sources: dict, overrides: dict[str, str] | None = None) -> list[PathCheck]:
    overrides = overrides or {}
    checks: list[PathCheck] = []
    # Resolved bd_bin from THIS SAME scan (not a fresh detect call) -- set
    # once the "bd_bin" entry in PATH_SPECS is processed, since it always
    # comes before "beads_dir" in that list. Used by _check_beads_dir to
    # actually ask bd whether beads_dir is a real workspace; stays None
    # (workspace check skipped, existence-only) if bd itself isn't
    # resolvable anywhere on this machine.
    bd_bin_resolved: str | None = None
    # beads_dir's own configured value from THIS SAME scan -- "beads_dir"
    # always precedes "beads_workspace" in PATH_SPECS, so both are already
    # known by the time _check_beads_workspace needs them.
    beads_dir_configured: str | None = None
    for key, kind, collector, bin_name in PATH_SPECS:
        configured = sources.get(key)
        configured = configured if isinstance(configured, str) and configured else None
        if key == "beads_dir":
            beads_dir_configured = configured
            checks.append(_check_beads_dir(configured, bd_bin_resolved))
        elif key == "beads_workspace":
            checks.append(_check_beads_workspace(bd_bin_resolved, beads_dir_configured))
        elif kind == "binary":
            c = _check_binary(key, collector, bin_name, configured, overrides.get(key))
            checks.append(c)
            if key == "bd_bin":
                # Actually usable as argv[0] for a real subprocess -- unlike
                # `detected` (already an absolute path from detect.py),
                # `configured` can be the raw config string verbatim (e.g.
                # "~/.local/bin/bd"), which subprocess.run() does NOT
                # expand (that's a shell-only behaviour). Both
                # check_beads_workspace and resolve_any_workspace shell out
                # to this value directly, so it must be expanded here, not
                # left for them to discover as ENOENT.
                raw = c.configured if c.configured_exists else c.detected
                bd_bin_resolved = os.path.expanduser(raw) if raw else None
        elif kind == "dir":
            checks.append(_check_dir(key, collector, configured))
        else:
            checks.append(_check_file(key, collector, configured))
    for name, collector in PREREQ_BINARIES:
        checks.append(_check_binary(name, collector, name, name, overrides.get(name)))
    return checks


def exit_code(checks: list[PathCheck]) -> int:
    return 1 if any(c.blocking for c in checks) else 0


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


def format_python_line(selection: detect.PythonSelection) -> str:
    """One line describing which Python interpreter install.sh/doctor
    would pick right now, and why -- see detect.select_python()'s
    docstring for the selection rule (newest candidate meeting
    PYTHON_FLOOR, not merely the first name matched). Callers that want
    the CRITDASH_OVERRIDE_PYTHON_PATH override honoured (i.e. everything
    except the tests exercising this pure-formatting function directly)
    should check that env var first -- see main()."""
    floor = ".".join(str(p) for p in detect.PYTHON_FLOOR)
    if selection.selected is not None:
        c = selection.selected
        version = ".".join(str(p) for p in c.version)
        return f"python3: selected {c.path} (version {version}, floor >= {floor})"
    if selection.best_below_floor is not None:
        c = selection.best_below_floor
        version = ".".join(str(p) for p in c.version)
        return (
            f"python3: FOUND BUT BELOW FLOOR -- {c.path} is version {version}, "
            f"need >= {floor}"
        )
    tried = ", ".join(detect.PYTHON_INTERPRETER_NAMES)
    dirs = ", ".join(detect.BINARY_CANDIDATE_DIRS)
    return f"python3: NOT FOUND -- tried {tried} on PATH and in: {dirs}"


def build_python_probe(floor: str | None = None) -> dict:
    """The "python" section of --probe's JSON: which interpreter would be
    (or, with an override, already is) selected, its version, and whether
    that satisfies PYTHON_FLOOR. CRITDASH_OVERRIDE_PYTHON_PATH/_VERSION
    (set by install.sh's run_probe() only when --python was passed) are
    used verbatim when present -- an override skips detect.select_python()
    entirely, the same as every other override key, and is already known
    valid (install.sh's validate_python_override ran before this process
    was ever started)."""
    floor = floor or ".".join(str(p) for p in detect.PYTHON_FLOOR)
    override_path = os.environ.get("CRITDASH_OVERRIDE_PYTHON_PATH", "").strip()
    if override_path:
        return {
            "configured": override_path,
            "selected": override_path,
            "version": os.environ.get("CRITDASH_OVERRIDE_PYTHON_VERSION", "").strip() or None,
            "floor": floor,
            "state": "ok",
            "required": True,
            "override": True,
            "detected_below_floor": None,
        }
    selection = detect.select_python()
    if selection.selected is not None:
        c = selection.selected
        return {
            "configured": None,
            "selected": c.path,
            "version": ".".join(str(p) for p in c.version),
            "floor": floor,
            "state": "ok",
            "required": True,
            "override": False,
            "detected_below_floor": None,
        }
    below = selection.best_below_floor
    return {
        "configured": None,
        "selected": None,
        "version": None,
        "floor": floor,
        "state": "missing",
        "required": True,
        "override": False,
        "detected_below_floor": (
            {"path": below.path, "version": ".".join(str(p) for p in below.version)}
            if below is not None else None
        ),
    }


def build_probe(sources: dict) -> dict:
    """The full --probe --json payload: install.sh's --probe just prints
    this. Flat and obvious on purpose (see INSTALL.md "Probe schema" for
    the field-by-field contract an installing agent parses) -- one entry
    per tool/data path in "checks" (PATH_SPECS + PREREQ_BINARIES, the same
    set --doctor's table shows), the Python floor selection separately
    under "python" (it isn't a single configured path the way the others
    are), the platform, and one overall "ready" boolean an agent can act
    on without inspecting every row itself."""
    overrides = overrides_from_env()
    checks = build_checks(sources, overrides)
    python = build_python_probe()

    checks_json = []
    missing_required = []
    for c in checks:
        required = c.key in REQUIRED_CHECK_KEYS
        checks_json.append({
            "key": c.key,
            "kind": c.kind,
            "collector": c.collector,
            "configured": c.configured,
            "configured_exists": c.configured_exists,
            "detected": c.detected,
            "state": c.state,
            "required": required,
            "override": c.override,
            "note": c.note,
        })
        # MISSING blocks readiness only for a required key -- see
        # REQUIRED_CHECK_KEYS. MISMATCH never blocks readiness, required or
        # not: it means a working copy of the tool WAS found (just not at
        # the configured path), so the tool is usable either way -- see
        # INSTALL.md's MISSING-vs-MISMATCH section.
        if required and c.state == "missing":
            missing_required.append(c.key)
    if python["state"] == "missing":
        missing_required.append("python")

    return {
        "ready": len(missing_required) == 0,
        "platform": detect.current_platform(),
        "python": python,
        "checks": checks_json,
        "missing_required": missing_required,
    }


def main() -> int:
    args = sys.argv[1:]
    json_mode = "--json" in args
    cfg = load_config(create=False)
    overrides = overrides_from_env()

    if json_mode:
        probe = build_probe(cfg.sources)
        print(json.dumps(probe, indent=2, sort_keys=True))
        return 0 if probe["ready"] else 1

    checks = build_checks(cfg.sources, overrides)
    print(format_table(checks))
    print()
    override_python_path = os.environ.get("CRITDASH_OVERRIDE_PYTHON_PATH", "").strip()
    if override_python_path:
        override_version = os.environ.get("CRITDASH_OVERRIDE_PYTHON_VERSION", "").strip()
        floor = ".".join(str(p) for p in detect.PYTHON_FLOOR)
        print(
            f"python3: using {override_python_path} (version {override_version or '?'}, "
            f"--python override -- detection skipped, floor >= {floor})"
        )
    else:
        print(format_python_line(detect.select_python()))
    # sync.remote WARNING: an otherwise-OK beads_workspace row can still
    # carry a note (see _check_beads_workspace) -- never blocking (state
    # stays "ok"), so this prints unconditionally, not just when code != 0.
    sync_remote_warnings = [c for c in checks if c.key == "beads_workspace" and c.state == "ok" and c.note]
    for c in sync_remote_warnings:
        print()
        print(f"doctor: {c.note}")
    code = exit_code(checks)
    if code != 0:
        mismatched = [c.key for c in checks if c.mismatch]
        if mismatched:
            print()
            print(
                "doctor: MISMATCH -- config/sources.json has a configured path that is "
                "missing, but a working one was detected elsewhere: " + ", ".join(mismatched)
            )
            print("Update config/sources.json's \"DETECTED\" column values above to fix this.")
        not_workspace = [c for c in checks if c.state == "not_workspace"]
        for c in not_workspace:
            print()
            print(f"doctor: NOT_WORKSPACE -- {c.key} ({c.configured}) exists but bd does not "
                  f"recognize it as a workspace: {c.note}")
            print(
                "Run `bd where --json` from a directory where bd already works and use its "
                "\"path\" field -- that's the .beads directory ITSELF, not its parent, and not "
                "~/.beads unless that genuinely is a workspace."
            )
        no_workspace = [c for c in checks if c.state == "no_workspace"]
        for c in no_workspace:
            print()
            print(f"doctor: NO_WORKSPACE -- bd does not resolve a beads workspace anywhere on "
                  f"this host: {c.note}")
            print(NO_WORKSPACE_REMEDY)
    return code


if __name__ == "__main__":
    sys.exit(main())
