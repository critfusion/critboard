"""dispatch collector: routes.conf, pause flags, fleet-dispatch.log tail."""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import BaseCollector, CollectorIssue

_TS_PREFIX_RE = re.compile(r"^(\S+)\s+(.*)$")

_DISPATCH_REMEDY = (
    "Create ~/.overlord (see the fleet-dispatch docs), or ignore this panel "
    "if you do not run a dispatch/overlord workflow."
)


def availability_issue(overlord_dir: Path) -> CollectorIssue | None:
    """None if `overlord_dir` exists -- a fresh install with no fleet-dispatch
    workflow at all has no ~/.overlord directory, which is a normal, expected
    state, not a failure (same "optional, auto-detected" treatment as
    beads.availability_issue). collect() itself still degrades gracefully
    (empty routes/log) if this collector is force-enabled anyway despite the
    directory being absent -- this check only decides whether main.py
    schedules it at all."""
    if not overlord_dir.exists():
        return CollectorIssue(
            "config_missing",
            f"overlord directory not found: {overlord_dir}",
            remedy=_DISPATCH_REMEDY,
            optional=True,
        )
    return None


def parse_routes_conf(text: str) -> list[dict]:
    routes = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        label, kind = parts[0], parts[1]
        precheck = parts[2] if len(parts) > 2 else None
        routes.append({"label": label, "kind": kind, "precheck": precheck})
    return routes


def tail_lines(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            pos = size
            newline_count = 0
            while pos > 0 and newline_count <= n:
                read_size = min(block, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)
                data = chunk + data
                newline_count = data.count(b"\n")
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-n:]
    except OSError:
        return []


def parse_log_line(line: str) -> dict:
    m = _TS_PREFIX_RE.match(line)
    if m:
        return {"t": m.group(1), "line": m.group(2)}
    return {"t": None, "line": line}


class DispatchCollector(BaseCollector):
    name = "dispatch"
    interval_s = 30.0

    def __init__(self, ctx=None, overlord_dir: str | None = None):
        super().__init__(ctx)
        self.overlord_dir = Path(overlord_dir or "~/.overlord").expanduser()

    async def collect(self) -> dict:
        routes_path = self.overlord_dir / "routes.conf"
        pause_dir = self.overlord_dir / "pause"
        log_path = self.overlord_dir / "fleet-dispatch.log"

        routes_text = routes_path.read_text() if routes_path.exists() else ""
        routes = parse_routes_conf(routes_text)

        paused_all = (pause_dir / "ALL").exists()
        for r in routes:
            r["paused"] = paused_all or (pause_dir / r["label"]).exists()

        recent = [parse_log_line(line) for line in tail_lines(log_path, 50)]

        return {
            "dispatch": {
                "routes": routes,
                "paused_all": paused_all,
                "recent": recent,
            }
        }
