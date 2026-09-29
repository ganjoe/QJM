from chart_viewer.config import ViewerConfig
"""Tests for Window Lifecycle and Stateless Restores (Section 8, 11 & Criteria 8, 9, 10)."""

from chart_viewer.ui.app import ViewerApp
from chart_viewer.ui.window import ChartWindow
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.transport.in_process import create_in_process_pair
from chart_viewer.models.envelope import make_envelope, MessageKind


class _RecordingTransport:
    """Minimaler Viewer-Transport ohne Agent dahinter (nur Sends mitschreiben)."""

    def __init__(self) -> None:
        self.sent = []
        self.handlers = []
        self._connected = True

    def on_event(self, handler) -> None:
        self.handlers.append(handler)

    def on_connect(self, handler) -> None:
        pass

    def on_disconnect(self, handler) -> None:
        pass

    def send_command(self, envelope) -> None:
        self.sent.append(envelope)

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


def _symbol_change_requests(transport) -> list:
    return [e for e in transport.sent if e.type == "window.change_symbol"]


def _snapshot_payload(symbol: str = "AAPL") -> dict:
    return {
        "symbol": symbol,
        "timeframe": {"unit": "D", "multiplier": 1},
        "bars": [
            {
                "t_open": 1700000000,
                "t_close": 1700086400,
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 100.0,
            }
        ],
        "overlays": [],
        "annotations": [],
    }


def test_empty_window_re_requests_the_same_symbol(qapp):
    """Ein leeres Fenster schluckt den zweiten Klick auf denselben Ticker nicht."""
    win = ChartWindow(window_id="win_aapl_1d", config=ViewerConfig())
    requests = []
    win.symbol_change_requested.connect(
        lambda window_id, symbol, preset: requests.append((window_id, symbol, preset))
    )

    win.symbol = "AAPL"  # Der Klick hat den Ticker bereits gesetzt ...
    win.has_data = False  # ... aber es kamen keine Daten an
    win.request_symbol_change("AAPL")
    assert requests == [("win_aapl_1d", "AAPL", None)]

    # Mit Daten bleibt derselbe Ticker ein No-Op
    win.has_data = True
    win.request_symbol_change("AAPL")
    assert requests == [("win_aapl_1d", "AAPL", None)]


def test_empty_window_requests_the_symbol_again(qapp):
    """Bleibt ein Fenster leer (Antwort verloren), wird der Wunsch wiederholt."""
    transport = _RecordingTransport()
    app = ViewerApp(
        config=ViewerConfig(
            control_panel_enabled=False,
            render_request_timeout_ms=6000,
            render_request_max_attempts=2,
        ),
        transport=transport,
    )

    app._handle_window_open({"window_id": "win_aapl_1d", "symbol": "AAPL"}, b"")
    app._on_symbol_change_requested("win_aapl_1d", "AAPL")
    assert len(_symbol_change_requests(transport)) == 1

    app._pending_renders["win_aapl_1d"]["sent_at"] -= 30.0
    app._check_pending_renders()
    assert len(_symbol_change_requests(transport)) == 2

    # Letzter Versuch verbraucht: kein Endlos-Retry
    app._pending_renders["win_aapl_1d"]["sent_at"] -= 30.0
    app._check_pending_renders()
    assert len(_symbol_change_requests(transport)) == 2
    assert app._pending_renders == {}


def test_window_with_data_is_not_requested_again(qapp):
    """Ein gezeichneter Chart wird nie doppelt angefordert."""
    transport = _RecordingTransport()
    app = ViewerApp(
        config=ViewerConfig(control_panel_enabled=False, render_request_timeout_ms=6000),
        transport=transport,
    )

    app._handle_window_open({"window_id": "win_aapl_1d", "symbol": "AAPL"}, b"")
    app._on_symbol_change_requested("win_aapl_1d", "AAPL")
    app.state_manager.apply_snapshot("win_aapl_1d", _snapshot_payload())

    app._pending_renders["win_aapl_1d"]["sent_at"] -= 30.0
    app._check_pending_renders()

    assert len(_symbol_change_requests(transport)) == 1
    assert app._pending_renders == {}


