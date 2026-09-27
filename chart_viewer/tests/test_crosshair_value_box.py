"""Tests for the crosshair value boxes (per-curve Y-axis readout at the cursor bar).

Design contract:
- The number belongs to the bar marked by the VERTICAL crosshair line (x) —
  never to the cursor's Y position.
- Every curve of a pane gets its own framed box (main pane: Close + Volume).
- Works for local mouse crosshairs and for remote (window-sync) crosshairs.
- Without a crosshair (mouse outside the pane, chart window not focused) the
  boxes stay visible and read the NEWEST value of every curve.
- Font and chrome of a box are scaled by VALUE_BOX_SCALE (~30 % smaller).
"""

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QFont

from chart_viewer.config import ViewerConfig
from chart_viewer.core.state_manager import WindowData
from chart_viewer.models.entities import Bar, Overlay, OverlayPoint
from chart_viewer.ui.canvas import ChartCanvas
from chart_viewer.ui.pane import VALUE_BOX_SCALE, X_AXIS_FONT_PT, Y_AXIS_WIDTH
from chart_viewer.ui.window import ChartWindow

BAR_COUNT = 30
T0 = 1_700_000_000
DAY = 86_400
MAGENTA = "#FF00FF"


def _bars(count=BAR_COUNT):
    return [
        Bar(
            t_open=T0 + i * DAY,
            t_close=T0 + (i + 1) * DAY,
            open=100.0 + i,
            high=102.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=12_000_000.0 + i * 100_000.0,
        )
        for i in range(count)
    ]


def _overlay(ov_id, pane="main", values=(), style=None, ov_type="line"):
    pts = []
    for item in values:
        val2 = item[2] if len(item) > 2 else None
        pts.append(OverlayPoint(t=item[0], value=item[1], value2=val2))
    return Overlay(
        overlay_id=ov_id,
        series_id="s",
        pane=pane,
        type=ov_type,
        values=pts,
        style=style or {"color": MAGENTA},
    )


def _canvas(overlays=(), bars=None, config=None, width=900, height=600):
    canvas = ChartCanvas(window_id="w", config=config or ViewerConfig())
    canvas.resize(width, height)
    win = WindowData("w")
    win.bars = bars if bars is not None else _bars()
    win.overlays = {ov.overlay_id: ov for ov in overlays}
    canvas.set_window_data(win)
    for pane in canvas._panes.values():
        pane.resize(width, height)
    return canvas


def _chart_window(bars=None, overlays=(), config=None, width=900, height=600):
    """Real ChartWindow (top-level) around the same fixture data as _canvas."""
    win = ChartWindow(window_id="w", config=config or ViewerConfig())
    win.resize(width, height)
    data = WindowData("w")
    data.bars = bars if bars is not None else _bars()
    data.overlays = {ov.overlay_id: ov for ov in overlays}
    win.bind_data(data)
    for pane in win.canvas._panes.values():
        pane.resize(width, height)
    return win


def _value_map(values):
    return {v.key: v for v in values}


# ── Value source: the bar (x), never the cursor Y ────────────────────────


def test_value_comes_from_bar_index_not_from_cursor_y(qapp):
    canvas = _canvas()
    pane = canvas._panes["main"]
    bars = canvas.window_data.bars

    values = pane.crosshair_values(5)
    price = _value_map(values)["price"]
    assert price.value == pytest.approx(bars[5].close)
    assert price.text == "105.50"
    assert price.secondary_text == "12,5 Mio."

    # The cursor Y position does not take part in the layout at all.
    font = pane._value_box_font(QFont())
    x = canvas.x_trans.bar_to_x(5.0)
    pane.set_crosshair(x, 20.0, True, 5)
    rects_top = [r for _, r in pane._layout_value_boxes(values, 400.0, font)]
    pane.set_crosshair(x, 380.0, True, 5)
    rects_bottom = [r for _, r in pane._layout_value_boxes(pane.crosshair_values(5), 400.0, font)]
    assert rects_top == rects_bottom


def test_value_changes_with_the_marked_bar(qapp):
    canvas = _canvas()
    pane = canvas._panes["main"]
    assert _value_map(pane.crosshair_values(5))["price"].value == pytest.approx(105.5)
    assert _value_map(pane.crosshair_values(6))["price"].value == pytest.approx(106.5)


# ── Idle readout: newest value while no crosshair marks a bar ────────────


