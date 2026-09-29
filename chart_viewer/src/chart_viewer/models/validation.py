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


# Y-Achsen-Skalierungen, die ein Pane annehmen darf (Log/Lin-Taste im Pane).
PANE_SCALES = ("linear", "log")


def sanitize_pane_scales(raw) -> dict:
    """Normalize a pane -> scale mapping from a preset/snapshot payload.

    Pane IDs are lowercased (the viewer addresses panes that way); unknown scale
    values fall back to "linear" instead of raising, so one broken entry can
    never take a whole preset down.
    """
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, value in raw.items():
        pane_id = str(key or "").strip().lower()
        if not pane_id:
            continue
        scale = str(value or "").strip().lower()
        clean[pane_id] = scale if scale in PANE_SCALES else "linear"
    return clean


def sanitize_pane_range(raw):
    """Feste Y-Achsen-Spanne eines Panes: (min, max) oder None.

    Vertrag: Pane-Preset `params.range = [min, max]` (z. B. [0, 100] fuer eine
    Breiten-Pane). Gueltig ist nur ein Paar endlicher Zahlen mit min < max -
    alles andere wird verworfen, statt eine Achse kaputt zu skalieren.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        lo, hi = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None
    if lo != lo or hi != hi:                     # NaN
        return None
    if abs(lo) == float("inf") or abs(hi) == float("inf"):
        return None
    if lo >= hi:
        return None
    return (lo, hi)


def is_log_compatible(prices: Sequence[float]) -> bool:
    """Prices > 0 mandatory for Log-Y axis.

    Returns False if any price <= 0.
    """
    for p in prices:
        if p <= 0:
            return False
    return True
