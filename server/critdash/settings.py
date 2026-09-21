"""GET /api/settings/suggest: detected defaults the settings UI can offer.

Nothing here is applied automatically -- these are suggestions only, for a
human to accept or ignore from the settings panel. See the AGENTS.md
briefing for the exact contract.
"""

from __future__ import annotations

import os
from pathlib import Path

_MAX_CANDIDATES = 3


def suggest_human_labels(beads_items: list[dict], dispatch_routes: list[dict]) -> dict:
    """A label is a human_labels candidate if it:
      (a) appears on at least one OPEN (non-closed) bead,
      (b) matches no entry in the dispatch routes, and
      (c) never appears as any bead's `repo` value.
    A project label always fails (c) (every bead under that project has it as
    `repo`, per collectors/beads.py:guess_repo); a person label passes all
    three. Ranked by open-bead count, capped at 3. Returns an empty list
    (never a guess) when nothing qualifies.
    """
    route_labels = {r.get("label") for r in dispatch_routes if r.get("label")}
    repo_values = {i.get("repo") for i in beads_items if i.get("repo")}

    counts: dict[str, int] = {}
    for item in beads_items:
        if (item.get("status") or "") == "closed":
            continue
        for label in item.get("labels") or []:
            counts[label] = counts.get(label, 0) + 1

    candidates = [
        (label, n) for label, n in counts.items()
        if label not in route_labels and label not in repo_values
    ]
    candidates.sort(key=lambda c: c[1], reverse=True)
    top = candidates[:_MAX_CANDIDATES]

    if not top:
        reason = (
            "no label found that appears on an open bead, matches no dispatch "
            "route, and never appears as a bead repo"
        )
    else:
        n = top[0][1]
        reason = (
            f"label appears on {n} open bead{'s' if n != 1 else ''}, "
            "matches no dispatch route, and never appears as a bead repo"
        )
    return {"detected": [label for label, _ in top], "reason": reason}


def detect_timezone() -> tuple[str, str]:
    """Best-effort local IANA timezone name for this host. Never raises --
    falls back to ("UTC", "default") if nothing usable is found. Checked in
    order: $TZ, /etc/localtime's zoneinfo symlink target, /etc/timezone."""
    tz_env = os.environ.get("TZ")
    if tz_env:
        return tz_env, "env"
    try:
        link = os.readlink("/etc/localtime")
        marker = "zoneinfo/"
        idx = link.find(marker)
        if idx != -1:
            return link[idx + len(marker):], "system"
    except OSError:
        pass
    etc_tz = Path("/etc/timezone")
    if etc_tz.exists():
        try:
            name = etc_tz.read_text().strip()
            if name:
                return name, "system"
        except OSError:
            pass
    return "UTC", "default"


__all__ = ["detect_timezone", "suggest_human_labels"]
