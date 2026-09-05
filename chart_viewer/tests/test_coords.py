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


def test_y_axis_bottom_anchored_zoom():
    """Verify Rule: Chart Y-zoom remains pinned at the bottom of the window."""
    y_trans = YAxisTransform(viewport_height_px=600.0)
    y_trans.fit_range(50.0, 150.0, requested_mode="linear")

    initial_p_bottom = y_trans.p_bottom
    initial_p_top = y_trans.p_top

    # Zoom in (scale factor < 1)
    y_trans.manual_scale(0.8)
    assert math.isclose(y_trans.p_bottom, initial_p_bottom, abs_tol=1e-5), "p_bottom must remain fixed at window bottom"
    assert y_trans.p_top < initial_p_top, "p_top must compress downward towards bottom"

    # Zoom out (scale factor > 1)
    y_trans.manual_scale(1.5)
    assert math.isclose(y_trans.p_bottom, initial_p_bottom, abs_tol=1e-5), "p_bottom must remain fixed at window bottom"
    assert y_trans.p_top > initial_p_top, "p_top must expand upward away from bottom"


def test_y_axis_tc2000_boundary_handle():
    """Verify TC2000-style Y-axis mini-arrow handle adjusts the upper boundary."""
    y_trans = YAxisTransform(viewport_height_px=600.0, min_padding_px=25.0)
    y_trans.fit_range(100.0, 200.0, requested_mode="linear")
    initial_p_bottom = y_trans.p_bottom

    # Set top boundary to pixel 120 (20% from top of 600px pane)
    y_trans.set_top_boundary_px(120.0)

    # p_max (200.0) must now map to exactly pixel 120
    assert math.isclose(y_trans.price_to_y(200.0), 120.0, abs_tol=1.0)
    # p_bottom must remain fixed
    assert math.isclose(y_trans.p_bottom, initial_p_bottom, abs_tol=1e-5)


def test_x_axis_pinned_right_margin_zoom():
    """Verify Rule: Chart remains pinned at 10% distance from the right edge upon X-zoom."""
    x_trans = XAxisTransform(
        viewport_width_px=1000.0,
        candle_width_px=10.0,
        right_margin_pct=0.10,
        pin_to_right=True,
        right_index=150.0,
    )

    # With right_margin_pct = 0.10 and viewport_width = 1000, margin is 100px.
    # The bar at right_index sits at pixel 900 (10% from right edge).
    expected_x = 900.0
    assert math.isclose(x_trans.bar_to_x(150.0), expected_x, abs_tol=1e-4)

    # Zoom in
    x_trans.zoom(1.5)
    assert math.isclose(x_trans.bar_to_x(150.0), expected_x, abs_tol=1e-4), "Latest candle must stay pinned at 10% margin"

    # Zoom out
    x_trans.zoom(0.5)
    assert math.isclose(x_trans.bar_to_x(150.0), expected_x, abs_tol=1e-4), "Latest candle must stay pinned at 10% margin"


def test_x_axis_touches_left_window_edge():
    """Verify Rule: Initial load auto-fits left border, and zoom-out can reach min_candle_width_px."""
    cfg = ViewerConfig(min_candle_width_px=1.0, right_margin_pct=0.10)
    # 1. Initial layout with 60 bars on a 1000px viewport (anchor_x = 900)
    x_trans = XAxisTransform(
        viewport_width_px=1000.0,
        candle_width_px=8.0,
        right_margin_pct=0.10,
        pin_to_right=True,
        right_index=59.0,
        config=cfg,
    )

    # Before adjustment, 60 bars * 8.0 = 480px, leaving 900 - 480 = 420px void on the left
    assert x_trans.bar_to_x(0.0) > 0.0

    # Calling ensure_touches_left() auto-fits candle width to span from x=0 to anchor_x
    changed = x_trans.ensure_touches_left()
    assert changed is True
    assert math.isclose(x_trans.bar_to_x(0.0), 0.0, abs_tol=1e-3), "Bar 0 must touch left window edge (x=0)"
    assert math.isclose(x_trans.bar_to_x(59.0), 900.0, abs_tol=1e-3), "Bar 59 must stay pinned at 10% right margin"

    # 2. Zoom in: candle width increases, bar 0 goes beyond left edge (x < 0), latest bar stays pinned
    x_trans.zoom(1.5)
    assert x_trans.bar_to_x(0.0) < 0.0, "Zoom-in expands history past left edge"
    assert math.isclose(x_trans.bar_to_x(59.0), 900.0, abs_tol=1e-3)

    # 3. Zoom out: candle width can decrease smoothly down to touch_left_border limit
    for _ in range(15):
        x_trans.zoom(0.7)

    # Since touch_left_border is True, it should stop zooming out when all 60 bars are visible.
    # 900px / 59 intervals = 15.2542 px
    assert math.isclose(x_trans.candle_width_px, 900.0 / 59.0, abs_tol=1e-3), "Zoom-out reaches touch_left_border limit"
    assert math.isclose(x_trans.bar_to_x(59.0), 900.0, abs_tol=1e-3), "Latest bar remains pinned at 10% margin"

    # 4. Pan cannot drag bar 0 into the screen past origin
    x_trans.pan(500.0)
    assert x_trans.bar_to_x(0.0) <= 0.0, "Pan must not detach chart from left window edge"


