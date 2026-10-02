"""Shared IANA timezone helpers.

Server-side aggregation always stays in UTC -- every stored timestamp
(usage_events.ts, kimi_turn_events.ts, etc.) is UTC and never rewritten.
Only PRESENTATION shifts by timezone: which calendar day a UTC timestamp is
displayed under. See config/layout.json's optional "timezone" key (an IANA
name, default "UTC") and GET /api/snapshot's top-level "settings" object.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from .config import Config

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


def read_layout_timezone(config: Config) -> str:
    """The "timezone" key in config/layout.json (IANA name), defaulting to
    UTC. Read fresh on every call (not cached) so a POST /api/config/layout
    that changes it takes effect immediately, no restart needed. Any
    problem reading/parsing the file, or an unrecognized zone name, falls
    back to UTC rather than 500ing -- this is presentation-only; it must
    never break a history query or a collector tick."""
    try:
        with config.layout_path.open() as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_TZ
    if not isinstance(doc, dict):
        return DEFAULT_TZ
    tz = doc.get("timezone")
    if isinstance(tz, str) and is_valid_timezone(tz):
        return tz
    return DEFAULT_TZ


def local_day_start_utc_iso(now_utc: datetime, tz_name: str | None) -> str:
    """Start of the calendar day containing `now_utc` in `tz_name`, as the UTC
    "%Y-%m-%dT%H:%M:%SZ" string the usage store compares against. DST-safe:
    midnight is built from the local calendar date with zoneinfo, so a
    23- or 25-hour day starts at the right instant. A missing or invalid
    zone name means UTC."""
    return local_day_start(now_utc, tz_name).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day_start(now_utc: datetime, tz_name: str | None) -> datetime:
    zone = safe_zoneinfo(tz_name)
    local = now_utc.astimezone(zone)
    return datetime(local.year, local.month, local.day, tzinfo=zone)


def ctx_tz_name(ctx) -> str:
    """The layout timezone for a collector that has an AppContext (UTC when
    it has none, e.g. a bare test collector)."""
    config = getattr(ctx, "config", None)
    if config is None or not hasattr(config, "layout_path"):
        return DEFAULT_TZ
    return read_layout_timezone(config)


__all__ = [
    "DEFAULT_TZ", "ctx_tz_name", "is_valid_timezone", "local_day_start",
    "local_day_start_utc_iso", "read_layout_timezone", "safe_zoneinfo",
]
