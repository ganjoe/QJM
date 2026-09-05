from chart_viewer.config import ViewerConfig
"""Tests for validation rules (Section 2.3)."""

from chart_viewer.models.entities import Bar
from chart_viewer.models.validation import (
    validate_bar,
    validate_series_monotonicity,
    is_log_compatible,
)


def test_bar_validation_valid():
    bar = Bar(t_open=1000, t_close=1060, open=100.0, high=105.0, low=95.0, close=102.0)
    validated = validate_bar(bar)
    assert validated.is_valid is True


def test_bar_validation_invalid_high():
    # high < max(open, close)
    bar = Bar(t_open=1000, t_close=1060, open=100.0, high=101.0, low=95.0, close=103.0)
    validated = validate_bar(bar)
    # Must NOT be discarded, but flagged
    assert validated.is_valid is False


def test_bar_validation_invalid_low():
    # low > min(open, close)
    bar = Bar(t_open=1000, t_close=1060, open=100.0, high=105.0, low=101.0, close=102.0)
    validated = validate_bar(bar)
    assert validated.is_valid is False


def test_series_monotonicity():
    bars_valid = [
        Bar(t_open=1000, t_close=1060, open=10.0, high=12.0, low=9.0, close=11.0),
        Bar(t_open=1060, t_close=1120, open=11.0, high=13.0, low=10.0, close=12.0),
    ]
    assert validate_series_monotonicity(bars_valid) is True

    # Overlapping or negative duration
    bars_invalid_order = [
        Bar(t_open=1060, t_close=1120, open=11.0, high=13.0, low=10.0, close=12.0),
        Bar(t_open=1000, t_close=1060, open=10.0, high=12.0, low=9.0, close=11.0),
    ]
    assert validate_series_monotonicity(bars_invalid_order) is False

    bars_negative_duration = [
        Bar(t_open=1060, t_close=1050, open=11.0, high=13.0, low=10.0, close=12.0),
    ]
    assert validate_series_monotonicity(bars_negative_duration) is False


def test_log_compatibility():
    assert is_log_compatible([10.0, 20.5, 0.01]) is True
    assert is_log_compatible([10.0, 0.0, 20.0]) is False
    assert is_log_compatible([10.0, -5.0, 20.0]) is False
