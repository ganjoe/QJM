"""Regression + unit tests for TC2000-style crosshair snapping.

Bug: the X-axis date badge used int(x_to_bar(cx)) (floor), so it flipped at the
candle CENTER. The left half of every candle body showed the previous candle date,
i.e. the date jumped *inside* the candle. The fix snaps to the nearest bar with
floor(x + 0.5), so the flip happens in the gap and the badge, the vertical line and
the emitted crosshair timestamp all share one bar index.
"""

from types import SimpleNamespace

import pytest

from chart_viewer.config import ViewerConfig
from chart_viewer.coords.x_axis import XAxisTransform


def _transform(right_index=100.0, candle_width=10.0, width=1000.0):
    cfg = ViewerConfig(min_candle_width_px=1.0, max_candle_width_px=100.0)
    return XAxisTransform(
        viewport_width_px=width,
        candle_width_px=candle_width,
        right_margin_pct=0.10,
        right_index=right_index,
        config=cfg,
    )


def test_nearest_boundary_is_between_candles():
    """The snap decision flips halfway between two candle centers, not at a center."""
    x = _transform()
    cw = x.candle_width_px
    for i in range(1, 20):
        center = x.bar_to_x(float(i))
        assert x.nearest_bar_index(center - 0.5 * cw - 0.01, 200) == i - 1
        assert x.nearest_bar_index(center - 0.5 * cw + 0.01, 200) == i
        assert x.nearest_bar_index(center, 200) == i


def test_snap_never_changes_inside_candle_body():
    """Across the whole candle body (80% of the slot) the index stays constant."""
    x = _transform(candle_width=20.0)
    cw = x.candle_width_px
    for i in range(1, 20):
        center = x.bar_to_x(float(i))
        for tenth in range(-8, 9):
            px = center + tenth / 10.0
            assert x.nearest_bar_index(px, 200) == i, (i, tenth, x.x_to_bar(px))


def test_old_floor_behaviour_would_have_flipped_at_center():
    """Guards the regression: floor() changes at the center, nearest does not."""
    x = _transform(candle_width=20.0)
    cw = x.candle_width_px
    i = 10
    center = x.bar_to_x(float(i))
    left_body = center - 0.4 * cw
    assert int(x.x_to_bar(left_body)) == i - 1        # old, buggy badge value
    assert x.nearest_bar_index(left_body, 200) == i   # fixed value


def test_snap_clamps_to_loaded_range():
    x = _transform()
    assert x.nearest_bar_index(-10_000.0, 50) == 0
    assert x.nearest_bar_index(10_000.0, 50) == 49
    assert x.nearest_bar_index(0.0, 0) == 0


def test_no_bankers_rounding():
    """x_to_bar == 2.5 must snap to 3 (half-up), not 2 (Python round)."""
    x = _transform(right_index=100.0, candle_width=10.0)
    px = x.anchor_x - x.candle_width_px * (100.0 - 2.0 - 0.5)
    assert x.x_to_bar(px) == pytest.approx(2.5)
    assert x.nearest_bar_index(px, 200) == 3


def test_bar_center_for_pixel_returns_exact_center():
    x = _transform()
    for i in range(1, 20):
        idx, px = x.bar_center_for_pixel(x.bar_to_x(float(i)) + 1.0, 200)
        assert idx == i
        assert px == pytest.approx(x.bar_to_x(float(i)))


def _make_canvas():
    from chart_viewer.ui.canvas import ChartCanvas

    canvas = ChartCanvas("test-win", ViewerConfig())
    canvas.resize(1000, 600)
    canvas._rebuild_panes(["main"])
    canvas.window_data = SimpleNamespace(
        bars=[SimpleNamespace(t_open=1_700_000_000 + i * 86_400) for i in range(50)]
    )
    canvas.x_trans.latest_bar_index = 49.0
    canvas.x_trans.right_index = 49.0
    canvas.x_trans.set_viewport_width(930.0)
    return canvas


def test_canvas_snap_pushes_same_index_to_line_badge_and_broadcast(qapp):
    """Line x, badge index and emitted timestamp come from one snapped index."""
    canvas = _make_canvas()
    cw = canvas.x_trans.candle_width_px
    center = canvas.x_trans.bar_to_x(20.0)
    left_body = center - 0.4 * cw

    idx, snapped_x = canvas._snap_crosshair(left_body)
    assert idx == 20
    assert snapped_x == pytest.approx(center)

    canvas._apply_crosshair("main", left_body, 10.0)
    pane = canvas._panes["main"]
    assert pane._crosshair_bar_index == 20
    assert pane._crosshair_x == pytest.approx(center)

    emitted = []
    canvas.crosshair_moved.connect(lambda ts, i: emitted.append(i))
    canvas._broadcast_crosshair_from_x(left_body)
    assert emitted[-1] == 20


def test_remote_crosshair_keeps_index(qapp):
    canvas = _make_canvas()
    px = canvas.x_trans.bar_to_x(7.0)
    canvas._apply_crosshair_remote(px, 7)
    assert canvas._panes["main"]._crosshair_bar_index == 7
