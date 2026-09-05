from chart_viewer.config import ViewerConfig
"""Tests for Window Lifecycle and Stateless Restores (Section 8, 11 & Criteria 8, 9, 10)."""

from chart_viewer.ui.app import ViewerApp
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.transport.in_process import create_in_process_pair
from chart_viewer.models.envelope import make_envelope, MessageKind


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
