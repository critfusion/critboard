"""GET /api/settings/suggest: detected defaults the settings UI can offer.

Nothing here is applied automatically -- these are suggestions only, for a
human to accept or ignore from the settings panel. See the AGENTS.md
briefing for the exact contract.
"""

from __future__ import annotations

import os
from pathlib import Path

_MAX_CANDIDATES = 3


def suggest_human_labels(
    beads_items: list[dict],
    dispatch_routes: list[dict],
    worktrees: list[dict] | None = None,
) -> dict:
    """A label is a human_labels candidate if it:
      (a) appears on at least one OPEN (non-closed) bead,
      (b) matches no entry in the dispatch routes,
      (c) matches no worktree/repo name from a live filesystem scan
          (`worktrees[].repo`, from collectors/worktrees.py -- derived from
          directories on disk, never from bead labels), and
      (d) is never the `assignee` of any open bead carrying it -- a project
          label's beads routinely get claimed (by a dispatch route's agent,
          or by hand); a person label is a marker no route wakes and nobody
          runs `bd claim` against, so its beads stay unclaimed.

    Earlier version of (c) checked collectors/beads.py:guess_repo's `repo`
    field instead of the worktree scan. That was circular: guess_repo derives
    `repo` FROM a bead's own labels (first label that isn't a known
    intent-prefix), so any label that becomes a bead's repo guess is, by
    construction, a label carried by that same bead -- including the person
    label itself. Verified live on this host: the person label's open-bead
    count and its "used as a repo value" count were identical (44 == 44),
    because guess_repo had assigned every one of those beads its own label
    back as `repo`. That eliminated the correct answer before ranking ever
    ran. worktrees[].repo fixes this because it comes from `os.walk` over
    repo_roots (collectors/worktrees.py), never from a label.

    Ranked by open-bead count, capped at 3. Returns an empty list (never a
    guess) when nothing qualifies.
    """
    route_labels = {r.get("label") for r in dispatch_routes if r.get("label")}
    worktree_names = {
        w["repo"].lower() for w in (worktrees or []) if w.get("repo")
    }

    counts: dict[str, int] = {}
    claimed: set[str] = set()
    for item in beads_items:
        if (item.get("status") or "") == "closed":
            continue
        labels = item.get("labels") or []
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        if item.get("assignee"):
            claimed.update(labels)

    candidates = [
        (label, n) for label, n in counts.items()
        if label not in route_labels
        and label.lower() not in worktree_names
        and label not in claimed
    ]
    candidates.sort(key=lambda c: c[1], reverse=True)
    top = candidates[:_MAX_CANDIDATES]

    if not top:
        reason = (
            "no label found that appears on an open bead, matches no dispatch "
            "route, matches no worktree/repo name, and is never claimed by "
            "an assignee"
        )
    else:
        n = top[0][1]
        reason = (
            f"label appears on {n} open bead{'s' if n != 1 else ''}, "
            "matches no dispatch route, matches no worktree/repo name, and "
            "is never claimed by an assignee"
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
