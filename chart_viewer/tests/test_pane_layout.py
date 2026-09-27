"""Pane identity and the chart-definition pane layout (contract section 3).

Covers the pane order from the snapshot panes list, the incremental diff (widget
identity survives reorder/add/remove), role=price => main pane, the X-axis pane,
the splitter weights and the legacy overlay-derived behaviour of snapshots
without a panes list.
"""

import pytest
from PySide6.QtGui import QColor

from chart_viewer.config import ViewerConfig
from chart_viewer.core.state_manager import StateManager, WindowData
from chart_viewer.models.entities import Anchor, Annotation, Bar, Overlay, OverlayPoint
from chart_viewer.ui import canvas as canvas_module
from chart_viewer.ui.canvas import ChartCanvas

T0 = 1_700_000_000
DAY = 86_400
BAR_COUNT = 30


@pytest.fixture(autouse=True)
def _isolated_splitter_prefs(tmp_path, monkeypatch):
    """Never read/write the real ~/.chart_viewer_splitter.json from tests."""
    monkeypatch.setattr(canvas_module, "_SPLITTER_PREFS_PATH", str(tmp_path / "splitter.json"))


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


def _overlay(ov_id, pane, ov_type="line"):
    return Overlay(
        overlay_id=ov_id,
        series_id="s",
        type=ov_type,
        pane=pane,
        values=[OverlayPoint(t=T0 + i * DAY, value=100.0 + i) for i in range(20)],
        style={"color": "#26A69A"},
    )


def _price(pane_id="main", **extra):
    entry = {"pane_id": pane_id, "role": "price"}
    entry.update(extra)
    return entry


def _value(pane_id, **extra):
    entry = {"pane_id": pane_id, "role": "value"}
    entry.update(extra)
    return entry


def _win(panes=None, overlays=(), x_axis_pane=None, pane_scales=None, window_id="w"):
    win = WindowData(window_id)
    win.bars = _bars()
    win.overlays = {ov.overlay_id: ov for ov in overlays}
    win.panes = list(panes or [])
    win.x_axis_pane = x_axis_pane
    if pane_scales is not None:
        win.pane_scales = dict(pane_scales)
    return win


def _canvas(win, width=900, height=600):
    canvas = ChartCanvas(window_id=win.window_id, config=ViewerConfig())
    canvas.resize(width, height)
    canvas.set_window_data(win)
    for pane in canvas._panes.values():
        pane.resize(width, height)
    return canvas


def _canvas_with_capture(win, width=900, height=600):
    """Canvas whose splitter calls are recorded before the first set_window_data."""
    canvas = ChartCanvas(window_id=win.window_id, config=ViewerConfig())
    canvas.resize(width, height)
    calls = {"stretch": [], "sizes": [], "restore": [], "total": None}
    original_stretch = canvas._splitter.setStretchFactor
    original_sizes = canvas._splitter.setSizes

    def record_stretch(index, factor):
        calls["stretch"].append((index, factor))
        return original_stretch(index, factor)

    def record_sizes(sizes):
        calls["sizes"].append(list(sizes))
        calls["total"] = canvas._splitter.height() or 700
        return original_sizes(sizes)

    canvas._splitter.setStretchFactor = record_stretch
    canvas._splitter.setSizes = record_sizes
    canvas._restore_splitter_sizes = lambda count: calls["restore"].append(count)
    canvas.set_window_data(win)
    return canvas, calls


def _pane_ids(canvas):
    return [canvas._splitter.widget(i).pane_id for i in range(canvas._splitter.count())]


# -- Snapshot payload ------------------------------------------------------


def test_snapshot_reads_panes_chart_and_x_axis_pane():
    manager = StateManager()
    win = manager.apply_snapshot(
        "w1",
        {
            "symbol": "NVDA",
            "bars": [],
            "chart": {"id": "rs_chart", "display_name": "RS-Chart", "draft": False},
            "x_axis_pane": "rs",
            "panes": [
                {
                    "pane_id": "main",
                    "role": "price",
                    "title": "SMA 10-200",
                    "weight": 7,
                    "scale": "log",
                    "preset_id": "qmaggi__main",
                },
                {
                    "pane_id": "rs",
                    "role": "value",
                    "title": "RS-Monitor",
                    "weight": 2,
                    "scale": "linear",
                    "preset_id": "rs_monitor",
                },
            ],
        },
    )

    assert [pane["pane_id"] for pane in win.panes] == ["main", "rs"]
    assert win.panes[0]["title"] == "SMA 10-200"
    assert win.panes[0]["weight"] == 7
    assert win.chart == {"id": "rs_chart", "display_name": "RS-Chart", "draft": False}
    assert win.x_axis_pane == "rs"