def test_idle_readout_uses_the_newest_value_of_every_curve(qapp):
    bars = _bars()
    canvas = _canvas(overlays=[
        _overlay("sma", values=[(b.t_open, 100.0 + i) for i, b in enumerate(bars)]),
        _overlay("bb", values=[(b.t_open, 110.0 + i, 90.0 + i) for i, b in enumerate(bars)],
                 ov_type="band"),
    ])
    pane = canvas._panes["main"]

    values = _value_map(pane.latest_values())
    assert set(values) == {"price", "sma", "bb:upper", "bb:lower"}
    assert values["price"].value == pytest.approx(bars[-1].close)
    assert values["sma"].value == pytest.approx(100.0 + len(bars) - 1)
    assert values["bb:upper"].value == pytest.approx(110.0 + len(bars) - 1)
    # Marker overlays stay without a box in the idle readout as well.
    marked = _canvas(overlays=[_overlay("mark", values=[(b.t_open, 1.0) for b in bars],
                                         ov_type="marker")])
    assert set(_value_map(marked._panes["main"].latest_values())) == {"price"}


def test_idle_readout_uses_the_last_available_point_of_a_lagging_curve(qapp):
    """A curve that ends before the last bar still reports its newest number."""
    bars = _bars()
    lagging = _overlay("sma", values=[(b.t_open, 50.0 + i) for i, b in enumerate(bars[:-2])])
    canvas = _canvas(overlays=[lagging])
    pane = canvas._panes["main"]

    # Crosshair boxes stay strict: no value at the marked bar -> no box.
    assert "sma" not in _value_map(pane.crosshair_values(len(bars) - 1))
    # Idle readout: the end of the series counts, even if it lags the price.
    assert _value_map(pane.latest_values())["sma"].value == pytest.approx(
        50.0 + (len(bars) - 3)
    )


def test_idle_boxes_replace_the_frozen_crosshair_when_the_mouse_leaves(qapp):
    canvas = _canvas()
    pane = canvas._panes["main"]
    assert pane._crosshair_x is None

    # No crosshair at all -> cache key None -> newest values.
    newest = _value_map(pane._value_cache_for(None))
    assert pane._value_cache[0] is None
    assert newest["price"].value == pytest.approx(canvas.window_data.bars[-1].close)

    # Hovering switches the readout to the marked bar (cache key = bar index) ...
    canvas._apply_crosshair("main", canvas.x_trans.bar_to_x(4.0), 100.0)
    assert pane._crosshair_bar_index == 4
    assert pane._value_cache_for(4)[0].value == pytest.approx(104.5)
    assert pane._value_cache[0] == 4

    # ... and leaving the pane drops back to the newest values.
    canvas._apply_crosshair("main", -1.0, -1.0)
    assert pane._crosshair_x is None
    assert _value_map(pane._value_cache_for(None))["price"].value == pytest.approx(
        canvas.window_data.bars[-1].close
    )


def test_window_deactivation_drops_the_crosshair_for_the_idle_readout(qapp):
    canvas = _canvas()
    pane = canvas._panes["main"]
    canvas._apply_crosshair("main", canvas.x_trans.bar_to_x(6.0), 120.0)
    assert pane._crosshair_x is not None

    canvas.set_window_active(False)  # e.g. the user works in the Control Panel
    assert pane._crosshair_x is None
    assert pane._is_crosshair_active is False
    assert _value_map(pane._value_cache_for(None))["price"].value == pytest.approx(
        canvas.window_data.bars[-1].close
    )


def test_chart_window_forwards_activation_changes_to_the_canvas(qapp, monkeypatch):
    win = _chart_window()
    pane = win.canvas._panes["main"]
    win.canvas._apply_crosshair("main", win.canvas.x_trans.bar_to_x(5.0), 50.0)
    assert pane._crosshair_x is not None

    monkeypatch.setattr(win, "isActiveWindow", lambda: False)
    win.changeEvent(QEvent(QEvent.Type.ActivationChange))
    assert pane._crosshair_x is None
    win.deleteLater()


# ── One box per curve of the pane ────────────────────────────────────────


def test_every_curve_of_the_pane_gets_a_box(qapp):
    bars = _bars()
    overlays = [
        _overlay("sma50", values=[(b.t_open, 101.0 + i) for i, b in enumerate(bars)],
                 style={"color": "#2962FF"}),
        _overlay("sma200", values=[(b.t_open, 95.0 + i) for i, b in enumerate(bars)],
                 style={"color": "#FF6D00"}),
        _overlay("bb", values=[(b.t_open, 110.0 + i, 90.0 + i) for i, b in enumerate(bars)],
                 style={"color": "#26A69A"}, ov_type="band"),
    ]
    canvas = _canvas(overlays=overlays)
    pane = canvas._panes["main"]

    values = _value_map(pane.crosshair_values(7))
    assert set(values) == {"price", "sma50", "sma200", "bb:upper", "bb:lower"}
    assert values["sma50"].value == pytest.approx(108.0)
    assert values["sma50"].text == "108.00"
    assert values["sma50"].color == "#2962FF"
    assert values["bb:upper"].value == pytest.approx(117.0)
    assert values["bb:lower"].value == pytest.approx(97.0)


