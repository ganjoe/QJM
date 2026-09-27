"""Rendering of the generic overlay primitives (contract section 3).

Covers zone geometry, line segmentation on colour changes, per-bar histogram
colours, level lines, and the rule that zones/levels stay out of the Y
autoscale and out of the crosshair readout.
"""

import pytest
from PySide6.QtGui import QColor, QImage, QPainter

from chart_viewer.config import ViewerConfig
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.models.entities import Bar, Overlay, OverlayPoint
from chart_viewer.ui.pane import (
    DECOR_OVERLAY_TYPES,
    OVERLAY_DRAW_ORDER,
    ChartPane,
    Y_AXIS_WIDTH,
)

T0 = 1_700_000_000
DAY = 86_400
BAR_COUNT = 40
CANVAS_W = 800
CANVAS_H = 300
CHART_W = CANVAS_W - int(Y_AXIS_WIDTH)

BG = QColor("#000000")


def _bars(count: int = BAR_COUNT):
    return [
        Bar(
            t_open=T0 + i * DAY,
            t_close=T0 + (i + 1) * DAY,
            open=100.0 + i,
            high=102.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=1000.0 + i,
        )
        for i in range(count)
    ]


def _point(index, value, value2=None, t2_index=None, color_override=None):
    return OverlayPoint(
        t=T0 + index * DAY,
        value=value,
        value2=value2,
        t2=T0 + t2_index * DAY if t2_index is not None else None,
        color_override=color_override,
    )


def _overlay(ov_id, ov_type, points, **style):
    return Overlay(
        overlay_id=ov_id,
        series_id="s",
        type=ov_type,
        pane="ind",
        values=points,
        style=dict(style),
    )


def _pane(overlays=(), bars=None, is_main=False, value_range=(90.0, 130.0)):
    config = ViewerConfig()
    x_trans = XAxisTransform(config=config)
    pane = ChartPane(pane_id="ind", x_trans=x_trans, config=config, is_main=is_main)
    pane.resize(CANVAS_W, CANVAS_H)
    x_trans.set_viewport_width(float(CHART_W))
    x_trans.latest_bar_index = float(BAR_COUNT - 1)
    x_trans.right_index = float(BAR_COUNT - 1)
    pane.y_trans.viewport_height_px = float(CANVAS_H)
    pane.y_trans.fit_range(value_range[0], value_range[1])
    pane.set_data(bars if bars is not None else _bars(), {ov.overlay_id: ov for ov in overlays})
    return pane


def _render(pane):
    """Paint the overlay layer of a pane onto a black image."""
    image = QImage(CANVAS_W, CANVAS_H, QImage.Format.Format_ARGB32)
    image.fill(BG)
    painter = QPainter(image)
    try:
        pane._render_overlays(painter, float(CHART_W), float(CANVAS_H))
    finally:
        painter.end()
    return image


def _count(image, predicate):
    hits = 0
    for y in range(image.height()):
        for x in range(CHART_W):
            if predicate(image.pixelColor(x, y)):
                hits += 1
    return hits


def _is_red(color):
    return color.red() > 200 and color.green() < 40 and color.blue() < 40


def _is_green(color):
    return color.green() > 200 and color.red() < 40 and color.blue() < 40


def _is_magenta(color):
    return color.red() > 200 and color.blue() > 200 and color.green() < 40


# -- Zones -----------------------------------------------------------------


def test_zone_geometry_maps_data_space_to_pixels(qapp):
    zone = _overlay("zone", "zone", [_point(5, 110.0, 120.0, t2_index=10)], color="#FF0000")
    pane = _pane([zone])
    point = pane._indexed_overlays["zone"]["pts"][0]

    rect = pane.zone_rect(point.idx, point.t2_idx, point.value, point.value2, CANVAS_H)

    assert rect.left() == pytest.approx(pane.x_trans.bar_to_x(5.0))
    assert rect.right() == pytest.approx(pane.x_trans.bar_to_x(10.0))
    assert rect.top() == pytest.approx(pane.y_trans.price_to_y(120.0))
    assert rect.bottom() == pytest.approx(pane.y_trans.price_to_y(110.0))
    assert rect.width() > 0 and rect.height() > 0


def test_zone_rect_normalises_reversed_corners(qapp):
    pane = _pane()

    rect = pane.zone_rect(10.0, 5.0, 120.0, 110.0, CANVAS_H)

    assert rect.left() == pytest.approx(pane.x_trans.bar_to_x(5.0))
    assert rect.right() == pytest.approx(pane.x_trans.bar_to_x(10.0))
    assert rect.top() == pytest.approx(pane.y_trans.price_to_y(120.0))
    assert rect.bottom() == pytest.approx(pane.y_trans.price_to_y(110.0))


