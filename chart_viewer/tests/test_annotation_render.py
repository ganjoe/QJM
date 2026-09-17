"""ADD_ANNOTATION must actually draw: end-to-end render regression tests.

Guards the bug where annotation.set was accepted by the server and cached in
StateManager, but no UI code ever painted it — so the command reported success
while the chart stayed visually unchanged.
"""

import base64

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QImage

from chart_viewer.config import ViewerConfig
from chart_viewer.ui.app import ViewerApp
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.transport.in_process import create_in_process_pair
from chart_viewer.models.entities import Anchor, Annotation
from chart_viewer.models.envelope import make_envelope, MessageKind

# Colors that cannot collide with the theme palette (up #3877FF, down #E040FB,
# background #131722, grid #2A2E39, text #D1D4DC).
HLINE_COLOR = "#FF0000"
MARKER_COLOR = "#00FF00"

WIN_ID = "win_anntest_1d"


def _bars(count: int = 40) -> list[dict]:
    bars = []
    for i in range(count):
        o = 100.0 + i
        bars.append({
            "t_open": 1_700_000_000 + i * 86400,
            "t_close": 1_700_000_000 + (i + 1) * 86400,
            "open": o,
            "high": o + 2.0,
            "low": o - 2.0,
            "close": o + 1.0,
            "volume": 1000.0,
        })
    return bars


def _start_viewer(qapp):
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)
    agent.start()
    app.start()
    agent.open_window(window_id=WIN_ID, symbol="TEST")
    agent.send_snapshot(WIN_ID, {"symbol": "TEST", "bars": _bars()})
    qapp.processEvents()
    return agent, app


def _main_pane(app):
    return app.windows[WIN_ID].canvas._panes["main"]


def _visible_price_fraction(app, fraction: float) -> float:
    """A price guaranteed to sit inside the pane's current visible Y-range."""
    pane = _main_pane(app)
    lo = float(pane.y_trans.p_min)
    hi = float(pane.y_trans.p_max)
    return lo + (hi - lo) * fraction


def _screenshot(agent) -> bytes:
    res = agent.request_screenshots(window_id=WIN_ID, timeout_s=5.0)
    assert res and res.get("screenshots"), "no screenshot returned"
    return base64.b64decode(res["screenshots"][0]["image_base64"])


def _count_color(png_bytes: bytes, hex_color: str, tolerance: int = 24) -> int:
    img = QImage()
    assert img.loadFromData(png_bytes, "PNG"), "screenshot is not a decodable PNG"
    target = QColor(hex_color)
    hits = 0
    for y in range(img.height()):
        for x in range(img.width()):
            px = img.pixelColor(x, y)
            if (abs(px.red() - target.red()) <= tolerance
                    and abs(px.green() - target.green()) <= tolerance
                    and abs(px.blue() - target.blue()) <= tolerance):
                hits += 1
    return hits


def _send_annotation(agent, qapp, annotation: dict) -> None:
    agent.send_command(make_envelope(
        msg_type="annotation.set",
        payload={"annotation": annotation},
        kind=MessageKind.COMMAND,
        window_id=WIN_ID,
    ))
    qapp.processEvents()


def test_hline_annotation_is_actually_rendered(qapp):
    agent, app = _start_viewer(qapp)

    # Baseline: nothing colored like the annotation is on screen yet
    assert _count_color(_screenshot(agent), HLINE_COLOR) == 0

    _send_annotation(agent, qapp, {
        "id": "sup_1",
        "type": "hline",
        "anchors": [{"price": _visible_price_fraction(app, 0.5), "mode": "data"}],
        "style": {"color": HLINE_COLOR, "width": 2, "text": "SUPPORT"},
        "persistent": True,
    })

    assert "sup_1" in app.state_manager.get_window_data(WIN_ID).annotations
    assert _count_color(_screenshot(agent), HLINE_COLOR) > 0, \
        "hline was accepted but never painted"


def test_trade_marker_is_actually_rendered(qapp):
    agent, app = _start_viewer(qapp)

    _send_annotation(agent, qapp, {
        "id": "buy_1",
        "type": "trade_marker",
        "anchors": [{"price": _visible_price_fraction(app, 0.35), "mode": "data"}],
        "style": {"color": MARKER_COLOR, "width": 2, "action": "BUY", "text": "BUY"},
        "persistent": True,
    })

    assert _count_color(_screenshot(agent), MARKER_COLOR) > 0, \
        "trade marker was accepted but never painted"


def test_annotation_remove_stops_rendering(qapp):
    agent, app = _start_viewer(qapp)

    _send_annotation(agent, qapp, {
        "id": "sup_2",
        "type": "hline",
        "anchors": [{"price": _visible_price_fraction(app, 0.5), "mode": "data"}],
        "style": {"color": HLINE_COLOR, "width": 2},
        "persistent": True,
    })
    assert _count_color(_screenshot(agent), HLINE_COLOR) > 0

    agent.send_command(make_envelope(
        msg_type="annotation.remove",
        payload={"id": "sup_2"},
        kind=MessageKind.COMMAND,
        window_id=WIN_ID,
    ))
    qapp.processEvents()

    assert _count_color(_screenshot(agent), HLINE_COLOR) == 0, \
        "removed annotation is still painted"


def test_annotation_from_snapshot_is_rendered(qapp):
    """Annotations shipped with snapshot.full (viewer reconnect) must paint too."""
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)
    agent.start()
    app.start()
    agent.open_window(window_id=WIN_ID, symbol="TEST")

    agent.send_snapshot(WIN_ID, {"symbol": "TEST", "bars": _bars()})
    qapp.processEvents()
    price = _visible_price_fraction(app, 0.5)

    agent.send_snapshot(WIN_ID, {
        "symbol": "TEST",
        "bars": _bars(),
        "annotations": [{
            "id": "snap_1",
            "type": "hline",
            "anchors": [{"price": price, "mode": "data"}],
            "style": {"color": HLINE_COLOR, "width": 2},
            "persistent": True,
        }],
    })
    qapp.processEvents()

    assert _count_color(_screenshot(agent), HLINE_COLOR) > 0, \
        "annotation delivered via snapshot was never painted"


class _PaintCounter(QObject):
    """Counts Paint events delivered to a widget."""

    def __init__(self):
        super().__init__()
        self.count = 0

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Paint:
            self.count += 1
        return False


def test_set_annotations_repaints_synchronously(qapp):
    """Agent-set annotations must reach the screen without further interaction.

    This guards the failure mode where grab()-based screenshots looked correct
    while the physical screen stayed stale: set_annotations used to only call
    update(), so the paint sat in the event queue until the next natural repaint
    (e.g. the user moving the mouse over the chart).
    """
    agent, app = _start_viewer(qapp)
    pane = _main_pane(app)

    counter = _PaintCounter()
    pane.installEventFilter(counter)
    try:
        before = counter.count
        pane.set_annotations({
            "sync_1": Annotation(
                id="sync_1",
                type="hline",
                anchors=[Anchor(price=_visible_price_fraction(app, 0.5), mode="data")],
                style={"color": HLINE_COLOR, "width": 2},
            )
        })
        # Deliberately NO qapp.processEvents() here: the repaint must already
        # have happened synchronously.
        assert counter.count > before, \
            "set_annotations did not repaint synchronously (deferred to event loop)"
    finally:
        pane.removeEventFilter(counter)
