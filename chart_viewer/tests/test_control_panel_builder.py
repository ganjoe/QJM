"""Tests for the CHART-BUILDER section of the viewer control panel.

The builder talks to the agent exclusively through `control.request` /
`control.response` envelopes (contract: docs/architecture/chart-presets.md §4).
The server side is built in parallel, so this module drives the panel against a
synchronous fake that implements the new ops (list_panes, list_charts,
get_chart_state, compose_chart, apply_chart, save_chart).
"""

import gc
from typing import Any, Dict, List

import pytest
from PySide6.QtCore import QSettings

from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.config import ViewerConfig
from chart_viewer.models.envelope import MessageKind, make_envelope
from chart_viewer.transport.in_process import create_in_process_pair
from chart_viewer.ui.app import ViewerApp
from chart_viewer.ui.control_panel import ControlPanelWindow, derive_chart_id


PANE_PRESETS = [
    {
        "id": "qmaggi__main",
        "display_name": "SMA 10-200",
        "description": "",
        "role": "price",
        "kind": "indicator",
        "member_count": 4,
        "summary": "SMA 10/20/50/200",
    },
    {
        "id": "rs_monitor",
        "display_name": "RS-Monitor",
        "description": "",
        "role": "value",
        "kind": "indicator",
        "member_count": 2,
        "summary": "IBD RS + Revival",
    },
    {
        "id": "rsi_14",
        "display_name": "RSI 14",
        "description": "",
        "role": "any",
        "kind": "indicator",
        "member_count": 1,
        "summary": "",
    },
]
INITIAL_PANES = [
    {
        "pane_id": "main",
        "pane_preset_id": "qmaggi__main",
        "title": "SMA 10-200",
        "role": "price",
        "scale": "linear",
        "weight": 7,
    },
    {
        "pane_id": "rs_monitor",
        "pane_preset_id": "rs_monitor",
        "title": "RS-Monitor",
        "role": "value",
        "scale": "linear",
        "weight": 2,
    },
]
WINDOW = {"window_id": "win_nvda_1d", "symbol": "NVDA", "color_flag": 0}