def test_marker_overlays_get_no_box(qapp):
    bars = _bars()
    canvas = _canvas(overlays=[_overlay("mark", values=[(b.t_open, 100.0) for b in bars],
                                       ov_type="marker")])
    pane = canvas._panes["main"]
    assert set(_value_map(pane.crosshair_values(3))) == {"price"}


def test_missing_value_hides_that_curve(qapp):
    bars = _bars()
    late = _overlay("late", values=[(b.t_open, 50.0) for b in bars[10:]])
    canvas = _canvas(overlays=[late])
    pane = canvas._panes["main"]

    assert "late" not in _value_map(pane.crosshair_values(3))
    assert "late" in _value_map(pane.crosshair_values(12))


def test_nearest_point_tolerance_is_configurable(qapp):
    bars = _bars()
    ov = _overlay("gap", values=[(bars[7].t_open, 42.0)])

    default_canvas = _canvas(overlays=[ov])
    assert "gap" in _value_map(default_canvas._panes["main"].crosshair_values(8))

    strict_canvas = _canvas(overlays=[ov], config=ViewerConfig(crosshair_value_box_snap_bars=0.0))
    assert "gap" not in _value_map(strict_canvas._panes["main"].crosshair_values(8))


# ── Layout ───────────────────────────────────────────────────────────────


def _six_overlays():
    bars = _bars()
    return [
        _overlay("l%d" % i, values=[(b.t_open, 100.0 + i * 0.1) for b in bars])
        for i in range(6)
    ]


def test_layout_boxes_do_not_overlap_and_stay_inside(qapp):
    canvas = _canvas(overlays=_six_overlays())
    pane = canvas._panes["main"]
    pane.y_trans.viewport_height_px = 400.0
    pane.y_trans.fit_range(90.0, 115.0)

    values = pane.crosshair_values(10)
    rects = [r for _, r in pane._layout_value_boxes(values, 400.0, pane._value_box_font(QFont()))]
    assert len(rects) == len(values) == 7  # price + 6 curves

    ordered = sorted(rects, key=lambda r: r.top())
    for a, b in zip(ordered, ordered[1:]):
        assert a.bottom() <= b.top() + 0.01
    for r in ordered:
        assert r.top() >= 0.0
        assert r.bottom() <= 400.0


def test_layout_survives_a_tiny_pane(qapp):
    canvas = _canvas(overlays=_six_overlays())
    pane = canvas._panes["main"]
    pane.y_trans.viewport_height_px = 60.0
    pane.y_trans.fit_range(90.0, 115.0)

    rects = [r for _, r in pane._layout_value_boxes(
        pane.crosshair_values(10), 60.0, pane._value_box_font(QFont()))]
    assert rects
    for r in rects:
        assert r.top() >= 0.0
        assert r.bottom() <= 60.0


def test_font_is_coupled_to_the_x_axis_font(qapp):
    """Readout font = X-axis font x config factor x VALUE_BOX_SCALE."""
    assert X_AXIS_FONT_PT == 9.0
    canvas = _canvas()
    default_factor = ViewerConfig().crosshair_value_box_font_factor
    assert canvas._panes["main"]._value_box_font(QFont()).pointSizeF() == pytest.approx(
        X_AXIS_FONT_PT * default_factor * VALUE_BOX_SCALE
    )

    big = _canvas(config=ViewerConfig(crosshair_value_box_font_factor=2.0))
    assert big._panes["main"]._value_box_font(QFont()).pointSizeF() == pytest.approx(
        X_AXIS_FONT_PT * 2.0 * VALUE_BOX_SCALE
    )


def test_value_boxes_are_about_thirty_percent_smaller(qapp, monkeypatch):
    """VALUE_BOX_SCALE shrinks the whole field (font + chrome), not just the text."""
    import chart_viewer.ui.pane as pane_module

    assert VALUE_BOX_SCALE == pytest.approx(0.7)

    canvas = _canvas()
    pane = canvas._panes["main"]
    pane.y_trans.viewport_height_px = 400.0
    pane.y_trans.fit_range(90.0, 115.0)
    values = pane.crosshair_values(3)

    monkeypatch.setattr(pane_module, "VALUE_BOX_SCALE", 1.0)  # original layout
    (_, full), = pane._layout_value_boxes(values, 400.0, pane._value_box_font(QFont()))
    monkeypatch.setattr(pane_module, "VALUE_BOX_SCALE", VALUE_BOX_SCALE)
    (_, small), = pane._layout_value_boxes(values, 400.0, pane._value_box_font(QFont()))

    assert 0.6 * full.height() <= small.height() <= 0.8 * full.height()
    assert 0.5 * full.width() <= small.width() <= 0.8 * full.width()


