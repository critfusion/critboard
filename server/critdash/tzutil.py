"""Shared IANA timezone helpers.

Server-side aggregation always stays in UTC -- every stored timestamp
(usage_events.ts, kimi_turn_events.ts, etc.) is UTC and never rewritten.
Only PRESENTATION shifts by timezone: which calendar day a UTC timestamp is
displayed under. See config/layout.json's optional "timezone" key (an IANA
name, default "UTC") and GET /api/snapshot's top-level "settings" object.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "UTC"


def safe_zoneinfo(tz_name: str | None) -> ZoneInfo:
    """Never raises -- an empty/unrecognized timezone name falls back to UTC
    rather than 500ing a history query a user is actively looking at."""
    if not tz_name:
        return ZoneInfo(DEFAULT_TZ)
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(DEFAULT_TZ)


def is_valid_timezone(tz_name: str) -> bool:
    if not isinstance(tz_name, str) or not tz_name:
        return False
    try:
        ZoneInfo(tz_name)
        return True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False


__all__ = ["DEFAULT_TZ", "is_valid_timezone", "safe_zoneinfo"]
