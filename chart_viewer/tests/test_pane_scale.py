"""Tests for the per-pane LOG/LIN Y-scale button and its preset persistence.

Contract:
- Every pane carries its own LOG/LIN button in the Y-axis gutter, below the
  AUTO/MANUAL pill, and toggles only its own Y scale.
- Log on non-positive values falls back to linear and is flagged (amber) instead
  of plotting nonsense.
- The mapping {pane_id: "linear"|"log"} travels with the preset snapshot and a
  user click is reported back to the agent, who persists it in the preset.
"""

import time

import pytest
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QMouseEvent

from chart_viewer.config import ViewerConfig
from chart_viewer.core.state_manager import StateManager, WindowData
from chart_viewer.models.entities import Bar, Overlay, OverlayPoint
from chart_viewer.models.envelope import MessageKind, make_envelope
from chart_viewer.ui.canvas import ChartCanvas
from chart_viewer.ui.pane import Y_AXIS_WIDTH, Y_SCALE_BUTTON_HEIGHT, Y_SCALE_BUTTON_BOTTOM_PX

BAR_COUNT = 30
T0 = 1_700_000_000
DAY = 86_400


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


def _overlay(ov_id, pane="main", values=(), ov_type="line"):
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
        style={"color": "#26A69A"},
    )


def _canvas(overlays=(), bars=None, pane_scales=None, width=900, height=600):
    canvas = ChartCanvas(window_id="w", config=ViewerConfig())
    canvas.resize(width, height)
    win = WindowData("w")
    win.bars = bars if bars is not None else _bars()
    win.overlays = {ov.overlay_id: ov for ov in overlays}
    if pane_scales is not None:
        win.pane_scales = dict(pane_scales)
    canvas.set_window_data(win)
    for pane in canvas._panes.values():
        pane.resize(width, height)
    return canvas


def _click(pane, pos):
    pane.mousePressEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(pos),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


def _volume_overlay(values=None):
    if values is None:
        values = [(T0 + i * DAY, 1_000.0 + i) for i in range(20)]
    return _overlay("volume", pane="volume", values=values, ov_type="histogram")


# -- Button geometry -------------------------------------------------------


def test_scale_button_sits_in_the_gutter_above_the_mode_pill(qapp):
    canvas = _canvas()
    pane = canvas._panes["main"]
    pane.draw_x_axis = False

    rect = pane.y_scale_button_rect()
    chart_w = pane.width() - Y_AXIS_WIDTH

    assert rect.left() >= chart_w
    assert rect.right() <= pane.width()
    assert rect.height() == Y_SCALE_BUTTON_HEIGHT
    # Der AUTO/MANUAL-Pill belegt die untersten 20 px.
    assert rect.bottom() == pane.height() - (Y_SCALE_BUTTON_BOTTOM_PX - Y_SCALE_BUTTON_HEIGHT)


def test_every_pane_has_its_own_button(qapp):
    canvas = _canvas(overlays=[_volume_overlay()])
    assert set(canvas._panes) == {"main", "volume"}
    # Beide Panes starten linear und haben eine eigene Taste.
    assert all(p.y_scale_type == "linear" for p in canvas._panes.values())
    assert all(p.y_scale_button_rect().isValid() for p in canvas._panes.values())


# -- Click behaviour -------------------------------------------------------


def test_click_toggles_log_and_reports_to_the_canvas(qapp):
    canvas = _canvas()
    pane = canvas._panes["main"]
    seen = []
    canvas.pane_scale_changed.connect(lambda pane_id, scale: seen.append((pane_id, scale)))

    _click(pane, pane.y_scale_button_rect().center())

    assert pane.y_scale_type == "log"
    assert pane.y_trans.mode == "log"
    assert seen == [("main", "log")]
    assert pane._is_measuring is False  # Taste ist kein Messwerkzeug-Start
    assert canvas.pane_scales() == {"main": "log"}


