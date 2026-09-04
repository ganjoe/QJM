"""Tests for Calendar continuous arithmetic and Binance weekly anchor (Section 2.1 & Criterion 11)."""

from datetime import datetime, timezone
from chart_viewer.models.entities import Calendar
from chart_viewer.coords.calendar import (
    time_to_index,
    index_to_time,
    get_binance_weekly_anchor,
)


def test_continuous_arithmetic():
    cal = Calendar(epoch_utc=0, bar_duration_sec=300)  # 5-min calendar
    # t = 1500 -> index = 5
    assert time_to_index(1500, cal) == 5
    assert time_to_index(1501, cal) == 5
    assert time_to_index(1799, cal) == 5
    assert time_to_index(1800, cal) == 6

    t_open, t_close = index_to_time(5, cal)
    assert t_open == 1500
    assert t_close == 1800


def test_binance_weekly_anchor():
    """Verify Binance weekly candle opens Monday 00:00:00 UTC."""
    # Example: 2026-09-04 14:30:00 UTC (A Friday)
    dt_friday = datetime(2026, 9, 4, 14, 30, 0, tzinfo=timezone.utc)
    ts_friday = dt_friday.timestamp()

    # The week started on Monday: 2026-08-31 00:00:00 UTC
    anchor_ts = get_binance_weekly_anchor(ts_friday, anchor_weekday=1)
    anchor_dt = datetime.fromtimestamp(anchor_ts, tz=timezone.utc)

    assert anchor_dt.year == 2026
    assert anchor_dt.month == 8
    assert anchor_dt.day == 31
    assert anchor_dt.hour == 0
    assert anchor_dt.minute == 0
    assert anchor_dt.second == 0
    assert anchor_dt.isoweekday() == 1  # Exactly Monday!

    # A Sunday night at 23:59:59 should still belong to the same week starting that Monday
    dt_sunday = datetime(2026, 9, 6, 23, 59, 59, tzinfo=timezone.utc)
    sunday_anchor = get_binance_weekly_anchor(dt_sunday.timestamp(), anchor_weekday=1)
    assert sunday_anchor == anchor_ts

    # The next Monday at 00:00:00 should start a new weekly anchor
    dt_next_monday = datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)
    next_anchor = get_binance_weekly_anchor(dt_next_monday.timestamp(), anchor_weekday=1)
    next_dt = datetime.fromtimestamp(next_anchor, tz=timezone.utc)
    assert next_dt.day == 7
    assert next_dt.isoweekday() == 1