# ── Crosshair sources ────────────────────────────────────────────────────


def test_remote_crosshair_shows_values(qapp):
    bars = _bars()
    canvas = _canvas(overlays=[_overlay("sma", values=[(b.t_open, 100.0) for b in bars])])
    canvas._apply_crosshair_remote(canvas.x_trans.bar_to_x(7.0), 7)

    pane = canvas._panes["main"]
    assert pane._crosshair_bar_index == 7
    assert pane._is_crosshair_active is False
    assert pane._crosshair_y is None
    assert _value_map(pane.crosshair_values(7))["sma"].value == pytest.approx(100.0)


def test_badge_and_boxes_share_one_bar_index(qapp):
    canvas = _canvas()
    pane = canvas._panes["main"]
    px = canvas.x_trans.bar_to_x(12.0) + 2.0  # inside the candle body of bar 12

    pane.set_crosshair(px, None, False, None)
    assert pane._resolve_crosshair_bar_index(px) == 12  # nearest-neighbour fallback

    pane.set_crosshair(px, None, False, 3)
    assert pane._resolve_crosshair_bar_index(px) == 3   # authoritative index wins


# ── Multi-pane ───────────────────────────────────────────────────────────


def test_each_pane_shows_its_own_curves(qapp):
    bars = _bars()
    volume = _overlay(
        "volume", pane="volume", ov_type="histogram",
        values=[(b.t_open, b.volume) for b in bars],
        style={"color": "#546E7A"},
    )
    canvas = _canvas(overlays=[volume])
    assert set(canvas._panes) == {"main", "volume"}

    main = canvas._panes["main"]
    vol_pane = canvas._panes["volume"]
    canvas._apply_crosshair("main", canvas.x_trans.bar_to_x(4.0), 100.0)

    assert main._crosshair_bar_index == 4
    assert vol_pane._crosshair_bar_index == 4
    assert set(_value_map(main.crosshair_values(4))) == {"price"}
    assert set(_value_map(vol_pane.crosshair_values(4))) == {"volume"}
    assert _value_map(vol_pane.crosshair_values(4))["volume"].value == pytest.approx(
        bars[4].volume
    )


# ── Pixel smoke test (boxes really reach the Y-axis gutter) ──────────────


def _gutter_magenta_hits(pane):
    img = pane.grab().toImage()
    gutter_x = max(0, int(pane.width() - Y_AXIS_WIDTH))
    hits = []
    for y in range(img.height()):
        for x in range(gutter_x, img.width()):
            px = img.pixelColor(x, y)
            if px.red() >= 225 and px.green() <= 30 and px.blue() >= 225:
                hits.append((x, y))
    return hits


def test_renderer_paints_boxes_without_and_with_a_crosshair(qapp):
    bars = _bars()
    # A curve that stays inside the visible price range at both ends.
    overlay_value = lambda i: 105.5 + i * 0.3
    canvas = _canvas(
        overlays=[_overlay("mag", values=[(b.t_open, overlay_value(i))
                                          for i, b in enumerate(bars)])]
    )
    canvas.show()
    qapp.processEvents()
    pane = canvas._panes["main"]
    pane.resize(900, 600)
    qapp.processEvents()

    # 1) No crosshair: the boxes stay visible and read the NEWEST value.
    hits = _gutter_magenta_hits(pane)
    assert hits, "ohne Crosshair muss die Wert-Box den neuesten Wert zeigen"
    expected_y = pane.y_trans.price_to_y(overlay_value(len(bars) - 1))
    mean_y = sum(y for _, y in hits) / float(len(hits))
    assert abs(mean_y - expected_y) <= 30.0

    # 2) Crosshair: the same boxes read the marked bar instead of the newest.
    canvas._apply_crosshair("main", canvas.x_trans.bar_to_x(10.0), 100.0)
    qapp.processEvents()

    hits = _gutter_magenta_hits(pane)
    assert hits, "mit Crosshair muss die Wert-Box (Kurvenfarbe) im Gutter erscheinen"

    expected_y = pane.y_trans.price_to_y(overlay_value(10))
    mean_y = sum(y for _, y in hits) / float(len(hits))
    assert abs(mean_y - expected_y) <= 30.0
