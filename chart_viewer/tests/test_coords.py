"""Tests for coordinate transformations and guards (Section 5 & Criterion 4)."""

import math
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.coords.y_axis import YAxisTransform
from chart_viewer.config import ViewerConfig


def test_x_axis_zoom_limits():
    """Verify hard limit of minimum 6 visible candles."""
    cfg = ViewerConfig(min_visible_bars=6, future_space_bars=10, max_candle_width_px=100.0)
    transform = XAxisTransform(
        viewport_width_px=800.0,
        candle_width_px=10.0,
        future_space_bars=10,
        right_index=100.0,
        config=cfg,
    )

    # Zoom in aggressively
    for _ in range(20):
        transform.zoom(1.5, anchor_mouse_x=400.0)

    # Visible bars must never drop below 6
    assert transform.visible_bars >= 6


def test_x_axis_zoom_anchor_stationary():
    """Verify that the bar under the mouse cursor remains stationary during zoom."""
    transform = XAxisTransform(
        viewport_width_px=1000.0,
        candle_width_px=10.0,
        future_space_bars=10,
        right_index=50.0,
    )

    mouse_x = 450.0
    bar_before = transform.x_to_bar(mouse_x)

    # Zoom in
    transform.zoom(1.2, anchor_mouse_x=mouse_x)
    bar_after = transform.x_to_bar(mouse_x)

    assert math.isclose(bar_before, bar_after, abs_tol=1e-3)


def test_y_axis_linear_padding():
    y_trans = YAxisTransform(
        viewport_height_px=500.0,
        margin_top_pct=0.10,
        margin_bottom_pct=0.10,
    )
    # Range 100 to 200 -> R = 100. P_top = 210, P_bottom = 90
    forced = y_trans.fit_range(100.0, 200.0, requested_mode="linear")
    assert forced is False
    assert y_trans.mode == "linear"
    assert math.isclose(y_trans.p_top, 210.0, abs_tol=1e-4)
    assert math.isclose(y_trans.p_bottom, 90.0, abs_tol=1e-4)

    # Test conversion: price 210 -> pixel 0 (top), price 90 -> pixel 500 (bottom)
    assert math.isclose(y_trans.price_to_y(210.0), 0.0, abs_tol=1e-4)
    assert math.isclose(y_trans.price_to_y(90.0), 500.0, abs_tol=1e-4)
    assert math.isclose(y_trans.price_to_y(150.0), 250.0, abs_tol=1e-4)


def test_y_axis_log_guard_forced_linear():
    """Verify Guard: If P_min <= 0, force linear mode and report axis.mode_forced."""
    y_trans = YAxisTransform(viewport_height_px=500.0)

    # P_min = 0.0 with requested_mode="log"
    forced = y_trans.fit_range(0.0, 100.0, requested_mode="log")
    assert forced is True
    assert y_trans.is_mode_forced is True
    assert y_trans.mode == "linear"

    # Negative price
    forced_neg = y_trans.fit_range(-5.0, 100.0, requested_mode="log")
    assert forced_neg is True
    assert y_trans.mode == "linear"

    # Strictly positive prices -> log mode succeeds
    normal_log = y_trans.fit_range(10.0, 1000.0, requested_mode="log")
    assert normal_log is False
    assert y_trans.is_mode_forced is False
    assert y_trans.mode == "log"