def test_snapshot_decodes_zone_level_and_rules():
    manager = StateManager()
    win = manager.apply_snapshot(
        "w1",
        {
            "symbol": "T",
            "bars": [],
            "overlays": [
                {
                    "overlay_id": "zone_1",
                    "type": "zone",
                    "series_id": "s",
                    "pane": "rs",
                    "style": {"color": "#26A69A", "alpha": 38},
                    "rules": {"thresholds": [{"above": 80, "color": "#26A69A"}]},
                    "values": [
                        {"t": T0, "value": 30.0, "value2": 80.0, "t2": T0 + 5 * DAY},
                        {"t": T0 + DAY, "value": None, "value2": None, "t2": T0 + 6 * DAY},
                    ],
                },
                {
                    "overlay_id": "level_1",
                    "type": "level",
                    "series_id": "s",
                    "pane": "rs",
                    "style": {"label": "80", "dash": True},
                    "rules": {},
                    "values": [{"t": T0, "value": 80.0, "color_override": "#EF5350"}],
                },
            ],
        },
    )

    zone = win.overlays["zone_1"]
    assert zone.type == "zone"
    assert zone.rules == {"thresholds": [{"above": 80, "color": "#26A69A"}]}
    assert zone.values[0].t2 == T0 + 5 * DAY
    assert zone.values[1].value is None
    assert zone.values[1].t2 == T0 + 6 * DAY
    assert win.overlays["level_1"].type == "level"
    assert win.overlays["level_1"].values[0].color_override == "#EF5350"


def test_snapshot_without_the_new_fields_keeps_the_old_behaviour():
    manager = StateManager()
    win = manager.apply_snapshot("w1", {"symbol": "T", "bars": [], "overlays": []})

    assert win.panes == []
    assert win.chart == {}
    assert win.x_axis_pane is None


def test_snapshot_full_replaces_a_previous_pane_list():
    manager = StateManager()
    manager.apply_snapshot("w1", {"symbol": "T", "panes": [_price()]})

    win = manager.apply_snapshot("w1", {"symbol": "T"})

    assert win.panes == []


# -- Pane order ------------------------------------------------------------


def test_pane_order_comes_from_the_panes_list(qapp):
    overlays = [_overlay("rs_curve", "rs"), _overlay("vol", "volume"), _overlay("sma", "main")]
    win = _win(
        panes=[_price(weight=7), _value("rs", weight=2), _value("volume", weight=1)],
        overlays=overlays,
    )

    canvas = _canvas(win)

    assert canvas._pane_order == ["main", "rs", "volume"]
    assert _pane_ids(canvas) == ["main", "rs", "volume"]


def test_panes_can_be_ordered_freely(qapp):
    win = _win(panes=[_value("rs"), _price("main"), _value("volume")])

    canvas = _canvas(win)

    assert canvas._pane_order == ["rs", "main", "volume"]
    assert canvas._panes["main"].is_main is True
    assert canvas._panes["rs"].is_main is False


def test_legacy_snapshot_derives_the_pane_order_from_the_overlays(qapp):
    win = _win(overlays=[_overlay("sma", "main"), _overlay("rs_curve", "rs"), _overlay("vol", "volume")])

    canvas = _canvas(win)

    assert canvas._pane_order == ["main", "rs", "volume"]


def test_legacy_snapshot_without_overlays_has_only_the_main_pane(qapp):
    canvas = _canvas(_win())

    assert canvas._pane_order == ["main"]
    assert canvas._panes["main"].is_main is True


# -- Roles -----------------------------------------------------------------


def test_role_price_is_the_main_pane_regardless_of_its_id(qapp):
    win = _win(panes=[_value("rs_pane"), _price("price_pane")])

    canvas = _canvas(win)

    assert canvas._panes["price_pane"].is_main is True
    assert canvas._panes["price_pane"].role == "price"
    assert canvas._panes["rs_pane"].is_main is False
    assert canvas._panes["rs_pane"].role == "value"
    assert canvas._price_pane() is canvas._panes["price_pane"]


def test_legacy_main_id_is_still_the_price_pane(qapp):
    canvas = _canvas(_win(panes=[{"pane_id": "main"}]))

    assert canvas._panes["main"].is_main is True
    assert canvas._panes["main"].role == "price"


