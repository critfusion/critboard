"""Locate real tool binaries and data paths, instead of trusting whatever
single machine's absolute paths config/sources.example.json happens to ship.

Driven by a real failure: the dashboard was installed on a Mac where beads
IS installed (Homebrew puts `bd` at /opt/homebrew/bin/bd on Apple Silicon,
or /usr/local/bin/bd on Intel), but config/sources.example.json hardcoded
`~/.local/bin/bd` -- the path used to seed this repo on a Debian host -- so
the dashboard reported "beads not configured" for a perfectly good install.
Every absolute-path default in config/sources.example.json has the same
failure mode; this module is the one place that knows how to go looking for
the real location instead of trusting the baked-in guess.

Used from three places:
  1. install.sh (via `python -m critdash.doctor`, see that module) writes
     resolved absolute paths into a freshly generated config/sources.json.
  2. `make doctor` / `./install.sh --doctor` (critdash/doctor.py) reports
     configured vs. detected for every tool/path, so a mismatch -- exactly
     the Mac bug above -- is one command away from obvious.
  3. main.py resolves `bd_bin` through resolve_binary() on every startup
     AND on every periodic redetect tick (see collector_redetect_loop), so
     a tool installed somewhere other than the configured path is found
     live, not just at install time.

Nothing here is guessed: every candidate is checked against the real
filesystem (or `shutil.which`, itself a real PATH search) before being
returned. A path this module cannot confirm is reported as not found
(None), never assumed to be right.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

# Binary search order, tried only after `shutil.which(name)` (PATH) has
# already failed. Covers every install convention seen in practice:
#   ~/.local/bin      -- pipx/cargo/go install, and this project's own
#                         installer's convention on Linux.
#   /opt/homebrew/bin -- Apple Silicon Homebrew.
#   /usr/local/bin    -- Intel Homebrew, and the traditional "make install"
#                         location on Linux too.
#   /opt/local/bin    -- MacPorts.
#   /usr/bin          -- distro packages.
# Checked unconditionally on every platform -- probing a directory that
# doesn't exist on this host is a cheap no-op (one failed stat), and it
# means detection is correct even when the code resolving it doesn't know
# or care what platform it's running on.
BINARY_CANDIDATE_DIRS: list[str] = [
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/usr/bin",
]

# macOS-only candidates for data directories that MIGHT follow the
# platform's ~/Library/Application Support convention instead of the
# dotfile/XDG-style default every one of these tools is documented (or, for
# overlord_dir, known first-hand) to use on every platform. NONE of these
# are confirmed to exist in practice -- this module has not been run on
# real macOS hardware. They are probed defensively (checked, never assumed)
# purely because a filesystem check costs nothing; see doctor's report and
# INSTALL.md for which of these were ever actually found versus only
# speculatively searched for.
DATA_DIR_MACOS_CANDIDATES: dict[str, list[str]] = {
    "kimi_dir": [
        "~/Library/Application Support/Kimi",
        "~/Library/Application Support/kimi-code",
    ],
    "overlord_dir": ["~/Library/Application Support/Overlord"],
    "claude_projects_dir": ["~/Library/Application Support/Claude/projects"],
}


def current_platform() -> str:
    """platform.system(): "Linux", "Darwin" (macOS), "Windows". Wrapped in
    a function (rather than called inline everywhere) so tests can inject a
    platform_name to every detect_* function below without monkeypatching
    the stdlib `platform` module."""
    return platform.system()


def detect_binary(name: str, search_dirs: list[str] | None = None) -> str | None:
    """Find an executable named `name`: PATH first (shutil.which), then
    search_dirs (default BINARY_CANDIDATE_DIRS) in priority order. Returns
    the first absolute path that exists and is executable, or None if
    nothing does -- never a guess. `search_dirs` is a full override (not
    appended), letting tests point this at a throwaway directory tree
    instead of the real BINARY_CANDIDATE_DIRS."""
    found = shutil.which(name)
    if found:
        return found
    for d in BINARY_CANDIDATE_DIRS if search_dirs is None else search_dirs:
        candidate = Path(d).expanduser() / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def resolve_binary(
    configured: str | None, name: str, search_dirs: list[str] | None = None
) -> str | None:
    """Resolve a sources.json binary value the way a live collector needs
    it resolved: try the CONFIGURED value first exactly as before (a bare
    name via PATH, anything with a "/" checked directly after expanding
    "~"), and only if that fails fall back to the full detect_binary()
    candidate scan for `name`. This is what finds `bd` on a Mac where
    sources.json still has the Debian install path baked in. Callers that
    want live self-healing (main.py's collector_redetect_loop) call this
    fresh on every tick rather than caching the result."""
    if configured:
        if "/" in configured:
            p = Path(configured).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
        else:
            found = shutil.which(configured)
            if found:
                return found
    return detect_binary(name, search_dirs)


def detect_dir(
    configured: str | None,
    default: str | None,
    key: str,
    platform_name: str | None = None,
) -> str | None:
    """Resolve a data-directory config value: try `configured`, then
    `default` (in case configured points somewhere unusual but the
    documented default is actually right), then -- only when
    `platform_name` (default: current_platform()) is "Darwin" -- any
    macOS-only candidate DATA_DIR_MACOS_CANDIDATES knows for `key` (see
    that dict's module-level note: probed, never assumed correct). Returns
    the first candidate that is an existing directory, or None."""
    platform_name = current_platform() if platform_name is None else platform_name
    candidates: list[str] = []
    for c in (configured, default):
        if c and c not in candidates:
            candidates.append(c)
    if platform_name == "Darwin":
        for c in DATA_DIR_MACOS_CANDIDATES.get(key, []):
            if c not in candidates:
                candidates.append(c)
    for c in candidates:
        p = Path(c).expanduser()
        if p.is_dir():
            return str(p)
    return None


def detect_file(
    configured: str | None,
    macos_candidates: list[str] | None = None,
    platform_name: str | None = None,
) -> str | None:
    """Same idea as detect_dir but for a single credential/env file
    (beads_env, opencode_auth_path, grok_auth_path, codex_auth_path). No
    macOS-specific location is confirmed for any of these today -- see
    INSTALL.md's config reference table -- so `macos_candidates` is
    normally omitted/empty; the parameter exists so a confirmed one can be
    added later without changing call sites. Returns the first candidate
    that is an existing file, or None."""
    platform_name = current_platform() if platform_name is None else platform_name
    candidates = [configured] if configured else []
    if platform_name == "Darwin":
        candidates.extend(macos_candidates or [])
    for c in candidates:
        p = Path(c).expanduser()
        if p.is_file():
            return str(p)
    return None