@pytest.mark.parametrize("value,value2", [(None, None), (110.0, None), (None, 120.0)])
def test_zone_with_null_values_spans_full_pane_height(qapp, value, value2):
    pane = _pane()

    rect = pane.zone_rect(5.0, 10.0, value, value2, CANVAS_H)

    assert rect.top() == 0.0
    assert rect.height() == pytest.approx(float(CANVAS_H))


def test_zone_is_drawn_with_the_style_alpha(qapp):
    zone = _overlay("zone", "zone", [_point(2, 100.0, 130.0, t2_index=20)], color="#FF0000")
    pane = _pane([zone])

    image = _render(pane)

    # Default alpha 30 over black leaves a dim red, never a saturated one.
    assert _count(image, lambda c: 20 <= c.red() <= 45 and c.green() < 5 and c.blue() < 5) > 0
    assert _count(image, _is_red) == 0


def test_zone_alpha_override_is_honoured(qapp):
    zone = _overlay("zone", "zone", [_point(2, 100.0, 130.0, t2_index=20)], color="#FF0000", alpha=255)
    pane = _pane([zone])

    image = _render(pane)

    assert _count(image, _is_red) > 0


def test_zone_is_painted_before_lines(qapp):
    """A zone is background decoration: a line drawn later wins the overlap."""
    zone = _overlay("zone", "zone", [_point(5, 100.0, 130.0, t2_index=25)], color="#FF0000", alpha=255)
    line = _overlay(
        "line", "line", [_point(i, 115.0) for i in range(BAR_COUNT)], color="#00FF00", width=5
    )
    pane = _pane([line, zone])  # line first in the overlay dict, zone still paints first

    image = _render(pane)

    x_inside_zone = int(pane.x_trans.bar_to_x(15.0))
    y_on_line = int(pane.y_trans.price_to_y(115.0))
    y_in_zone_only = int(pane.y_trans.price_to_y(105.0))
    assert _is_green(image.pixelColor(x_inside_zone, y_on_line))
    assert _is_red(image.pixelColor(x_inside_zone, y_in_zone_only))


def test_zone_without_t2_is_ignored(qapp):
    zone = _overlay("zone", "zone", [_point(5, 100.0, 130.0)], color="#FF0000", alpha=255)
    pane = _pane([zone])

    image = _render(pane)

    assert _count(image, _is_red) == 0


def test_draw_order_starts_with_zones(qapp):
    assert OVERLAY_DRAW_ORDER[0] == "zone"
    assert "level" in OVERLAY_DRAW_ORDER and "line" in OVERLAY_DRAW_ORDER
    assert OVERLAY_DRAW_ORDER.index("level") < OVERLAY_DRAW_ORDER.index("line")
    assert DECOR_OVERLAY_TYPES == frozenset({"zone", "level"})


# -- Line colour segments --------------------------------------------------


def test_line_splits_into_segments_at_color_override_changes(qapp):
    points = [
        _point(i, 100.0 + i, color_override="#FF0000" if i < 3 else "#00FF00")
        for i in range(6)
    ]
    pane = _pane([_overlay("line", "line", points, color="#26A69A")])

    segments = pane._line_color_segments(pane._indexed_overlays["line"]["pts"], "#26A69A")

    assert [color for color, _ in segments] == ["#FF0000", "#00FF00"]
    # The transition point is shared so the polyline stays connected.
    assert [len(segment) for _, segment in segments] == [3, 4]


def test_line_points_without_override_use_the_style_color(qapp):
    points = [_point(i, 100.0 + i) for i in range(4)]
    pane = _pane([_overlay("line", "line", points, color="#123456")])

    segments = pane._line_color_segments(pane._indexed_overlays["line"]["pts"], "#123456")

    assert [color for color, _ in segments] == ["#123456"]
    assert [len(segment) for _, segment in segments] == [4]


def test_line_gap_still_breaks_the_segment(qapp):
    points = [
        _point(0, 100.0),
        _point(1, 101.0),
        _point(2, None),
        _point(3, 103.0, color_override="#00FF00"),
    ]
    pane = _pane([_overlay("line", "line", points, color="#26A69A")])

    segments = pane._line_color_segments(pane._indexed_overlays["line"]["pts"], "#26A69A")

    assert [color for color, _ in segments] == ["#26A69A", "#00FF00"]
    assert [len(segment) for _, segment in segments] == [2, 1]


def test_line_with_color_override_paints_both_colours(qapp):
    points = [
        _point(i, 100.0 + i, color_override="#FF0000" if i < 10 else "#00FF00")
        for i in range(20)
    ]
    pane = _pane([_overlay("line", "line", points, color="#26A69A", width=3)])

    image = _render(pane)

    assert _count(image, _is_red) > 0
    assert _count(image, _is_green) > 0


# -- Histogram bars --------------------------------------------------------