def test_first_pane_becomes_price_when_no_role_says_so(qapp):
    canvas = _canvas(_win(panes=[_value("a"), _value("b")]))

    assert canvas._panes["a"].is_main is True
    assert canvas._panes["b"].is_main is False


def test_set_annotations_targets_the_price_pane(qapp):
    canvas = _canvas(_win(panes=[_price("price_pane"), _value("other")]))
    annotation = Annotation(
        id="a1", type="hline", anchors=[Anchor(price=100.0, mode="data")], style={}
    )

    canvas.set_annotations({"a1": annotation})

    assert "a1" in canvas._panes["price_pane"].annotations
    assert canvas._panes["other"].annotations == {}


# -- Incremental diff ------------------------------------------------------


def test_pane_widgets_survive_a_reorder(qapp):
    canvas = _canvas(_win(panes=[_price("main"), _value("rs")]))
    main_pane = canvas._panes["main"]
    rs_pane = canvas._panes["rs"]

    canvas.set_window_data(_win(panes=[_value("rs"), _price("main")]))

    # Same widgets, only moved -- no deleteLater / rebuild.
    assert canvas._panes["main"] is main_pane
    assert canvas._panes["rs"] is rs_pane
    assert main_pane.parent() is canvas._splitter
    assert canvas._pane_order == ["rs", "main"]
    assert _pane_ids(canvas) == ["rs", "main"]


def test_new_panes_are_inserted_at_their_position(qapp):
    canvas = _canvas(_win(panes=[_price("main"), _value("rs")]))
    main_pane = canvas._panes["main"]
    rs_pane = canvas._panes["rs"]

    canvas.set_window_data(_win(panes=[_price("main"), _value("volume"), _value("rs")]))

    assert canvas._pane_order == ["main", "volume", "rs"]
    assert canvas._panes["main"] is main_pane
    assert canvas._panes["rs"] is rs_pane
    volume_pane = canvas._panes["volume"]
    assert canvas._splitter.widget(1) is volume_pane
    assert volume_pane.role == "value"
    assert volume_pane.parent() is canvas._splitter


def test_removed_panes_are_dropped(qapp):
    canvas = _canvas(_win(panes=[_price("main"), _value("rs"), _value("volume")]))
    main_pane = canvas._panes["main"]

    canvas.set_window_data(_win(panes=[_price("main"), _value("volume")]))

    assert canvas._pane_order == ["main", "volume"]
    assert canvas._panes["main"] is main_pane
    assert "rs" not in canvas._panes
    assert _pane_ids(canvas) == ["main", "volume"]


def test_reused_panes_keep_their_shared_x_transform_and_signals(qapp):
    canvas = _canvas(_win(panes=[_price("main"), _value("rs")]))
    rs_pane = canvas._panes["rs"]
    seen = []
    canvas.crosshair_moved.connect(lambda ts, idx: seen.append((ts, idx)))

    canvas.set_window_data(_win(panes=[_value("rs"), _price("main")]))

    assert rs_pane.x_trans is canvas.x_trans
    # The crosshair path of the canvas is still wired to the reused pane.
    rs_pane.crosshair_moved_signal.emit("rs", float(canvas.x_trans.bar_to_x(3.0)), 10.0)
    assert seen and seen[-1][1] == 3


def test_overlay_routing_follows_the_pane_ids(qapp):
    canvas = _canvas(_win(panes=[_price("main"), _value("rs")], overlays=[_overlay("rs_curve", "rs")]))
    assert set(canvas._panes["rs"].overlays) == {"rs_curve"}

    canvas.set_window_data(
        _win(
            panes=[_price("main"), _value("rs")],
            overlays=[_overlay("sma", "main"), _overlay("rs_curve2", "rs")],
        )
    )

    assert set(canvas._panes["main"].overlays) == {"sma"}
    assert set(canvas._panes["rs"].overlays) == {"rs_curve2"}


# -- X axis ----------------------------------------------------------------


def test_x_axis_pane_from_the_snapshot(qapp):
    win = _win(
        panes=[_price("main"), _value("rs"), _value("volume")],
        x_axis_pane="rs",
    )

    canvas = _canvas(win)

    assert canvas._panes["rs"].draw_x_axis is True
    assert canvas._panes["main"].draw_x_axis is False
    assert canvas._panes["volume"].draw_x_axis is False


