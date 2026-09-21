"""Build-version tracker for the "new version available, reload" banner.

`build` is the first 8 hex chars of a sha256 over every file's relative path
plus its bytes, under `web/` and `config/`, walked in sorted order -- so it
is deterministic and changes whenever a served file changes. Skips
`web/screenshots/`, `__pycache__` dirs, dotfiles/dotdirs, and symlinks
(this specifically excludes the `web/config` symlink -- `config/` is already
hashed directly by walking it as its own root, and following the symlink
would double-count those files under a second relative path).

Recomputing the sha256 over every byte on every tick would be wasteful, so
`refresh()` first builds a cheap fingerprint from `(relpath, size, mtime_ns)`
via `stat()` only. The expensive byte-level hash only runs when that
fingerprint changes.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .update import git_identity

_HASHED_ROOTS = ("web", "config")
_PRUNE_DIR_NAMES = frozenset({"__pycache__"})


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iter_hashable_files(dashboard_root: Path):
    """Yield (relpath_posix, Path) for every file under _HASHED_ROOTS,
    pruning screenshots/__pycache__/dotfiles/symlinks along the way."""
    for root_name in _HASHED_ROOTS:
        root = dashboard_root / root_name
        if not root.is_dir() or root.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            rel_dir = Path(dirpath).relative_to(dashboard_root).as_posix()
            pruned = []
            for d in dirnames:
                if d.startswith("."):
                    continue
                if d in _PRUNE_DIR_NAMES:
                    continue
                if rel_dir == "web" and d == "screenshots":
                    continue
                if os.path.islink(os.path.join(dirpath, d)):
                    continue
                pruned.append(d)
            dirnames[:] = pruned

            for fn in filenames:
                if fn.startswith("."):
                    continue
                full = Path(dirpath) / fn
                if full.is_symlink():
                    continue
                yield full.relative_to(dashboard_root).as_posix(), full


@dataclass(frozen=True)
class _FileStamp:
    relpath: str
    size: int
    mtime_ns: int


class VersionTracker:
    def __init__(self, dashboard_root: Path):
        self.dashboard_root = Path(dashboard_root)
        self.started_at = _now_iso()
        self._fingerprint: tuple[_FileStamp, ...] | None = None
        self._build: str | None = None
        # Git identity is fetched once, not on every refresh(): unlike the
        # web/config file hash (which can change under a running process via
        # a hand-edited layout.json), the checked-out commit only changes
        # across a process restart -- which is exactly when a fresh
        # VersionTracker gets constructed, e.g. right after
        # update.apply_update()'s restart. None/None/None outside a git
        # checkout (see update.git_identity's docstring).
        self._git = git_identity(self.dashboard_root)
        self.refresh(force=True)

    def _collect_sorted_files(self) -> list[tuple[str, Path]]:
        return sorted(_iter_hashable_files(self.dashboard_root), key=lambda t: t[0])

    def _fingerprint_of(self, files: list[tuple[str, Path]]) -> tuple[_FileStamp, ...]:
        stamps = []
        for rel, full in files:
            try:
                st = full.stat()
            except OSError:
                continue
            stamps.append(_FileStamp(rel, st.st_size, st.st_mtime_ns))
        return tuple(stamps)

    def refresh(self, force: bool = False) -> bool:
        """Re-check files on disk. Returns True if `build` changed."""
        files = self._collect_sorted_files()
        fingerprint = self._fingerprint_of(files)
        if not force and fingerprint == self._fingerprint:
            return False
        self._fingerprint = fingerprint

        hasher = hashlib.sha256()
        for rel, full in files:
            hasher.update(rel.encode())
            hasher.update(b"\0")
            try:
                hasher.update(full.read_bytes())
            except OSError:
                pass
            hasher.update(b"\0")
        new_build = hasher.hexdigest()[:8]

        changed = new_build != self._build
        self._build = new_build
        return changed

    @property
    def build(self) -> str | None:
        return self._build

    def to_dict(self) -> dict:
        return {
            "build": self._build,
            "started_at": self.started_at,
            "commit": self._git["commit"],
            "branch": self._git["branch"],
            "dirty": self._git["dirty"],
        }
