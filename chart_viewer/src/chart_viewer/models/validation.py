"""Validation rules according to Section 2.3."""

from __future__ import annotations
from typing import Sequence
from chart_viewer.models.entities import Bar


def validate_bar(bar: Bar) -> Bar:
    """Validate bar consistency.

    Rule: high >= max(open, close) and low <= min(open, close).
    Violation -> Bar marked with is_valid = False (rendered with dashed outline,
    never discarded).
    """
    max_body = max(bar.open, bar.close)
    min_body = min(bar.open, bar.close)

    if bar.high < max_body or bar.low > min_body:
        bar.is_valid = False
    else:
        bar.is_valid = True
    return bar


def validate_series_monotonicity(bars: Sequence[Bar]) -> bool:
    """Check that t_open < t_close strictly monotonic within a series."""
    if not bars:
        return True

    for i in range(len(bars)):
        bar = bars[i]
        if bar.t_open >= bar.t_close:
            return False
        if i > 0 and bar.t_open < bars[i - 1].t_close:
            # Overlapping or backwards candle
            return False
    return True


def is_log_compatible(prices: Sequence[float]) -> bool:
    """Prices > 0 mandatory for Log-Y axis.

    Returns False if any price <= 0.
    """
    for p in prices:
        if p <= 0:
            return False
    return True
