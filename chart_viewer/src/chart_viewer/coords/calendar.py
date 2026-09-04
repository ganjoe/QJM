"""Calendar arithmetic according to Section 2.1 and Section 4."""

from __future__ import annotations
import math
from datetime import datetime, timezone, timedelta
from chart_viewer.models.entities import Calendar


def time_to_index(t: int, calendar: Calendar) -> int:
    """Calculate continuous bar index from timestamp.

    index = floor((t - epoch_utc) / bar_duration_sec)
    Works for both second and millisecond timestamps depending on epoch/bar_duration.
    If t is in ms and epoch is in sec, normalizes accordingly.
    """
    epoch = calendar.epoch_utc
    duration = calendar.bar_duration_sec
    # Normalize if t is in epoch ms (e.g. > 1e11) and duration is in sec (< 1e9)
    if t > 1e11 and duration < 1e9:
        t_sec = t / 1000.0
        return math.floor((t_sec - epoch) / duration)
    return math.floor((t - epoch) / duration)


def index_to_time(index: int, calendar: Calendar, return_ms: bool = False) -> tuple[int, int]:
    """Calculate (t_open, t_close) from bar index.

    Returns seconds or milliseconds based on return_ms.
    """
    epoch = calendar.epoch_utc
    duration = calendar.bar_duration_sec
    t_open = epoch + index * duration
    t_close = t_open + duration
    if return_ms:
        return int(t_open * 1000), int(t_close * 1000)
    return int(t_open), int(t_close)


def get_binance_weekly_anchor(t_sec: float, anchor_weekday: int = 1) -> int:
    """Calculate the Monday 00:00:00 UTC weekly anchor for a given timestamp.

    anchor_weekday: 1 = Monday (ISO standard / spec v2.2 default).
    Returns epoch seconds of the week's opening candle.
    """
    dt = datetime.fromtimestamp(t_sec, tz=timezone.utc)
    # ISO weekday: Monday = 1, Sunday = 7
    current_iso_weekday = dt.isoweekday()
    days_to_subtract = (current_iso_weekday - anchor_weekday) % 7
    monday = dt - timedelta(days=days_to_subtract)
    monday_00 = datetime(monday.year, monday.month, monday.day, 0, 0, 0, tzinfo=timezone.utc)
    return int(monday_00.timestamp())