def test_reconnect_resends_a_pending_render(qapp):
    """Ein Reconnect fordert leer gebliebene Fenster sofort nach."""
    transport = _RecordingTransport()
    app = ViewerApp(
        config=ViewerConfig(control_panel_enabled=False), transport=transport
    )

    app._handle_window_open({"window_id": "win_aapl_1d", "symbol": "AAPL"}, b"")
    app._on_symbol_change_requested("win_aapl_1d", "AAPL")
    assert len(_symbol_change_requests(transport)) == 1

    app._on_connection_state_changed(True)
    assert len(_symbol_change_requests(transport)) == 2

    # Mit Daten wird beim Reconnect nichts erneut angefordert
    app.state_manager.apply_snapshot("win_aapl_1d", _snapshot_payload())
    app._on_connection_state_changed(True)
    assert len(_symbol_change_requests(transport)) == 2


def test_watchdog_consumes_no_attempt_while_disconnected(qapp):
    """Offline wird nichts gesendet und kein Versuch verbraucht."""
    transport = _RecordingTransport()
    app = ViewerApp(
        config=ViewerConfig(control_panel_enabled=False), transport=transport
    )

    app._handle_window_open({"window_id": "win_aapl_1d", "symbol": "AAPL"}, b"")
    app._on_symbol_change_requested("win_aapl_1d", "AAPL")
    assert len(_symbol_change_requests(transport)) == 1

    transport.disconnect()
    app._pending_renders["win_aapl_1d"]["sent_at"] -= 60.0
    app._check_pending_renders()

    assert len(_symbol_change_requests(transport)) == 1
    assert app._pending_renders["win_aapl_1d"]["attempts"] == 1


def test_criterion_8_viewer_start_with_zero_windows(qapp):
    """Criterion 8: Viewer starts without any window.open command -> runs stable with 0 windows."""
    viewer_transport, agent_transport = create_in_process_pair()
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)

    app.start()

    # Viewer has 0 open windows
    assert len(app.windows) == 0


def test_criterion_9_user_closes_window_fire_and_forget(qapp):
    """Criterion 9: User closes a window -> window.closed arrives at agent, viewer does not wait for ACK."""
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)

    agent.start()
    app.start()

    # Agent opens a window
    agent.open_window(window_id="daily-win", symbol="AAPL")
    qapp.processEvents()

    assert "daily-win" in app.windows
    chart_win = app.windows["daily-win"]

    # Clear agent received events
    agent.received_events.clear()

    # User closes the window
    chart_win.close()
    qapp.processEvents()

    # Verify window is removed from Viewer
    assert "daily-win" not in app.windows

    # Verify window.closed event arrived at Agent
    closed_events = [e for e in agent.received_events if e.type == "window.closed"]
    assert len(closed_events) == 1
    assert closed_events[0].window_id == "daily-win"
    # Fire and forget: kind is EVENT (0), not waiting for ack
    assert closed_events[0].kind == MessageKind.EVENT


def _record_envelopes(agent_transport) -> list:
    """Zeichnet die Nachrichten des Agenten an den Client auf."""
    sent: list = []
    original = agent_transport.send_command

    def record(envelope):
        sent.append(envelope)
        return original(envelope)

    agent_transport.send_command = record
    return sent


def test_rerender_without_geometry_keeps_the_window_place(qapp):
    """Ein Re-Render ohne Geometrie darf das Fenster nicht verschieben.

    "Gespeichertes Chart anwenden" (apply_preset) und COMPOSE schicken nur Symbol,
    Timeframe und Chart mit. Die Geometrie gehoert dem Client; der Agent merkt sich
    den zuletzt gemeldeten Stand und schickt ihn hoechstens unveraendert zurueck.
    """
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)
    agent.start()
    app.start()
    qapp.processEvents()

    agent.open_window(window_id="win_a", symbol="NVDA")
    qapp.processEvents()
    win = app.windows["win_a"]
    win.move(321, 123)
    win.resize(880, 640)
    qapp.processEvents()

    assert agent.layout_ledger["win_a"]["position"] == {"x": 321, "y": 123}
    assert agent.layout_ledger["win_a"]["size"] == {"width": 880, "height": 640}

    sent = _record_envelopes(agent_transport)

    # Re-Render wie apply_preset: neues Symbol, keine Geometrie im Kommando.
    agent.open_window(window_id="win_a", symbol="NVDA")
    qapp.processEvents()

    opens = [e for e in sent if e.type == "window.open"]
    assert opens, "ein Re-Render schickt window.open"
    assert "position" not in opens[-1].payload and "size" not in opens[-1].payload, (
        "ein Re-Render darf keine Geometrie mitschicken - der Client besitzt sie"
    )
    assert agent.layout_ledger["win_a"]["position"] == {"x": 321, "y": 123}
    assert agent.layout_ledger["win_a"]["size"] == {"width": 880, "height": 640}
    assert (win.x(), win.y()) == (321, 123), "Fensterposition darf nicht springen"
    assert (win.width(), win.height()) == (880, 640), "Fenstergroesse darf nicht springen"