def test_x_axis_falls_back_to_the_bottom_pane(qapp):
    canvas = _canvas(_win(panes=[_price("main"), _value("rs"), _value("volume")]))

    assert canvas._panes["volume"].draw_x_axis is True
    assert canvas._panes["main"].draw_x_axis is False
    assert canvas._panes["rs"].draw_x_axis is False


def test_x_axis_with_unknown_pane_falls_back_to_the_bottom_pane(qapp):
    canvas = _canvas(_win(panes=[_price("main"), _value("rs")], x_axis_pane="missing"))

    assert canvas._panes["rs"].draw_x_axis is True
    assert canvas._panes["main"].draw_x_axis is False


def test_legacy_single_pane_keeps_the_x_axis(qapp):
    canvas = _canvas(_win())

    assert canvas._panes["main"].draw_x_axis is True


# -- Weights ---------------------------------------------------------------


def test_splitter_weights_come_from_the_snapshot(qapp):
    win = _win(panes=[_price("main", weight=7), _value("rs", weight=2), _value("volume", weight=2)])

    canvas, calls = _canvas_with_capture(win)

    assert calls["stretch"] == [(0, 7), (1, 2), (2, 2)]
    assert calls["restore"] == []  # snapshot weights win over the local prefs
    total = calls["total"]
    sizes = calls["sizes"][-1]
    assert sizes == [
        max(1, int(total * 7 / 11)),
        max(1, int(total * 2 / 11)),
        max(1, int(total * 2 / 11)),
    ]
    assert sizes[0] > sizes[1] == sizes[2]


def test_missing_weights_use_the_local_splitter_fallback(qapp):
    win = _win(panes=[_price("main"), _value("rs")])

    canvas, calls = _canvas_with_capture(win)

    assert calls["stretch"] == [(0, 7), (1, 2)]  # historical default stretch
    assert calls["restore"] == [2]
    assert calls["sizes"] == []


def test_legacy_snapshot_uses_the_local_splitter_fallback(qapp):
    canvas, calls = _canvas_with_capture(_win(overlays=[_overlay("vol", "volume")]))

    assert calls["restore"] == [2]


def test_live_update_with_the_same_weights_keeps_the_splitter(qapp):
    win = _win(panes=[_price("main", weight=7), _value("rs", weight=3)])
    canvas, calls = _canvas_with_capture(win)
    calls["stretch"].clear()
    calls["sizes"].clear()
    calls["restore"].clear()

    canvas.set_window_data(_win(panes=[_price("main", weight=7), _value("rs", weight=3)]))

    assert calls["stretch"] == []
    assert calls["sizes"] == []
    assert calls["restore"] == []


def test_weight_change_is_applied(qapp):
    win = _win(panes=[_price("main", weight=7), _value("rs", weight=3)])
    canvas, calls = _canvas_with_capture(win)
    calls["stretch"].clear()

    canvas.set_window_data(_win(panes=[_price("main", weight=5), _value("rs", weight=5)]))

    assert calls["stretch"] == [(0, 5), (1, 5)]


# -- Title -----------------------------------------------------------------


def test_pane_title_is_applied_and_rendered(qapp):
    canvas = _canvas(
        _win(panes=[_price("main"), _value("rs", title="RS-Monitor")])
    )
    pane = canvas._panes["rs"]
    pane.resize(800, 300)
    assert pane.title == "RS-Monitor"

    image = pane.grab().toImage()
    background = QColor(ViewerConfig().default_background_color)
    hits = sum(
        1
        for y in range(0, 18)
        for x in range(0, 140)
        if image.pixelColor(x, y) != background
    )
    assert hits > 0


def test_pane_without_title_draws_nothing(qapp):
    canvas = _canvas(_win(panes=[_price("main"), _value("rs")]))
    pane = canvas._panes["rs"]
    pane.resize(800, 300)
    assert pane.title == ""

    image = pane.grab().toImage()
    background = QColor(ViewerConfig().default_background_color)
    hits = sum(
        1
        for y in range(0, 18)
        for x in range(0, 140)
        if image.pixelColor(x, y) != background
    )
    assert hits == 0


def test_title_update_reaches_an_existing_pane(qapp):
    canvas = _canvas(_win(panes=[_price("main"), _value("rs", title="alt")]))
    pane = canvas._panes["rs"]

    canvas.set_window_data(_win(panes=[_price("main"), _value("rs", title="neu")]))

    assert pane.title == "neu"
    assert canvas._panes["rs"] is pane
