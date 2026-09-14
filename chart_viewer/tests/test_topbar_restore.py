"""Regression: the topbar info row must survive a viewer reconnect / layout.restore.

The topbar used to be delivered only as a live "topbar.set_block" message, which the
agent never persisted. On a viewer restart the windows are rebuilt from layout.restore
plus the stored snapshots, so the info row vanished. The snapshot now carries the topbar
blocks and apply_snapshot re-applies them via bind_data.
"""

import gc

from PySide6.QtCore import QCoreApplication, QEvent

from chart_viewer.config import ViewerConfig
from chart_viewer.ui.app import ViewerApp
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.transport.in_process import create_in_process_pair


def _bars():
    return [{"t_open": 1700000000, "t_close": 1700086400, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}]


TOPBAR = {"block_id": "info_block", "position": {"row": 0, "col": 0}, "content": "AAPL | Last: 1.50 | Bars: 1"}


def _snapshot():
    return {
        "symbol": "AAPL",
        "timeframe": {"unit": "D", "multiplier": 1},
        "sync_group_id": "stocks",
        "bars": _bars(),
        "topbar_blocks": [TOPBAR],
    }


def _teardown(qapp, *apps):
    """Destroy viewer Qt objects completely so nothing dangles into later tests."""
    for app in apps:
        if app is None:
            continue
        try:
            app.render_timer.stop()
        except Exception:
            pass
        for win in list(app.windows.values()):
            win.close()
            win.setParent(None)
            win.deleteLater()
        app.windows.clear()
    qapp.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
    qapp.processEvents()


def test_topbar_survives_viewer_restore(qapp):
    vt1, at = create_in_process_pair()
    agent = ChartAgent(transport=at)
    agent.start()
    app1 = ViewerApp(config=ViewerConfig(), transport=vt1)
    app1.start()
    app2 = None
    try:
        agent.open_window(window_id="w", symbol="AAPL", timeframe_unit="D", multiplier=1, sync_group_id="stocks")
        agent.send_snapshot("w", _snapshot())
        qapp.processEvents()

        # applied immediately from the snapshot / live message
        assert "info_block" in app1.windows["w"].topbar._blocks
        assert app1.windows["w"].topbar._blocks["info_block"].text() == "AAPL | Last: 1.50 | Bars: 1"
        # the agent persists exactly this snapshot for restore_layout
        assert agent.series_data["w"].get("topbar_blocks") == [TOPBAR]

        # Viewer process dies WITHOUT sending window.closed (crash / restart).
        vt1.disconnect()

        # Same agent, fresh viewer process.
        vt2, at2 = create_in_process_pair()
        agent.transport = at2
        at2.on_event(agent._on_envelope)
        at2.connect()
        app2 = ViewerApp(config=ViewerConfig(), transport=vt2)
        app2.start()

        # What ChartAgent._handle_viewer_ready() triggers on every viewer.ready.
        agent.restore_layout()
        qapp.processEvents()

        assert "w" in app2.windows
        assert len(app2.state_manager.get_window_data("w").bars) == 1
        assert "info_block" in app2.windows["w"].topbar._blocks, "topbar must survive restore_layout"
        assert app2.windows["w"].topbar._blocks["info_block"].text() == "AAPL | Last: 1.50 | Bars: 1"
    finally:
        _teardown(qapp, app1, app2)
