"""Tests for the viewer control panel (ticker search, watchlists, chart presets).

The panel talks to the agent exclusively through `control.request` /
`control.response` envelopes; these tests replace the agent's `ControlService`
with a synchronous stub so the full in-process round trip is exercised without
touching Supabase, the PCA service or the HTTP control API.
"""

import gc
import time
from typing import Any, Dict, List

import pytest
from PySide6.QtCore import QEvent, QPointF, QSettings, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.config import ViewerConfig
from chart_viewer.models.envelope import MessageKind, make_envelope
from chart_viewer.transport.in_process import create_in_process_pair
from chart_viewer.ui import watchlist_dialogs
from chart_viewer.ui.app import ViewerApp


WATCHLISTS = [
    {"name": "all", "editable": False},
    {"name": "10_favorite", "editable": True},
    {"name": "scan_latest", "editable": True},
]
TICKERS = {"10_favorite": ["VICR", "HOOD"], "scan_latest": ["AMD"]}
PRESETS = [
    {"id": "default", "display_name": "Standard", "description": "", "indicator_count": 4},
    {"id": "qmaggi", "display_name": "QMaggi", "description": "", "indicator_count": 10},
]
SYMBOLS = [
    {"ticker": "NVDA", "type": "CS", "has_parquet": True},
    {"ticker": "NVD", "type": "ETF", "has_parquet": False},
]


