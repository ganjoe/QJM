from chart_viewer.config import ViewerConfig
"""Tests for EventHub, Backpressure, and StateManager (Section 3, 4, 11 & Criterion 2)."""

from chart_viewer.core.event_hub import EventHub
from chart_viewer.core.backpressure import TickCoalescer
from chart_viewer.core.state_manager import StateManager
from chart_viewer.models.entities import WindowState, Timeframe, Bar


def test_event_hub_crosshair_sync_and_clamping():
    hub = EventHub()

    # Register two windows in the same sync_group
    win_a = WindowState(
        window_id="daily-1",
        symbol="BTCUSDT",
        timeframe=Timeframe(unit="D", multiplier=1),
        sync_group_id="group-crypto",
    )
    win_b = WindowState(
        window_id="5m-1",
        symbol="BTCUSDT",
        timeframe=Timeframe(unit="min", multiplier=5),
        sync_group_id="group-crypto",
    )
    hub.register_window(win_a)
    hub.register_window(win_b)

    received_broadcasts = []
    hub.on_crosshair_broadcast(lambda payload: received_broadcasts.append(payload))

    # Broadcast from win_a
    hub.broadcast_crosshair(
        source_window_id="daily-1",
        timestamp=1738762200000,
        bar_index_fraction=142.37,
    )

    assert len(received_broadcasts) == 1
    assert received_broadcasts[0]["source_window_id"] == "daily-1"
    assert received_broadcasts[0]["sync_group_id"] == "group-crypto"
    assert received_broadcasts[0]["timestamp"] == 1738762200000

    # Test downtime clamping:
    # Say available bars have a 1-hour gap between 1000 and 1060
    # Available bars: [900, 960, 1000, 1060, 1120]
    available_bars = [900, 960, 1000, 1060, 1120]

    # Timestamp 1010 falls inside gap (1000..1060) -> nearest is 1000
    clamped = EventHub.clamp_timestamp_to_available_bars(1010, available_bars)
    assert clamped == 1000

    # Timestamp 1050 falls inside gap -> nearest is 1060
    clamped = EventHub.clamp_timestamp_to_available_bars(1050, available_bars)
    assert clamped == 1060

    # Timestamp completely outside -> returns None (crosshair disappears)
    assert EventHub.clamp_timestamp_to_available_bars(800, available_bars) is None
    assert EventHub.clamp_timestamp_to_available_bars(1200, available_bars) is None


def test_tick_coalescer_backpressure():
    """Verify that 1000 ticks are coalesced into a single update per window."""
    flushed_records = []

    def on_flush(win_id, data):
        flushed_records.append((win_id, data))

    coalescer = TickCoalescer(on_flush)

    # Push 1000 ticks rapidly for window-1
    for i in range(1000):
        coalescer.push_tick("window-1", {"price": 100.0 + i, "tick_num": i})

    # Flush once (representing 1 render timer tick)
    flushed_count = coalescer.flush()
    assert flushed_count == 1
    assert len(flushed_records) == 1
    assert flushed_records[0][0] == "window-1"
    # Latest known state contains the 1000th tick!
    assert flushed_records[0][1]["price"] == 1099.0
    assert flushed_records[0][1]["tick_num"] == 999

    # Second flush should do nothing (no new ticks)
    assert coalescer.flush() == 0


def test_state_manager_stateless_reset():
    sm = StateManager()

    # Apply snapshot
    snapshot = {
        "symbol": "ETHUSDT",
        "bars": [
            {"t_open": 100, "t_close": 200, "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0},
        ],
        "annotations": [
            {"id": "ann-1", "type": "hline", "anchors": [{"price": 10.5}], "style": {}},
        ],
    }
    sm.apply_snapshot("win-eth", snapshot)
    data = sm.get_window_data("win-eth")
    assert data is not None
    assert len(data.bars) == 1
    assert len(data.annotations) == 1

    # Full reset (reconnect simulation)
    sm.clear_all()
    assert sm.get_window_data("win-eth") is None