def test_display_stock_without_geometry_keeps_the_window_place(qapp, monkeypatch):
    """Derselbe Weg wie der Anwenden-Knopf: DISPLAY_STOCK ohne position/size."""
    from chart_viewer import orchestrator
    from chart_viewer.agent import command_api

    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)
    agent.start()
    app.start()
    qapp.processEvents()

    agent.open_window(window_id="win_a", symbol="NVDA")
    qapp.processEvents()
    win = app.windows["win_a"]
    win.move(250, 140)
    win.resize(900, 700)
    qapp.processEvents()

    def fake_build_display_stock(**kwargs):
        return {
            "symbol": kwargs["symbol"],
            "timeframe": {"unit": "D", "multiplier": 1},
            "window_id": kwargs["window_id"],
            "bars": [],
            "overlays": [],
            "panes": [],
            "chart": {},
            "pane_scales": {},
            "position": kwargs.get("position"),
            "size": kwargs.get("size"),
        }

    monkeypatch.setattr(orchestrator, "build_display_stock", fake_build_display_stock)

    result = command_api.handle_command(
        {"action": "DISPLAY_STOCK", "window_id": "win_a", "symbol": "HOOD", "preset": "default"},
        agent,
        agent_transport,
    )
    qapp.processEvents()

    assert result["status"] == "ok"
    assert agent.layout_ledger["win_a"]["position"] == {"x": 250, "y": 140}
    assert agent.layout_ledger["win_a"]["size"] == {"width": 900, "height": 700}
    assert (win.x(), win.y()) == (250, 140)
    assert (win.width(), win.height()) == (900, 700)


def test_window_open_with_a_new_geometry_places_the_window(qapp):
    """Eine ausdrueckliche Platzierung (Setup laden) wirkt weiter."""
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)
    agent.start()
    app.start()
    qapp.processEvents()

    agent.open_window(window_id="win_a", symbol="NVDA")
    qapp.processEvents()
    win = app.windows["win_a"]

    sent = _record_envelopes(agent_transport)
    agent.open_window(
        window_id="win_a",
        symbol="NVDA",
        position={"x": 410, "y": 220},
        size={"width": 820, "height": 610},
    )
    qapp.processEvents()

    opens = [e for e in sent if e.type == "window.open"]
    assert opens[-1].payload["position"] == {"x": 410, "y": 220}
    assert opens[-1].payload["size"] == {"width": 820, "height": 610}
    assert (win.x(), win.y()) == (410, 220)
    assert (win.width(), win.height()) == (820, 610)