def test_y_zoom_never_violates_top_border():
    """Verify Bug 1 Fix: Aggressive Y-zoom never violates top margin or rages over top border."""
    y_trans = YAxisTransform(viewport_height_px=600.0, min_padding_px=25.0)
    y_trans.fit_range(50.0, 150.0, requested_mode="linear")

    # Initial p_max (150.0) has at least 15% headroom + 25px padding
    initial_y_max = y_trans.price_to_y(150.0)
    assert initial_y_max >= 25.0

    # Zoom in repeatedly (Shift+Wheel UP, factor < 1.0)
    for _ in range(25):
        y_trans.manual_scale(0.85)
        y_max = y_trans.price_to_y(150.0)
        # MUST NEVER exceed the top border: y_max >= min_padding_px (25.0) at all times
        assert y_max >= 25.0 - 1e-5, f"Candles must never rage over top border! Got y={y_max}"
        # Bottom must stay fixed
        assert y_trans.p_bottom <= 50.0

    # Exactly at clamp limit: y_max is clamped at 25.0px
    assert math.isclose(y_trans.price_to_y(150.0), 25.0, abs_tol=1e-3)


def test_resize_preserves_scaling_and_distance_rules():
    """Verify Bug 2 Fix: Window/pane resize preserves scaling, headroom ratio, 10% right margin, and left border touch."""
    x_trans = XAxisTransform(
        viewport_width_px=1000.0,
        candle_width_px=10.0,
        right_margin_pct=0.10,
        pin_to_right=True,
        right_index=150.0,
    )
    y_trans = YAxisTransform(viewport_height_px=600.0, min_padding_px=25.0)
    y_trans.fit_range(100.0, 200.0, requested_mode="linear")

    # Before resize:
    # 10% right margin: 1000 * 0.90 = 900.0
    assert math.isclose(x_trans.bar_to_x(150.0), 900.0, abs_tol=1e-3)
    # Headroom ratio is preserved
    ratio_before = y_trans.top_margin_ratio

    # 1. Resize smaller (height=400, width=800)
    y_trans.viewport_height_px = 400.0
    y_trans.fit_range(100.0, 200.0, requested_mode="linear")
    x_trans.viewport_width_px = 800.0
    x_trans.ensure_touches_left()

    # Right margin is at 800 * 0.90 = 720.0 (10% distance intact!)
    assert math.isclose(x_trans.bar_to_x(150.0), 720.0, abs_tol=1e-3)
    # Top padding is still >= min_padding_px (25px) and headroom ratio matches
    assert y_trans.price_to_y(200.0) >= 25.0
    assert math.isclose(y_trans.top_margin_ratio, ratio_before, abs_tol=1e-3)

    # 2. Resize larger (height=900, width=1600)
    y_trans.viewport_height_px = 900.0
    y_trans.fit_range(100.0, 200.0, requested_mode="linear")
    x_trans.viewport_width_px = 1600.0
    x_trans.ensure_touches_left()

    # Right margin is at 1600 * 0.90 = 1440.0 (10% distance intact!)
    assert math.isclose(x_trans.bar_to_x(150.0), 1440.0, abs_tol=1e-3)
    # Top padding is still >= min_padding_px (25px) and headroom ratio matches
    assert y_trans.price_to_y(200.0) >= 25.0
    assert math.isclose(y_trans.top_margin_ratio, ratio_before, abs_tol=1e-3)


def test_narrowing_compresses_candles():
    """Verify that narrowing the window compresses candles proportionally (stauchen) and keeps left touch."""
    cfg = ViewerConfig(min_visible_bars=6, right_margin_pct=0.10)
    transform = XAxisTransform(
        viewport_width_px=1000.0,
        candle_width_px=10.0,
        right_index=90.0,
        config=cfg,
        touch_left_border=True,
        pin_to_right=True,
    )

    # Initially anchor_x = 900. 900 - 90*10 = 0.0 -> touches left border
    assert math.isclose(transform.bar_to_x(0.0), 0.0, abs_tol=1e-3)

    # 1. Widen window: 1000 -> 1200
    transform.set_viewport_width(1200.0)
    # anchor_x is now 1080. Ratio = 1080/900 = 1.2 -> candle_width = 12.0
    assert math.isclose(transform.candle_width_px, 12.0, abs_tol=1e-3)
    assert math.isclose(transform.bar_to_x(0.0), 0.0, abs_tol=1e-3)

    # 2. Narrow window: 1200 -> 800
    transform.set_viewport_width(800.0)
    # anchor_x is now 720. Ratio = 720/1080 = 2/3 -> candle_width = 8.0
    assert math.isclose(transform.candle_width_px, 8.0, abs_tol=1e-3)
    assert math.isclose(transform.bar_to_x(0.0), 0.0, abs_tol=1e-3)


def test_right_click_pan():
    """Verify right-click / middle-click horizontal panning."""
    cfg = ViewerConfig(min_visible_bars=6, right_margin_pct=0.10)
    transform = XAxisTransform(
        viewport_width_px=1000.0,
        candle_width_px=10.0,
        right_index=90.0,
        config=cfg,
        touch_left_border=True,
        pin_to_right=True,
    )

    # 1. Pan left by -100 pixels (pulls chart left into history)
    transform.pan(-100.0)
    assert math.isclose(transform.right_index, 100.0, abs_tol=1e-3)
    assert math.isclose(transform.bar_to_x(0.0), -100.0, abs_tol=1e-3)

    # 2. Pan right by +50 pixels (pulls chart right)
    transform.pan(50.0)
    assert math.isclose(transform.right_index, 95.0, abs_tol=1e-3)
    assert math.isclose(transform.bar_to_x(0.0), -50.0, abs_tol=1e-3)

    # 3. Pan right beyond origin -> clamped at left border
    transform.pan(100.0)
    assert math.isclose(transform.right_index, 90.0, abs_tol=1e-3)
    assert math.isclose(transform.bar_to_x(0.0), 0.0, abs_tol=1e-3)