def test_second_click_returns_to_linear(qapp):
    canvas = _canvas()
    pane = canvas._panes["main"]
    seen = []
    canvas.pane_scale_changed.connect(lambda pane_id, scale: seen.append((pane_id, scale)))

    _click(pane, pane.y_scale_button_rect().center())
    _click(pane, pane.y_scale_button_rect().center())

    assert pane.y_scale_type == "linear"
    assert pane.y_trans.mode == "linear"
    assert seen == [("main", "log"), ("main", "linear")]


def test_click_on_the_button_does_not_touch_other_panes(qapp):
    canvas = _canvas(overlays=[_volume_overlay()])
    main_pane = canvas._panes["main"]

    _click(main_pane, main_pane.y_scale_button_rect().center())

    assert canvas._panes["main"].y_scale_type == "log"
    assert canvas._panes["volume"].y_scale_type == "linear"


def test_log_is_ratio_linear_where_linear_is_difference_linear(qapp):
    canvas = _canvas()
    pane = canvas._panes["main"]

    pane.set_y_scale_type("linear")
    lin_a = pane.y_trans.price_to_y(100.0) - pane.y_trans.price_to_y(110.0)
    lin_b = pane.y_trans.price_to_y(110.0) - pane.y_trans.price_to_y(120.0)

    pane.set_y_scale_type("log")
    log_a = pane.y_trans.price_to_y(100.0) - pane.y_trans.price_to_y(110.0)
    log_b = pane.y_trans.price_to_y(110.0) - pane.y_trans.price_to_y(121.0)

    # linear: gleiche Differenz -> gleicher Pixelabstand
    assert lin_a == pytest.approx(lin_b)
    # log: gleiches Verhaeltnis (1.1) -> gleicher Pixelabstand
    assert log_a == pytest.approx(log_b)
    # und die beiden Skalen sind wirklich verschieden
    assert log_a != pytest.approx(lin_a, rel=0.01)


def test_log_falls_back_to_linear_on_non_positive_values(qapp):
    overlay = _overlay(
        "spread",
        pane="spread",
        values=[(T0 + i * DAY, -5.0 + i) for i in range(20)],
    )
    canvas = _canvas(overlays=[overlay])
    pane = canvas._panes["spread"]

    pane.set_y_scale_type("log")

    assert pane.y_scale_type == "log"        # Wunsch bleibt gespeichert
    assert pane.y_trans.mode == "linear"     # gezeichnet wird linear
    assert pane.is_log_forced() is True
    assert pane.is_log_effective() is False


# -- Preset / snapshot plumbing -------------------------------------------


def test_preset_pane_scales_are_applied_to_the_panes(qapp):
    canvas = _canvas(overlays=[_volume_overlay()], pane_scales={"main": "log", "volume": "log"})

    assert canvas._panes["main"].y_scale_type == "log"
    assert canvas._panes["volume"].y_scale_type == "log"
    assert canvas.pane_scales() == {"main": "log", "volume": "log"}


def test_apply_pane_scales_never_echoes_back_to_the_agent(qapp):
    canvas = _canvas()
    seen = []
    canvas.pane_scale_changed.connect(lambda pane_id, scale: seen.append((pane_id, scale)))

    canvas.apply_pane_scales({"main": "log"})

    assert canvas._panes["main"].y_scale_type == "log"
    assert seen == []  # Preset/Snapshot ist keine Nutzeraktion


def test_apply_pane_scales_resets_unlisted_panes_to_linear(qapp):
    canvas = _canvas(overlays=[_volume_overlay()], pane_scales={"main": "log", "volume": "log"})

    canvas.apply_pane_scales({"volume": "linear"})

    assert canvas._panes["main"].y_scale_type == "linear"
    assert canvas._panes["volume"].y_scale_type == "linear"