class StubControlService:
    """Answers control.request envelopes like the real agent-side service."""

    def __init__(self, agent: ChartAgent, *, respond: bool = True) -> None:
        self.agent = agent
        self.respond = respond
        self.requests: List[Dict[str, Any]] = []
        self.applied_presets: List[Dict[str, Any]] = []
        self.watchlists = [dict(w) for w in WATCHLISTS]
        self.tickers = {name: list(items) for name, items in TICKERS.items()}

    def handle(self, request_id: str, op: str, params: Any) -> None:
        self.requests.append({"request_id": request_id, "op": op, "params": params or {}})
        if not self.respond:
            return
        data = self._data(op, params or {})
        envelope = make_envelope(
            msg_type="control.response",
            payload={"request_id": request_id, "op": op, "ok": True, "data": data, "error": None},
            kind=MessageKind.EVENT,
        )
        self.agent.transport.send_command(envelope)

    def _data(self, op: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if op == "search_symbols":
            return {"results": list(SYMBOLS), "match_count": len(SYMBOLS), "source": "db"}
        if op == "list_watchlists":
            return {"watchlists": [dict(w) for w in self.watchlists]}
        if op == "get_watchlist":
            name = params.get("list_name")
            if name == "all":
                return {"list_name": "all", "tickers": [], "count": 41723, "editable": False, "truncated": True}
            tickers = self.tickers.get(name, [])
            return {
                "list_name": name,
                "tickers": [{"ticker": t, "position": i} for i, t in enumerate(tickers)],
                "count": len(tickers),
                "editable": True,
                "truncated": False,
            }
        if op == "mutate_watchlist":
            return self._mutate(params)
        if op == "list_presets":
            return {"presets": list(PRESETS), "count": len(PRESETS)}
        if op == "apply_preset":
            self.applied_presets.append(dict(params))
            return {
                "preset_id": params.get("preset_id"),
                "color_flag": params.get("color_flag"),
                "applied": ["win_amd_1d"],
                "skipped": [],
            }
        return {}

    def _mutate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        action = str(params.get("action") or "")
        name = str(params.get("list_name") or "")
        target = str(params.get("target_list") or "")
        ticker = str(params.get("ticker") or "")

        def count(list_name: str) -> int:
            return len(self.tickers.get(list_name, []))

        if action == "delete":
            self.tickers.pop(name, None)
            self.watchlists = [w for w in self.watchlists if w.get("name") != name]
            return {"status": "deleted", "list_name": name}

        if action == "copy_list":
            tickers = list(self.tickers.get(name, []))
            self.tickers[target] = tickers
            if target not in [w.get("name") for w in self.watchlists]:
                self.watchlists.append({"name": target, "editable": True})
            return {
                "status": "copied",
                "list_name": name,
                "target_list": target,
                "count": len(tickers),
                "target_count": len(tickers),
            }

        if action in ("copy", "move"):
            status = "already_exists" if ticker in self.tickers.get(target, []) else "added"
            if status == "added":
                self.tickers.setdefault(target, []).append(ticker)
            if action == "move" and ticker in self.tickers.get(name, []):
                self.tickers[name].remove(ticker)
            return {
                "status": status if action == "copy" else "moved",
                "list_name": name,
                "ticker": ticker,
                "target_list": target,
                "count": count(name),
                "target_count": count(target),
            }

        if action == "add":
            status = "already_exists" if ticker in self.tickers.get(name, []) else "added"
            if status == "added":
                self.tickers.setdefault(name, []).append(ticker)
        else:
            status = "removed"
            if ticker in self.tickers.get(name, []):
                self.tickers[name].remove(ticker)
        return {
            "status": status,
            "list_name": name,
            "ticker": ticker,
            "count": count(name),
        }

    def ops(self) -> List[str]:
        return [r["op"] for r in self.requests]

    def last(self, op: str) -> Dict[str, Any]:
        matching = [r for r in self.requests if r["op"] == op]
        assert matching, f"no '{op}' request was sent (got {self.ops()})"
        return matching[-1]


# Strong references keep Qt objects alive for the duration of a test; they are
# released deterministically in _panel_cleanup (never from inside processEvents,
# where a cyclic-GC driven widget destruction can crash the Qt event loop).
_APPS: List[Any] = []


@pytest.fixture(autouse=True)
def _panel_cleanup(qapp):
    yield
    while _APPS:
        app = _APPS.pop()
        try:
            app.shutdown()
            for window in list(app.windows.values()):
                window.close()
        except RuntimeError:
            pass  # already deleted by Qt
    gc.collect()


def _setup(qapp, *, respond: bool = True):
    # Collect garbage left behind by earlier test modules *outside* of a Qt event
    # loop iteration (see _APPS comment above).
    gc.collect()

    # Isolate the persistent panel settings (QSettings survives test runs)
    settings = QSettings("QJM", "ChartViewer")
    settings.remove("panel")
    settings.sync()

    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    stub = StubControlService(agent, respond=respond)
    agent.control_service = stub
    app = ViewerApp(config=ViewerConfig(control_panel_enabled=True), transport=viewer_transport)
    _APPS.append(app)
    agent.start()
    app.start()
    qapp.processEvents()
    return agent, app, stub


def _wait_until(qapp, predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _tickers_in_panel(panel) -> List[str]:
    return [panel.watchlist_list.item(i).text() for i in range(panel.watchlist_list.count())]


# ── lifecycle ──────────────────────────────────────────────────────────────


def test_panel_created_on_start_and_outside_window_registry(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    assert panel is not None
    assert panel.isVisible()
    assert app.windows == {}
    assert app.get_all_geometries() == {}

    agent.open_window("win_amd_1d", "AMD")
    qapp.processEvents()
    assert "win_amd_1d" in app.windows
    assert list(app.get_all_geometries().keys()) == ["win_amd_1d"]

    app.windows["win_amd_1d"].close()
    qapp.processEvents()
    assert app.windows == {}
    assert panel.isVisible(), "closing chart windows must keep the control panel open"

def test_panel_cannot_be_closed_and_offers_no_close_button(qapp):
    """The panel is permanent while the viewer runs - no close button, no close."""
    agent, app, stub = _setup(qapp)
    panel = app.control_panel

    assert not bool(panel.windowFlags() & Qt.WindowType.WindowCloseButtonHint), \
        "the title bar must not offer a close button"

    panel.close()
    qapp.processEvents()
    assert panel.isVisible(), "a close request (X / Alt+F4 / .close()) must not take the panel down"

    # Escape used to be the second way to lose the panel.
    panel.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    qapp.processEvents()
    assert panel.isVisible(), "Escape must not hide the control panel"


def test_pin_button_keeps_panel_visible_and_persists_state(qapp):
    """Regression: the pin used to hide the panel.

    setWindowFlags() re-creates the native window and hides the widget; the old
    implementation checked isVisible() only *after* the flag change, saw False and
    never re-showed the panel.
    """
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    assert panel.isVisible()
    assert bool(panel.windowFlags() & Qt.WindowType.WindowStaysOnTopHint), \
        "a fresh panel starts 'always on top' (control_panel_always_on_top)"

    panel._apply_pin(False)
    qapp.processEvents()
    assert panel.isVisible(), "unpinning must not hide the panel"
    assert not bool(panel.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

    settings = QSettings("QJM", "ChartViewer")

    panel.pin_btn.click()
    qapp.processEvents()
    assert panel.isVisible(), "pinning must not hide the panel"
    assert bool(panel.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    assert panel.pin_btn.property("pinned") == "true"
    assert panel.pin_btn.toolTip() == "Immer im Vordergrund: an"
    assert settings.value("panel/pinned_v2", type=bool) is True

    panel.pin_btn.click()
    qapp.processEvents()
    assert panel.isVisible(), "unpinning must not hide the panel"
    assert not bool(panel.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    assert panel.pin_btn.property("pinned") == "false"
    assert panel.pin_btn.toolTip() == "Immer im Vordergrund: aus"
    assert settings.value("panel/pinned_v2", type=bool) is False


def test_panel_reloads_watchlists_and_presets_on_connect(qapp):
    agent, app, stub = _setup(qapp)
    assert "list_watchlists" in stub.ops()
    assert "list_presets" in stub.ops()
    panel = app.control_panel
    assert panel.watchlist_combo.count() == 3
    assert panel.preset_combo.count() == 2


def test_panel_reloads_after_reconnect(qapp):
    agent, app, stub = _setup(qapp)
    before = len(stub.requests)
    app.control_panel.set_connection_state(False)
    app.control_panel.set_connection_state(True)
    assert len(stub.requests) > before
    assert {"list_watchlists", "list_presets"} <= set(stub.ops()[before:])


# ── ticker search ──────────────────────────────────────────────────────────


def test_search_roundtrip_populates_results(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel

    panel.search_edit.setText("NV")
    panel._emit_search()

    assert panel.result_list.count() == 2
    first = panel.result_list.item(0)
    assert first.data(Qt.ItemDataRole.UserRole) == "NVDA"
    assert "●" in first.text()
    assert "○" in panel.result_list.item(1).text()
    assert panel.search_status.text() == "2 Treffer"

    request = stub.last("search_symbols")
    assert request["params"]["query"] == "NV"
    assert request["params"]["limit"] == app.config.control_search_limit


def test_search_debounce_emits_single_request(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel

    panel.search_edit.setText("N")
    panel.search_edit.setText("NV")
    panel.search_edit.setText("NVD")

    assert "search_symbols" not in stub.ops(), "debounce must wait for the pause"
    assert _wait_until(qapp, lambda: "search_symbols" in stub.ops()), "debounced search never fired"
    qapp.processEvents()
    assert stub.ops().count("search_symbols") == 1


def test_stale_search_response_is_ignored(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    panel.search_edit.setText("AMD")
    panel._on_search_response({"query": "NV"}, {"results": SYMBOLS})
    assert panel.result_list.count() == 0


def test_search_without_connection_shows_error(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    panel.set_connection_state(False)
    panel.search_edit.setText("NV")
    panel._emit_search()
    assert "nicht verbunden" in panel.status_label.text()


# ── watchlists ─────────────────────────────────────────────────────────────


def test_watchlist_selection_loads_tickers(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel

    index = panel.watchlist_combo.findData("10_favorite")
    panel._on_watchlist_activated(index)
    assert _tickers_in_panel(panel) == ["VICR", "HOOD"]
    assert panel.watchlist_info.text() == "2 Ticker"
    assert stub.last("get_watchlist")["params"]["list_name"] == "10_favorite"


def test_watchlist_combo_filter_commits_typed_text(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    panel.watchlist_combo.setEditText("scan")
    panel._commit_watchlist_text()
    assert _tickers_in_panel(panel) == ["AMD"]


def test_add_searched_ticker_to_watchlist_and_remove_it(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel

    index = panel.watchlist_combo.findData("10_favorite")
    panel._on_watchlist_activated(index)
    panel.search_edit.setText("NV")
    panel._emit_search()
    panel.result_list.setCurrentRow(0)
    assert panel.add_btn.isEnabled()

    panel.add_btn.click()
    assert stub.last("mutate_watchlist")["params"] == {
        "action": "add",
        "list_name": "10_favorite",
        "ticker": "NVDA",
    }
    assert _tickers_in_panel(panel) == ["VICR", "HOOD", "NVDA"]
    assert "hinzugefügt" in panel.status_label.text()

    # Remove the freshly added row again
    panel.watchlist_list.setCurrentRow(2)
    assert panel.remove_btn.isEnabled()
    panel.remove_btn.click()
    assert stub.last("mutate_watchlist")["params"]["action"] == "remove"
    assert _tickers_in_panel(panel) == ["VICR", "HOOD"]


def test_add_existing_ticker_reports_already_exists(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    panel._on_watchlist_activated(panel.watchlist_combo.findData("10_favorite"))
    panel.search_edit.setText("NV")
    panel._emit_search()
    panel.result_list.setCurrentRow(0)
    stub.tickers["10_favorite"].append("NVDA")  # simulated: already in the list
    panel.add_btn.click()
    assert "bereits" in panel.status_label.text()
    assert _tickers_in_panel(panel) == ["VICR", "HOOD"]


def test_master_list_is_readonly(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel

    panel._on_watchlist_activated(panel.watchlist_combo.findData("all"))
    assert panel.watchlist_list.count() == 0
    assert "schreibgeschützt" in panel.watchlist_info.text()

    panel.search_edit.setText("NV")
    panel._emit_search()
    panel.result_list.setCurrentRow(0)
    assert not panel.add_btn.isEnabled()
    assert not panel.remove_btn.isEnabled()
    panel.add_btn.click()  # must be a no-op
    assert "mutate_watchlist" not in stub.ops()


# ── presets ────────────────────────────────────────────────────────────────


def test_preset_dropdown_and_apply(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel

    assert [panel.preset_combo.itemData(i) for i in range(panel.preset_combo.count())] == [
        "default",
        "qmaggi",
    ]
    assert panel.preset_combo.currentData() == "default"

    panel._on_preset_activated(panel.preset_combo.findData("qmaggi"))
    assert stub.applied_presets == [{"preset_id": "qmaggi", "color_flag": 0}]
    assert panel._preset_id == "qmaggi"
    assert "aktualisiert" in panel.status_label.text()


# ── symbol routing / preset forwarding ─────────────────────────────────────


def test_symbol_activation_routes_to_flag_group_with_preset(qapp):
    agent, app, stub = _setup(qapp)
    recorded: List[tuple] = []
    agent._handle_window_change_symbol = lambda window_id, symbol, preset=None: recorded.append(
        (window_id, symbol, preset)
    )

    agent.open_window("win_amd_1d", "AMD")
    qapp.processEvents()
    app.windows["win_amd_1d"].color_flag = 0

    app.control_panel.symbol_activated.emit("MSFT", 0, "qmaggi")
    assert recorded == [("win_amd_1d", "MSFT", "qmaggi")]


def test_symbol_activation_creates_window_when_flag_group_is_empty(qapp):
    agent, app, stub = _setup(qapp)
    recorded: List[tuple] = []
    agent._handle_window_change_symbol = lambda window_id, symbol, preset=None: recorded.append(
        (window_id, symbol, preset)
    )

    app.control_panel.symbol_activated.emit("MSFT", 3, "qmaggi")
    qapp.processEvents()

    assert "win_msft_1d" in app.windows
    assert app.windows["win_msft_1d"].color_flag == 3
    assert recorded == [("win_msft_1d", "MSFT", "qmaggi")]


def test_watchlist_single_click_routes_symbol(qapp):
    """Marking a row is enough - the double click requirement is gone."""
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    activated: List[tuple] = []
    panel.symbol_activated.connect(lambda symbol, flag, preset: activated.append((symbol, flag, preset)))

    panel._on_watchlist_activated(panel.watchlist_combo.findData("10_favorite"))
    assert activated == [], "loading a list must not activate a ticker by itself"

    panel.watchlist_list.setCurrentRow(1)
    qapp.processEvents()
    assert activated == [("HOOD", 0, "")]
    assert "win_hood_1d" in app.windows
    assert app.control_panel._preset_id == "", "no preset override until the user picks one"


def test_programmatic_list_rebuild_does_not_switch_the_chart(qapp):
    """Clearing/removing rows moves the current row - that must not route."""
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    panel._on_watchlist_activated(panel.watchlist_combo.findData("10_favorite"))
    activated: List[str] = []
    panel.symbol_activated.connect(lambda symbol, flag, preset: activated.append(symbol))

    panel.watchlist_list.setCurrentRow(0)
    assert activated == ["VICR"]

    activated.clear()
    panel.remove_btn.click()
    qapp.processEvents()
    assert _tickers_in_panel(panel) == ["HOOD"]
    assert activated == [], "removing the selected ticker must not activate the next row"

    activated.clear()
    panel._on_watchlist_activated(panel.watchlist_combo.findData("scan_latest"))
    qapp.processEvents()
    assert activated == []


def test_watchlist_combo_opens_on_click_anywhere(qapp):
    """Clicking the text field must open the dropdown, not only the arrow."""
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    opened: List[bool] = []
    panel.watchlist_combo.showPopup = lambda: opened.append(True)  # type: ignore[method-assign]

    line_edit = panel.watchlist_combo.lineEdit()
    assert line_edit is not None
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(5, 5),
        QPointF(5, 5),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    handled = panel.eventFilter(line_edit, event)
    assert opened == [True]
    assert handled is False, "the click must still reach the line edit"


def test_copy_and_move_buttons_transfer_the_selected_ticker(qapp, monkeypatch):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    panel._on_watchlist_activated(panel.watchlist_combo.findData("10_favorite"))
    panel.watchlist_list.setCurrentRow(1)  # HOOD

    picks: List[str] = []
    monkeypatch.setattr(watchlist_dialogs, "ask_watchlist", lambda *a, **k: picks.pop(0) if picks else None)

    picks.append("scan_latest")
    panel.copy_btn.click()
    request = stub.last("mutate_watchlist")
    assert request["params"] == {
        "action": "copy",
        "list_name": "10_favorite",
        "ticker": "HOOD",
        "target_list": "scan_latest",
    }
    assert "kopiert" in panel.status_label.text()
    assert _tickers_in_panel(panel) == ["VICR", "HOOD"], "copy leaves the source untouched"
    assert stub.tickers["scan_latest"] == ["AMD", "HOOD"]

    picks.append("scan_latest")
    panel.move_btn.click()
    assert stub.last("mutate_watchlist")["params"]["action"] == "move"
    assert "verschoben" in panel.status_label.text()
    assert _tickers_in_panel(panel) == ["VICR"]


def test_new_watchlist_as_copy_of_an_existing_one(qapp, monkeypatch):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    monkeypatch.setattr(
        watchlist_dialogs, "ask_new_watchlist", lambda *a, **k: ("30_copy", "10_favorite")
    )

    panel.new_watchlist_btn.click()
    assert stub.last("mutate_watchlist")["params"] == {
        "action": "copy_list",
        "list_name": "10_favorite",
        "target_list": "30_copy",
    }
    qapp.processEvents()
    assert panel._current_watchlist == "30_copy"
    assert _tickers_in_panel(panel) == ["VICR", "HOOD"]
    assert "angelegt" in panel.status_label.text()


def test_new_empty_watchlist_is_kept_until_the_first_ticker(qapp, monkeypatch):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    monkeypatch.setattr(watchlist_dialogs, "ask_new_watchlist", lambda *a, **k: ("40_leer", ""))

    panel.new_watchlist_btn.click()
    assert "mutate_watchlist" not in stub.ops(), "an empty list needs no backend call"
    assert panel._pending_watchlists == ["40_leer"]
    assert panel._current_watchlist == "40_leer"
    assert _tickers_in_panel(panel) == []
    assert panel.watchlist_info.text() == "0 Ticker · neu"

    # A reload keeps the local-only list visible and selected.
    panel.refresh_watchlists()
    qapp.processEvents()
    assert "40_leer" in [panel.watchlist_combo.itemData(i) for i in range(panel.watchlist_combo.count())]
    assert panel._current_watchlist == "40_leer"

    # The first ticker materializes it in the database.
    panel.search_edit.setText("NV")
    panel._emit_search()
    panel.result_list.setCurrentRow(0)
    panel.add_btn.click()
    qapp.processEvents()
    assert stub.tickers["40_leer"] == ["NVDA"]
    assert panel._pending_watchlists == []
    assert _tickers_in_panel(panel) == ["NVDA"]


def test_delete_watchlist_button_confirms_and_reselects(qapp, monkeypatch):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    panel._on_watchlist_activated(panel.watchlist_combo.findData("scan_latest"))
    picks: List[str] = ["scan_latest"]
    monkeypatch.setattr(watchlist_dialogs, "ask_watchlist", lambda *a, **k: picks.pop(0) if picks else None)
    monkeypatch.setattr(watchlist_dialogs, "confirm_delete", lambda *a, **k: True)

    panel.delete_watchlist_btn.click()
    assert stub.last("mutate_watchlist")["params"] == {"action": "delete", "list_name": "scan_latest"}
    qapp.processEvents()

    names = [panel.watchlist_combo.itemData(i) for i in range(panel.watchlist_combo.count())]
    assert "scan_latest" not in names
    assert panel._current_watchlist == "all", "the deleted list was current -> fall back"
    assert _tickers_in_panel(panel) == []
    assert "schreibgeschützt" in panel.watchlist_info.text()


def test_delete_watchlist_aborts_without_confirmation(qapp, monkeypatch):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel
    monkeypatch.setattr(watchlist_dialogs, "ask_watchlist", lambda *a, **k: "scan_latest")
    monkeypatch.setattr(watchlist_dialogs, "confirm_delete", lambda *a, **k: False)

    panel.delete_watchlist_btn.click()
    assert "mutate_watchlist" not in stub.ops()


def test_panel_shortcut_brings_panel_to_front_without_hiding_it(qapp):
    """Ctrl+Shift+P now only raises the panel - hiding it was a way to lose it."""
    agent, app, stub = _setup(qapp)
    agent.open_window("win_amd_1d", "AMD")
    qapp.processEvents()
    panel = app.control_panel
    window = app.windows["win_amd_1d"]

    raised = []
    panel.raise_ = lambda: raised.append(True)  # type: ignore[method-assign]

    for _ in range(2):
        window.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_P,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        ))
        qapp.processEvents()
        assert panel.isVisible(), "the panel shortcut must never hide the panel"

    assert raised, "the shortcut must bring the panel to the front"


# ── robustness ─────────────────────────────────────────────────────────────


def test_unanswered_request_times_out(qapp):
    agent, app, stub = _setup(qapp, respond=False)
    panel = app.control_panel
    request_id = panel._request("search_symbols", {"query": "NV"}, {"query": "NV"})
    panel._pending[request_id]["sent_at"] -= 60
    panel._check_timeouts()
    assert "keine Antwort" in panel.status_label.text()
    assert request_id not in panel._pending


def test_backend_error_is_shown_and_buttons_reset(qapp):
    agent, app, stub = _setup(qapp)
    panel = app.control_panel

    def failing_handle(request_id: str, op: str, params: Any) -> None:
        stub.requests.append({"request_id": request_id, "op": op, "params": params or {}})
        stub.agent.transport.send_command(
            make_envelope(
                msg_type="control.response",
                payload={
                    "request_id": request_id,
                    "op": op,
                    "ok": False,
                    "data": None,
                    "error": {"code": "protected_list", "message": "geschützt"},
                },
                kind=MessageKind.EVENT,
            )
        )

    stub.handle = failing_handle
    panel._request("mutate_watchlist", {"action": "add"}, {"kind": "add"})
    assert "geschützt" in panel.status_label.text()
    assert panel._pending == {}