def test_histogram_bar_uses_color_override(qapp):
    points = [
        _point(5, 105.0, color_override="#FF0000"),
        _point(6, 105.0),
    ]
    pane = _pane([_overlay("hist", "histogram", points, color="#0000FF", alpha=255)])

    image = _render(pane)

    assert _count(image, _is_red) > 0
    assert _count(image, lambda c: c.blue() > 200 and c.red() < 40) > 0
    # The overridden bar is the LEFT one (bar 5), the style-coloured bar follows.
    row = int(pane.y_trans.price_to_y(105.0)) + 5
    red_xs = [x for x in range(CHART_W) if _is_red(image.pixelColor(x, row))]
    blue_xs = [x for x in range(CHART_W) if image.pixelColor(x, row).blue() > 200]
    assert red_xs and blue_xs
    assert max(red_xs) < min(blue_xs)


# -- Levels ----------------------------------------------------------------


def test_level_is_drawn_across_the_full_chart_width(qapp):
    level = _overlay("lvl", "level", [_point(0, 115.0)], color="#FF00FF", width=3)
    pane = _pane([level])

    image = _render(pane)

    y = int(pane.y_trans.price_to_y(115.0))
    magenta = sum(
        1 for x in range(CHART_W) if _is_magenta(image.pixelColor(x, y))
    )
    assert magenta >= 0.95 * CHART_W


def test_level_draws_every_value(qapp):
    level = _overlay("lvl", "level", [_point(0, 105.0), _point(5, 120.0)], color="#FF00FF", width=3)
    pane = _pane([level])

    image = _render(pane)

    for value in (105.0, 120.0):
        y = int(pane.y_trans.price_to_y(value))
        assert any(_is_magenta(image.pixelColor(x, y)) for x in range(CHART_W))


def test_level_uses_the_default_colour_and_can_dash(qapp):
    level = _overlay("lvl", "level", [_point(0, 115.0)], width=3, dash=True)
    pane = _pane([level])

    image = _render(pane)

    default_color = QColor("#546E7A")
    y = int(pane.y_trans.price_to_y(115.0))
    hits = sum(
        1
        for x in range(CHART_W)
        if abs(image.pixelColor(x, y).red() - default_color.red()) <= 10
        and abs(image.pixelColor(x, y).green() - default_color.green()) <= 10
        and abs(image.pixelColor(x, y).blue() - default_color.blue()) <= 10
    )
    # A dashed line covers less than a solid one, but still a large part.
    assert hits > 0
    assert hits < CHART_W


def test_level_label_is_painted(qapp):
    solid = _overlay("lvl", "level", [_point(0, 115.0)], color="#FF00FF", width=1)
    labelled = _overlay("lvl", "level", [_point(0, 115.0)], color="#FF00FF", width=1, label="80")

    without_label = _render(_pane([solid]))
    with_label = _render(_pane([labelled]))

    def _non_black(image):
        return _count(image, lambda c: c.red() + c.green() + c.blue() > 0)

    assert _non_black(with_label) > _non_black(without_label)


# -- Y autoscale / crosshair readout --------------------------------------


def _line_only():
    return _overlay("line", "line", [_point(i, 100.0 + i) for i in range(20)], color="#26A69A")


def test_zone_and_level_do_not_change_the_y_range(qapp):
    plain = _pane([_line_only()])
    plain.update_y_range()

    decorated = _pane([
        _line_only(),
        _overlay("zone", "zone", [_point(2, 1000.0, 2000.0, t2_index=10)], color="#FF0000"),
        _overlay("lvl", "level", [_point(0, 5000.0)], color="#546E7A"),
    ])
    decorated.update_y_range()

    assert decorated.y_trans.p_min == pytest.approx(plain.y_trans.p_min)
    assert decorated.y_trans.p_max == pytest.approx(plain.y_trans.p_max)
    assert decorated.y_trans.p_max < 200.0  # the far-away decoration is ignored


def test_crosshair_boxes_skip_zones_and_levels(qapp):
    pane = _pane([
        _line_only(),
        _overlay("zone", "zone", [_point(2, 1000.0, 2000.0, t2_index=10)], color="#FF0000"),
        _overlay("lvl", "level", [_point(0, 5000.0)], color="#546E7A"),
    ])

    assert [value.key for value in pane.crosshair_values(5)] == ["line"]
    assert [value.key for value in pane.latest_values()] == ["line"]


def test_overlay_boxes_never_return_decorations(qapp):
    pane = _pane([
        _overlay("zone", "zone", [_point(2, 1000.0, 2000.0, t2_index=10)], color="#FF0000"),
        _overlay("lvl", "level", [_point(0, 5000.0)], color="#546E7A"),
    ])

    assert pane._overlay_boxes("zone", pane._indexed_overlays["zone"], 1000.0, 2000.0) == []
    assert pane._overlay_boxes("lvl", pane._indexed_overlays["lvl"], 5000.0, None) == []