def test_snapshot_sanitizes_pane_scales_into_window_data():
    manager = StateManager()
    win = manager.apply_snapshot(
        "w1",
        {"symbol": "T", "bars": [], "overlays": [], "pane_scales": {"Main": "LOG", "volume": "bogus", "": "log"}},
    )

    assert win.pane_scales == {"main": "log", "volume": "linear"}


def test_snapshot_without_pane_scales_keeps_the_previous_mapping():
    manager = StateManager()
    manager.apply_snapshot("w1", {"symbol": "T", "pane_scales": {"main": "log"}})

    win = manager.apply_snapshot("w1", {"symbol": "T"})

    assert win.pane_scales == {"main": "log"}


def test_snapshot_with_empty_pane_scales_resets_to_linear():
    manager = StateManager()
    manager.apply_snapshot("w1", {"symbol": "T", "pane_scales": {"main": "log"}})

    win = manager.apply_snapshot("w1", {"symbol": "T", "pane_scales": {}})

    assert win.pane_scales == {}


def test_window_forwards_the_complete_pane_map(qapp):
    from chart_viewer.ui.window import ChartWindow

    win = ChartWindow(window_id="w1", config=ViewerConfig())
    seen = []
    win.pane_scale_changed.connect(lambda window_id, scales: seen.append((window_id, scales)))

    win.canvas.pane_scale_changed.emit("main", "log")

    assert seen == [("w1", win.canvas.pane_scales())]
    win.close()


# -- Orchestrator: preset -> snapshot -------------------------------------


def test_orchestrator_carries_pane_scales_from_the_preset(monkeypatch):
    import chart_viewer.orchestrator as orch

    monkeypatch.setattr(
        orch,
        "resolve_preset",
        lambda name: {
            "indicators": [],
            "topbar_metrics": [],
            "pane_scales": {"main": "log", "Volume": "log", "rs": "unknown"},
        },
    )
    monkeypatch.setattr(
        orch,
        "fetch_chart_data",
        lambda symbol, timeframe="1D", limit=2000: {
            "status": "ok",
            "columns": ["timestamp", "open", "high", "low", "close", "volume"],
            "data": [[T0, 100.0, 101.0, 99.0, 100.5, 1_000_000.0]],
            "features_stale": False,
        },
    )
    monkeypatch.setattr(orch, "_pca_get", lambda path: {"features": []})

    cmd = orch.build_display_stock("TEST", preset="qmaggi", window_id="w1")

    assert cmd["pane_scales"] == {"main": "log", "volume": "log", "rs": "linear"}


def test_orchestrator_without_preset_has_no_pane_scales(monkeypatch):
    import chart_viewer.orchestrator as orch

    monkeypatch.setattr(
        orch,
        "fetch_chart_data",
        lambda symbol, timeframe="1D", limit=2000: {
            "status": "ok",
            "columns": ["timestamp", "open", "high", "low", "close", "volume"],
            "data": [[T0, 100.0, 101.0, 99.0, 100.5, 1_000_000.0]],
            "features_stale": False,
        },
    )
    monkeypatch.setattr(orch, "_pca_get", lambda path: {"features": []})

    cmd = orch.build_display_stock("TEST", window_id="w1")

    assert cmd["pane_scales"] == {}


# -- Agent: viewer click -> preset ----------------------------------------


class _FakeTransport:
    def __init__(self):
        self.on_event_handler = None
        self.sent = []

    def on_event(self, handler):
        self.on_event_handler = handler

    def connect(self):
        pass

    def send_command(self, envelope):
        self.sent.append(envelope)


class _RecordingControlService:
    def __init__(self):
        self.calls = []

    def set_pane_scales(self, preset_id, pane_scales):
        self.calls.append((preset_id, pane_scales))
        return {"pane_scales": pane_scales}


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _agent_with_recorder(ledger_entry):
    from chart_viewer.agent.agent_client import ChartAgent

    agent = ChartAgent(transport=_FakeTransport())
    agent.control_service.shutdown()
    recorder = _RecordingControlService()
    agent.control_service = recorder
    agent.layout_ledger["w1"] = dict(ledger_entry)
    agent.series_data["w1"] = {"symbol": "TEST"}
    return agent, recorder