def test_display_stock_through_the_real_orchestrator_keeps_the_window_place(qapp, monkeypatch):
    """Der Orchestrator erfindet keine Geometrie (100,100 / 1100x750).

    Genau dieser Default zog jedes angewendete Chart auf dieselbe Stelle - der
    Test laeuft deshalb durch den echten build_display_stock, nur die Datenabrufe
    sind ersetzt.
    """
    from chart_viewer import orchestrator
    from chart_viewer.agent import command_api

    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)
    agent.start()
    app.start()
    qapp.processEvents()

    agent.open_window(window_id="win_a", symbol="NVDA")
    qapp.processEvents()
    win = app.windows["win_a"]
    win.move(250, 140)
    win.resize(900, 700)
    qapp.processEvents()

    monkeypatch.setattr(
        orchestrator,
        "fetch_chart_data",
        lambda symbol, timeframe="1D", limit=2000: {
            "status": "ok",
            "columns": ["timestamp", "open", "high", "low", "close", "volume"],
            "data": [[1_700_000_000, 100.0, 101.0, 99.0, 100.5, 1_000_000.0]],
            "features_stale": False,
        },
    )
    monkeypatch.setattr(orchestrator, "_pca_get", lambda path: {"features": []})
    monkeypatch.setattr(orchestrator, "_pca_get_soft", lambda path: None)

    result = command_api.handle_command(
        {"action": "DISPLAY_STOCK", "window_id": "win_a", "symbol": "HOOD", "preset": "default"},
        agent,
        agent_transport,
    )
    qapp.processEvents()

    assert result["status"] == "ok"
    assert (win.x(), win.y()) == (250, 140), "Anwenden darf das Fenster nicht verschieben"
    assert (win.width(), win.height()) == (900, 700), "Anwenden darf die Groesse nicht aendern"
    assert agent.layout_ledger["win_a"]["position"] == {"x": 250, "y": 140}
    assert agent.layout_ledger["win_a"]["size"] == {"width": 900, "height": 700}


def test_window_open_reports_the_client_geometry(qapp):
    """Der Client meldet seine Geometrie - der Ledger folgt ihm, nicht umgekehrt."""
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)
    agent.start()
    app.start()
    qapp.processEvents()

    agent.open_window(window_id="win_a", symbol="NVDA")
    qapp.processEvents()
    win = app.windows["win_a"]

    # Ein neu geoeffnetes Fenster hat keine Platzierung vom Agenten bekommen:
    # sein Ledger-Stand kommt aus der Meldung des Clients.
    assert agent.layout_ledger["win_a"]["size"] == {"width": win.width(), "height": win.height()}
    assert agent.layout_ledger["win_a"]["position"] == {"x": win.x(), "y": win.y()}

    win.resize(760, 540)
    qapp.processEvents()
    assert agent.layout_ledger["win_a"]["size"] == {"width": 760, "height": 540}


def test_criterion_10_viewer_restart_layout_restore(qapp):
    """Criterion 10: Viewer restart without agent restart -> layout.restore restores windows and annotations."""
    viewer_transport1, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    agent.start()

    # 1. First viewer session
    app1 = ViewerApp(config=ViewerConfig(), transport=viewer_transport1)
    app1.start()

    agent.open_window(window_id="win-crypto", symbol="BTCUSDT")
    agent.send_snapshot("win-crypto", {
        "symbol": "BTCUSDT",
        "bars": [{"t_open": 100, "t_close": 200, "open": 50.0, "high": 55.0, "low": 48.0, "close": 52.0}],
        "annotations": [{"id": "support-1", "type": "hline", "anchors": [{"price": 49.0}], "style": {}}],
    })
    qapp.processEvents()

    assert "win-crypto" in app1.windows
    assert len(app1.state_manager.get_window_data("win-crypto").annotations) == 1

    # 2. Simulate Viewer termination / disconnect
    viewer_transport1.disconnect()

    # 3. Viewer restarts with a fresh process / new ViewerApp instance (stateless)
    viewer_transport2, agent_transport2 = create_in_process_pair()
    # Re-bind agent to new connection
    agent.transport = agent_transport2
    agent_transport2.on_event(agent._on_envelope)
    agent_transport2.connect()

    app2 = ViewerApp(config=ViewerConfig(), transport=viewer_transport2)
    # App2 starts empty (0 windows)
    assert len(app2.windows) == 0

    # Start App2 -> sends viewer.ready -> Agent responds with layout.restore and snapshot
    app2.start()
    qapp.processEvents()

    # Verify windows and annotations are fully restored in new Viewer session!
    assert "win-crypto" in app2.windows
    restored_data = app2.state_manager.get_window_data("win-crypto")
    assert restored_data is not None
    assert restored_data.symbol == "BTCUSDT"
    assert len(restored_data.bars) == 1
    assert "support-1" in restored_data.annotations
    assert restored_data.annotations["support-1"].anchors[0].price == 49.0