class FakeControlService:
    """Synchronous stand-in for the agent-side ControlService (builder ops)."""

    def __init__(self, panel: ControlPanelWindow) -> None:
        self.panel = panel
        self.requests: List[Dict[str, Any]] = []
        self.states: Dict[str, Dict[str, Any]] = {}
        self.charts: Dict[str, Dict[str, Any]] = {}
        self.fail_ops: set = set()
        self.error_message = "kaputt"

    # ── transport plumbing ─────────────────────────────────────────────────

    def handle(self, request_id: str, op: str, params: Any) -> None:
        params = params or {}
        self.requests.append({"request_id": request_id, "op": op, "params": params})
        if op in self.fail_ops:
            payload = {
                "request_id": request_id,
                "op": op,
                "ok": False,
                "data": None,
                "error": {"code": "boom", "message": self.error_message},
            }
        else:
            payload = {
                "request_id": request_id,
                "op": op,
                "ok": True,
                "data": self._data(op, params),
                "error": None,
            }
        self.panel.apply_response(payload)

    def ops(self) -> List[str]:
        return [r["op"] for r in self.requests]

    def last(self, op: str) -> Dict[str, Any]:
        matching = [r for r in self.requests if r["op"] == op]
        assert matching, f"no '{op}' request was sent (got {self.ops()})"
        return matching[-1]

    # ── server state ───────────────────────────────────────────────────────

    def state(self, window_id: str) -> Dict[str, Any]:
        return self.states.setdefault(
            window_id,
            # No "symbol" key on purpose: the panel then keeps the symbol it got
            # with the window list (set_chart_windows).
            {
                "window_id": window_id,
                "chart_id": "",
                "draft": True,
                "panes": [dict(p) for p in INITIAL_PANES],
            },
        )

    def _data(self, op: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if op == "list_panes":
            return {"panes": [dict(p) for p in PANE_PRESETS]}
        if op == "list_charts":
            return {
                "charts": [
                    {
                        "id": c["id"],
                        "display_name": c["display_name"],
                        "description": "",
                        "pane_count": len(c.get("panes") or []),
                        "summary": "",
                    }
                    for c in self.charts.values()
                ]
            }
        if op == "get_chart_state":
            return dict(self.state(str(params.get("window_id") or "")))
        if op == "compose_chart":
            return self._compose(params)
        if op == "save_chart":
            return self._save(params)
        if op == "apply_chart":
            return self._apply(params)
        return {}

    def _compose(self, params: Dict[str, Any]) -> Dict[str, Any]:
        window_id = str(params.get("window_id") or "")
        state = dict(self.state(window_id))
        used = set()
        panes = []
        for raw in params.get("panes") or []:
            preset_id = str(raw.get("pane_preset_id") or "")
            pane_id = str(raw.get("pane_id") or preset_id)
            base = pane_id
            suffix = 2
            while pane_id in used:
                pane_id = f"{base}_{suffix}"
                suffix += 1
            used.add(pane_id)
            panes.append(
                {
                    "pane_id": pane_id,
                    "pane_preset_id": preset_id,
                    "title": preset_id,
                    "role": "",
                    "scale": str(raw.get("scale") or "linear"),
                    "weight": int(raw.get("weight") or 2),
                }
            )
        state["panes"] = panes
        state["draft"] = bool(params.get("draft", True))
        if state["draft"]:
            state["chart_id"] = str(params.get("base_chart_id") or "")
        self.states[window_id] = state
        return dict(state)

    def _save(self, params: Dict[str, Any]) -> Dict[str, Any]:
        window_id = str(params.get("window_id") or "")
        chart_id = str(params.get("chart_id") or "")
        state = dict(self.state(window_id))
        state["chart_id"] = chart_id
        state["draft"] = False
        self.states[window_id] = state
        self.charts[chart_id] = {
            "id": chart_id,
            "display_name": str(params.get("display_name") or chart_id),
            "panes": [dict(p) for p in state.get("panes") or []],
        }
        return dict(state)

    def _apply(self, params: Dict[str, Any]) -> Dict[str, Any]:
        window_id = str(params.get("window_id") or "")
        chart_id = str(params.get("chart_id") or "")
        state = dict(self.state(window_id))
        saved = self.charts.get(chart_id)
        if saved:
            state["panes"] = [dict(p) for p in saved.get("panes") or []]
        state["chart_id"] = chart_id
        state["draft"] = False
        self.states[window_id] = state
        return dict(state)


_APPS: List[Any] = []
_PANELS: List[Any] = []


@pytest.fixture(autouse=True)
def _builder_cleanup(qapp):
    yield
    while _PANELS:
        panel = _PANELS.pop()
        try:
            panel.shutdown()
        except RuntimeError:
            pass  # already deleted by Qt
    while _APPS:
        app = _APPS.pop()
        try:
            app.shutdown()
            for window in list(app.windows.values()):
                window.close()
        except RuntimeError:
            pass
    gc.collect()


def _panel(qapp, *, windows: bool = True, connected: bool = True):
    """Build a standalone panel wired to the synchronous fake."""
    gc.collect()
    settings = QSettings("QJM", "ChartViewer")
    settings.remove("panel")
    settings.sync()

    panel = ControlPanelWindow(config=ViewerConfig(control_panel_enabled=True))
    _PANELS.append(panel)
    fake = FakeControlService(panel)
    panel.control_request.connect(fake.handle)
    if connected:
        panel.set_connection_state(True)
        qapp.processEvents()
    if windows:
        panel.set_chart_windows([dict(WINDOW)])
        qapp.processEvents()
    return panel, fake


# ── catalog ────────────────────────────────────────────────────────────────


def test_catalog_fills_price_pane_and_add_dropdowns(qapp):
    panel, fake = _panel(qapp)

    assert fake.ops().count("list_panes") == 1
    assert fake.ops().count("list_charts") == 1

    price_ids = [panel.price_pane_combo.itemData(i) for i in range(panel.price_pane_combo.count())]
    assert price_ids == ["qmaggi__main", "rsi_14"], "only price|any presets belong in the price combo"
    assert panel.price_pane_combo.currentData() == "qmaggi__main"

    add_ids = sorted(panel.add_pane_combo.itemData(i) for i in range(panel.add_pane_combo.count()))
    assert add_ids == ["qmaggi__main", "rs_monitor", "rsi_14"]
    labels = [panel.add_pane_combo.itemText(i) for i in range(panel.add_pane_combo.count())]
    assert "RS-Monitor — IBD RS + Revival" in labels

    rows = [panel.builder_pane_list.item(i).text() for i in range(panel.builder_pane_list.count())]
    assert rows == [
        "main · SMA 10-200 · LIN · Gewicht 7",
        "rs_monitor · RS-Monitor · LIN · Gewicht 2",
    ]
    assert panel.builder_target_label.text() == "Ziel: win_nvda_1d · NVDA"
    assert panel.builder_draft_label.text() == "• unbenannt"


def test_catalog_is_cached_and_reloaded_on_demand(qapp):
    panel, fake = _panel(qapp)
    assert fake.ops().count("list_panes") == 1

    panel.builder_pane_list.setCurrentRow(1)
    panel.scale_toggle_btn.click()
    index = panel.price_pane_combo.findData("rsi_14")
    panel.price_pane_combo.setCurrentIndex(index)
    panel._on_price_pane_activated(index)

    assert fake.ops().count("list_panes") == 1, "widget interaction must not reload the catalog"
    assert fake.ops().count("list_charts") == 1

    panel.builder_reload_btn.click()
    assert fake.ops().count("list_panes") == 2
    assert fake.ops().count("list_charts") == 2


# ── composing ──────────────────────────────────────────────────────────────


def test_add_pane_composes_with_unique_pane_id(qapp):
    panel, fake = _panel(qapp)
    index = panel.add_pane_combo.findData("rs_monitor")
    panel.add_pane_combo.setCurrentIndex(index)
    panel.add_pane_btn.click()

    request = fake.last("compose_chart")
    panes = request["params"]["panes"]
    assert [p["pane_id"] for p in panes] == ["main", "rs_monitor", "rs_monitor_2"]
    assert [p["pane_preset_id"] for p in panes] == ["qmaggi__main", "rs_monitor", "rs_monitor"]
    assert request["params"]["window_id"] == "win_nvda_1d"
    assert request["params"]["draft"] is True
    # The panel renders the preview immediately and reloads the truth afterwards.
    assert fake.ops().count("get_chart_state") == 2
    assert panel.builder_pane_list.count() == 3
    assert "rs_monitor_2" in panel.builder_pane_list.item(2).text()


def test_price_pane_selection_replaces_main_preset(qapp):
    panel, fake = _panel(qapp)
    index = panel.price_pane_combo.findData("rsi_14")
    panel.price_pane_combo.setCurrentIndex(index)
    panel._on_price_pane_activated(index)

    panes = fake.last("compose_chart")["params"]["panes"]
    assert [p["pane_id"] for p in panes] == ["main", "rs_monitor"]
    assert panes[0]["pane_preset_id"] == "rsi_14"
    assert panes[0]["weight"] == 7, "the price pane keeps its weight"


def test_remove_pane_keeps_price_pane(qapp):
    panel, fake = _panel(qapp)
    panel.builder_pane_list.setCurrentRow(1)
    panel.remove_pane_btn.click()
    assert [p["pane_id"] for p in fake.last("compose_chart")["params"]["panes"]] == ["main"]

    panel.builder_pane_list.setCurrentRow(0)
    sent = len(fake.requests)
    panel.remove_pane_btn.click()
    assert "Preispane" in panel.status_label.text()
    assert len(fake.requests) == sent, "the price pane must not be composed away"


def test_move_pane_up_and_down(qapp):
    panel, fake = _panel(qapp)
    panel.builder_pane_list.setCurrentRow(1)

    panel.pane_up_btn.click()
    assert [p["pane_id"] for p in fake.last("compose_chart")["params"]["panes"]] == [
        "rs_monitor",
        "main",
    ]
    assert panel.builder_pane_list.currentRow() == 0, "the selection follows the moved pane"

    panel.pane_down_btn.click()
    assert [p["pane_id"] for p in fake.last("compose_chart")["params"]["panes"]] == [
        "main",
        "rs_monitor",
    ]
    assert panel.builder_pane_list.currentRow() == 1


def test_lin_log_toggles_the_scale_of_the_selected_pane(qapp):
    panel, fake = _panel(qapp)
    panel.builder_pane_list.setCurrentRow(1)

    panel.scale_toggle_btn.click()
    panes = fake.last("compose_chart")["params"]["panes"]
    assert panes[1]["scale"] == "log"
    assert "LOG" in panel.builder_pane_list.item(1).text()

    panel.scale_toggle_btn.click()
    assert fake.last("compose_chart")["params"]["panes"][1]["scale"] == "linear"


def test_volume_checkbox_adds_and_removes_the_builtin_pane(qapp):
    panel, fake = _panel(qapp)

    panel.volume_check.setChecked(True)
    panes = fake.last("compose_chart")["params"]["panes"]
    assert panes[-1] == {
        "pane_id": "volume",
        "pane_preset_id": "builtin:volume",
        "scale": "linear",
        "weight": 2,
    }
    assert "Volumen" in panel.builder_pane_list.item(2).text()

    panel.volume_check.setChecked(False)
    panes = fake.last("compose_chart")["params"]["panes"]
    assert all(p["pane_preset_id"] != "builtin:volume" for p in panes)
    assert panel.builder_pane_list.count() == 2


def test_save_chart_uses_the_chart_id_from_the_dialog(qapp, monkeypatch):
    panel, fake = _panel(qapp)
    asked: List[str] = []

    def fake_dialog(default: str) -> str:
        asked.append(default)
        return "Mein Chart"

    monkeypatch.setattr(panel, "_ask_chart_name", fake_dialog)
    panel.save_chart_btn.click()

    request = fake.last("save_chart")
    assert request["params"] == {
        "window_id": "win_nvda_1d",
        "chart_id": "mein_chart",
        "display_name": "Mein Chart",
    }
    assert asked == ["nvda_chart"]
    assert fake.ops().count("list_charts") == 2, "saving refreshes the chart catalog"
    assert "mein_chart" in panel.status_label.text()
    assert panel.builder_draft_label.text() == "", "a saved chart is no draft any more"


def test_weight_spin_sets_the_pane_weight(qapp):
    panel, fake = _panel(qapp)
    panel.builder_pane_list.setCurrentRow(1)
    qapp.processEvents()

    panel.pane_weight_spin.setValue(5)
    panel.pane_weight_spin.editingFinished.emit()
    qapp.processEvents()

    panes = fake.last("compose_chart")["params"]["panes"]
    assert [p["pane_id"] for p in panes] == ["main", "rs_monitor"]
    assert panes[1]["weight"] == 5
    assert panes[0]["weight"] == 7, "andere Panes bleiben unberuehrt"


def test_title_edit_writes_a_pane_override(qapp):
    panel, fake = _panel(qapp)
    panel.builder_pane_list.setCurrentRow(1)
    qapp.processEvents()

    panel.pane_title_edit.setText("RS neu")
    panel.pane_title_edit.editingFinished.emit()
    qapp.processEvents()

    panes = fake.last("compose_chart")["params"]["panes"]
    assert panes[1]["overrides"] == {"title": "RS neu"}
    assert "overrides" not in panes[0]


def test_duplicate_pane_gets_a_unique_slot(qapp):
    panel, fake = _panel(qapp)
    panel.builder_pane_list.setCurrentRow(1)
    qapp.processEvents()

    panel.duplicate_pane_btn.click()
    qapp.processEvents()

    panes = fake.last("compose_chart")["params"]["panes"]
    assert [p["pane_id"] for p in panes] == ["main", "rs_monitor", "rs_monitor_2"]
    assert panes[2]["pane_preset_id"] == "rs_monitor"
    assert panel.builder_pane_list.count() == 3


def test_duplicate_pane_refuses_the_price_pane(qapp):
    panel, fake = _panel(qapp)
    panel.builder_pane_list.setCurrentRow(0)
    qapp.processEvents()

    assert panel.duplicate_pane_btn.isEnabled() is False
    panel._on_duplicate_pane_clicked()
    assert "Preispane" in panel.status_label.text()
    assert "compose_chart" not in fake.ops()


def test_save_button_saves_in_place_without_dialog(qapp, monkeypatch):
    panel, fake = _panel(qapp)
    fake.states["win_nvda_1d"]["chart_id"] = "mein_chart"
    fake.states["win_nvda_1d"]["draft"] = False
    panel._refresh_chart_state()
    qapp.processEvents()

    def forbidden(default: str) -> str:
        raise AssertionError("In-Place-Speichern darf keinen Namensdialog oeffnen")

    monkeypatch.setattr(panel, "_ask_chart_name", forbidden)
    panel.save_inplace_btn.click()
    qapp.processEvents()

    assert fake.last("save_chart")["params"] == {
        "window_id": "win_nvda_1d",
        "chart_id": "mein_chart",
    }


def test_save_button_falls_back_to_the_dialog_for_an_unnamed_draft(qapp, monkeypatch):
    panel, fake = _panel(qapp)
    panel._refresh_chart_state()
    qapp.processEvents()
    assert not panel._chart_state.get("chart_id")

    monkeypatch.setattr(panel, "_ask_chart_name", lambda default: "Neu Chart")
    panel.save_inplace_btn.click()
    qapp.processEvents()

    assert fake.last("save_chart")["params"]["chart_id"] == "neu_chart"


def test_compose_warnings_and_skipped_land_in_the_status_line(qapp):
    panel, fake = _panel(qapp)
    original = fake._compose

    def compose_with_warnings(params):
        data = original(params)
        data["warnings"] = [{"code": "unknown_column", "pane_id": "rs", "detail": "ibd_rs"}]
        data["skipped"] = [{"pane_preset_id": "ghost", "reason": "unknown_preset"}]
        return data

    fake._compose = compose_with_warnings
    panel._compose()
    qapp.processEvents()

    status = panel.status_label.text()
    assert "ibd_rs" in status and "ghost" in status


def test_derive_chart_id_slugifies_names():
    assert derive_chart_id("mein_chart") == "mein_chart"
    assert derive_chart_id("Mein Chart") == "mein_chart"
    assert derive_chart_id("RS/Monitor!") == "rsmonitor"
    assert derive_chart_id("   ") == ""


def test_discard_reloads_the_server_state_and_renders_the_saved_chart(qapp):
    panel, fake = _panel(qapp)
    panel._builder_panes = list(panel._builder_panes) + [
        {
            "pane_id": "lokal",
            "pane_preset_id": "rsi_14",
            "title": "RSI 14",
            "role": "any",
            "scale": "linear",
            "weight": 2,
        }
    ]
    panel._rebuild_builder_widgets()
    assert panel.builder_pane_list.count() == 3

    fake.states["win_nvda_1d"]["chart_id"] = "rs_chart"
    panel.discard_chart_btn.click()

    assert [p["pane_id"] for p in panel._builder_panes] == ["main", "rs_monitor"]
    assert panel.builder_pane_list.count() == 2
    assert fake.last("apply_chart")["params"] == {
        "window_id": "win_nvda_1d",
        "chart_id": "rs_chart",
    }
    assert "rs_chart" in panel.status_label.text()


# ── target handling ────────────────────────────────────────────────────────


def test_builder_widgets_are_disabled_without_a_chart_window(qapp):
    panel, fake = _panel(qapp, windows=False)

    assert "kein Chartfenster" in panel.builder_target_label.text()
    for widget in (
        panel.builder_pane_list,
        panel.price_pane_combo,
        panel.add_pane_combo,
        panel.add_pane_btn,
        panel.remove_pane_btn,
        panel.pane_up_btn,
        panel.pane_down_btn,
        panel.scale_toggle_btn,
        panel.volume_check,
        panel.save_chart_btn,
        panel.discard_chart_btn,
    ):
        assert not widget.isEnabled(), widget

    panel.set_chart_windows([dict(WINDOW)])
    qapp.processEvents()
    assert panel.builder_target_label.text() == "Ziel: win_nvda_1d · NVDA"
    assert panel.add_pane_btn.isEnabled()
    assert fake.ops().count("get_chart_state") == 1


def test_target_is_sticky_across_window_refreshes(qapp):
    panel, fake = _panel(qapp, windows=False)
    windows = [
        {"window_id": "win_nvda_1d", "symbol": "NVDA", "color_flag": 0},
        {"window_id": "win_amd_1d", "symbol": "AMD", "color_flag": 1},
    ]
    panel.set_chart_windows(list(windows))
    qapp.processEvents()
    assert panel._builder_target == "win_nvda_1d", "an empty target is filled with the first window"

    panel.set_target_window("win_amd_1d")
    assert panel._builder_target == "win_amd_1d"
    assert panel.builder_target_label.text() == "Ziel: win_amd_1d · AMD"

    panel.set_chart_windows(list(windows))
    qapp.processEvents()
    assert panel._builder_target == "win_amd_1d", "a refresh must not steal the sticky target"

    panel.set_target_window("win_gone_1d")
    assert panel._builder_target == "win_amd_1d", "unknown windows are ignored"

    panel.set_chart_windows([windows[1]])
    qapp.processEvents()
    assert panel._builder_target == "win_amd_1d"

    panel.set_chart_windows([])
    qapp.processEvents()
    assert panel._builder_target == ""
    assert "kein Chartfenster" in panel.builder_target_label.text()
    assert not panel.add_pane_btn.isEnabled()


# ── robustness / app wiring ────────────────────────────────────────────────


def test_compose_error_lands_in_the_status_line_and_keeps_the_builder_alive(qapp):
    panel, fake = _panel(qapp)
    fake.fail_ops.add("compose_chart")
    fake.error_message = "compose kaputt"

    panel.add_pane_combo.setCurrentIndex(panel.add_pane_combo.findData("rsi_14"))
    panel.add_pane_btn.click()

    assert "compose kaputt" in panel.status_label.text()
    assert panel.status_label.property("error") == "true"
    assert panel.add_pane_btn.isEnabled(), "a failed compose must not disable the builder"
    assert panel.builder_pane_list.count() == 3, "the local preview stays until Verwerfen"


class _StubService:
    """Minimal agent-side stub for the app-wiring test."""

    def __init__(self, agent: ChartAgent) -> None:
        self.agent = agent
        self.requests: List[Dict[str, Any]] = []

    def handle(self, request_id: str, op: str, params: Any) -> None:
        self.requests.append({"request_id": request_id, "op": op, "params": params or {}})
        envelope = make_envelope(
            msg_type="control.response",
            payload={
                "request_id": request_id,
                "op": op,
                "ok": True,
                "data": self._data(op, params or {}),
                "error": None,
            },
            kind=MessageKind.EVENT,
        )
        self.agent.transport.send_command(envelope)

    def _data(self, op: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if op == "list_panes":
            return {"panes": [dict(p) for p in PANE_PRESETS]}
        if op == "list_charts":
            return {"charts": []}
        if op == "get_chart_state":
            return {
                "window_id": params.get("window_id"),
                "chart_id": "",
                "draft": True,
                "panes": [dict(p) for p in INITIAL_PANES],
            }
        return {}


def _app(qapp):
    gc.collect()
    settings = QSettings("QJM", "ChartViewer")
    settings.remove("panel")
    settings.sync()

    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    stub = _StubService(agent)
    agent.control_service = stub
    app = ViewerApp(config=ViewerConfig(control_panel_enabled=True), transport=viewer_transport)
    _APPS.append(app)
    agent.start()
    app.start()
    qapp.processEvents()
    return agent, app, stub


def test_app_forwards_window_activation_and_clears_the_target(qapp):
    agent, app, stub = _app(qapp)
    panel = app.control_panel
    assert panel is not None

    agent.open_window("win_amd_1d", "AMD")
    qapp.processEvents()
    assert panel._builder_target == "win_amd_1d"

    agent.open_window("win_nvda_1d", "NVDA")
    qapp.processEvents()

    # ViewerApp connects ChartWindow.window_activated when the window is created
    # (window strand); emitting it must move the sticky target.
    window = app.windows["win_nvda_1d"]
    activated = getattr(window, "window_activated", None)
    if activated is not None:
        activated.emit("win_nvda_1d")
    else:  # pragma: no cover - fallback while the window strand is in flight
        app._on_chart_window_activated("win_nvda_1d")
    assert panel._builder_target == "win_nvda_1d"
    assert panel.builder_target_label.text() == "Ziel: win_nvda_1d · NVDA"

    # A panel refresh (snapshot, window list) must never steal the sticky target.
    app._refresh_panel_chart_windows()
    qapp.processEvents()
    assert panel._builder_target == "win_nvda_1d"

    app.windows["win_nvda_1d"].close()
    qapp.processEvents()
    assert panel._builder_target == "win_amd_1d", "a closed target falls back to an open window"

    app.windows["win_amd_1d"].close()
    qapp.processEvents()
    assert panel._builder_target == ""
    assert "kein Chartfenster" in panel.builder_target_label.text()
