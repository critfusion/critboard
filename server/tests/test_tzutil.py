from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from critdash.history import parse_window
from critdash.tzutil import ctx_tz_name, local_day_start_utc_iso, read_layout_timezone


@pytest.mark.parametrize("now,tz,expected", [
    (datetime(2026, 10, 2, 0, 47, tzinfo=UTC), "America/New_York", "2026-10-01T04:00:00Z"),
    (datetime(2026, 10, 2, 0, 47, tzinfo=UTC), "UTC", "2026-10-02T00:00:00Z"),
    (datetime(2026, 3, 8, 12, 0, tzinfo=UTC), "America/New_York", "2026-03-08T05:00:00Z"),
    (datetime(2026, 11, 1, 12, 0, tzinfo=UTC), "America/New_York", "2026-11-01T04:00:00Z"),
    # a zone ahead of UTC: 23:30Z Oct 1 is already Oct 2 in Tokyo
    (datetime(2026, 10, 1, 23, 30, tzinfo=UTC), "Asia/Tokyo", "2026-10-01T15:00:00Z"),
    (datetime(2026, 10, 2, 0, 47, tzinfo=UTC), "No/Such_Zone", "2026-10-02T00:00:00Z"),
    (datetime(2026, 10, 2, 0, 47, tzinfo=UTC), None, "2026-10-02T00:00:00Z"),
])
def test_local_day_start_utc_iso(now, tz, expected):
    assert local_day_start_utc_iso(now, tz) == expected


def test_read_layout_timezone_and_ctx(tmp_path):
    layout = tmp_path / "layout.json"
    config = SimpleNamespace(layout_path=layout)
    assert read_layout_timezone(config) == "UTC"  # missing file
    layout.write_text('{"timezone": "America/New_York"}')
    assert read_layout_timezone(config) == "America/New_York"
    assert ctx_tz_name(SimpleNamespace(config=config)) == "America/New_York"
    layout.write_text('{"timezone": "Bogus"}')
    assert read_layout_timezone(config) == "UTC"
    assert ctx_tz_name(None) == "UTC"


def test_history_today_window_honors_timezone():
    now = datetime(2026, 10, 2, 0, 47, tzinfo=UTC)
    since, until = parse_window("today", now, "America/New_York")
    assert since == datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    assert until == now
    assert parse_window("today", now)[0] == datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
    assert parse_window("today", now, "Bogus/Zone")[0] == datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