def test_agent_persists_pane_scales_in_the_active_preset():
    agent, recorder = _agent_with_recorder({"window_id": "w1", "symbol": "TEST", "preset": "qmaggi"})

    agent._handle_pane_scales_changed("w1", {"MAIN": "log", "volume": "linear"})

    expected = {"main": "log", "volume": "linear"}
    assert agent.layout_ledger["w1"]["pane_scales"] == expected
    assert agent.series_data["w1"]["pane_scales"] == expected
    assert _wait_for(lambda: recorder.calls)
    assert recorder.calls == [("qmaggi", expected)]


def test_agent_handles_the_viewer_envelope():
    agent, recorder = _agent_with_recorder({"window_id": "w1", "symbol": "TEST", "preset": "qmaggi"})

    agent._on_envelope(
        make_envelope(
            msg_type="window.pane_scales_changed",
            payload={"window_id": "w1", "pane_scales": {"main": "log"}},
            kind=MessageKind.EVENT,
            window_id="w1",
        )
    )

    assert _wait_for(lambda: recorder.calls)
    assert recorder.calls == [("qmaggi", {"main": "log"})]


def test_agent_without_preset_keeps_the_scales_local():
    agent, recorder = _agent_with_recorder({"window_id": "w1", "symbol": "TEST"})

    agent._handle_pane_scales_changed("w1", {"main": "log"})

    assert agent.layout_ledger["w1"]["pane_scales"] == {"main": "log"}
    assert recorder.calls == []

# -- Full in-process round trip: pane click -> agent -> preset ------------


def _wait_until(qapp, predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_in_process_round_trip_persists_the_click(qapp):
    """Viewer-Pane -> App -> Transport -> Agent -> Preset-Write (ohne Netz)."""
    from chart_viewer.agent.agent_client import ChartAgent
    from chart_viewer.transport.in_process import create_in_process_pair
    from chart_viewer.ui.app import ViewerApp

    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    agent.control_service.shutdown()
    recorder = _RecordingControlService()
    agent.control_service = recorder
    app = ViewerApp(config=ViewerConfig(control_panel_enabled=False), transport=viewer_transport)

    try:
        agent.start()
        app.start()
        qapp.processEvents()

        agent.open_window("w1", "TEST")
        # open_window legt den Ledger-Eintrag neu an -> Preset danach setzen
        # (genau so macht es run_server.py im DISPLAY_STOCK-Zweig).
        agent.layout_ledger["w1"]["preset"] = "qmaggi"
        agent.send_snapshot(
            "w1",
            {
                "symbol": "TEST",
                "timeframe": {"unit": "D", "multiplier": 1},
                "bars": [
                    {
                        "t_open": T0 + i * DAY,
                        "t_close": T0 + (i + 1) * DAY,
                        "open": 100.0 + i,
                        "high": 102.0 + i,
                        "low": 99.0 + i,
                        "close": 100.5 + i,
                        "volume": 1_000_000.0,
                    }
                    for i in range(30)
                ],
                "overlays": [],
                "annotations": [],
                "pane_scales": {"main": "linear"},
            },
        )
        assert _wait_until(qapp, lambda: "w1" in app.windows)

        win = app.windows["w1"]
        pane = win.canvas._panes["main"]
        pane.resize(800, 500)

        _click(pane, pane.y_scale_button_rect().center())

        assert _wait_until(qapp, lambda: recorder.calls)
        assert recorder.calls == [("qmaggi", {"main": "log"})]
        assert agent.layout_ledger["w1"]["pane_scales"] == {"main": "log"}
    finally:
        app.shutdown()
        for window in list(app.windows.values()):
            window.close()

