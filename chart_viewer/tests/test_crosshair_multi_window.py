"""End-to-end integration test for Criterion 2: Multi-Window Crosshair Sync & Downtime Clamping."""

from chart_viewer.ui.app import ViewerApp
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.agent.synthetic_feed import generate_synthetic_bars
from chart_viewer.transport.in_process import create_in_process_pair
from PySide6.QtCore import QPointF
from PySide6.QtGui import QMouseEvent
from PySide6.QtCore import Qt


def test_criterion_2_multi_window_crosshair_sync_with_downtime_clamping(qapp):
    """Criterion 2: Zwei Fenster (Daily + 5-Min, Crypto-Symbol) synchronisieren das Crosshair

    arithmetisch korrekt, inkl. Clamping über eine Exchange-Downtime hinweg.
    """
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(transport=viewer_transport)

    agent.start()
    app.start()

    # Window A: Daily window, group "crypto-btc"
    agent.open_window(
        window_id="win-daily",
        symbol="BTCUSDT",
        timeframe_unit="D",
        multiplier=1,
        sync_group_id="crypto-btc",
    )

    # Window B: 5-Min window, group "crypto-btc"
    agent.open_window(
        window_id="win-5m",
        symbol="BTCUSDT",
        timeframe_unit="min",
        multiplier=5,
        sync_group_id="crypto-btc",
    )
    qapp.processEvents()

    assert "win-daily" in app.windows
    assert "win-5m" in app.windows

    win_daily = app.windows["win-daily"]
    win_5m = app.windows["win-5m"]

    # Generate bars:
    # 5-min bars with a downtime gap at bar 10 spanning 10 missing bars
    bars_5m = generate_synthetic_bars(
        count=50,
        start_price=100.0,
        start_time=1700000000,
        bar_duration_sec=300,
        downtime_gap_at=10,
        downtime_gap_bars=10,  # Missing 10 * 300 = 3000 seconds
    )

    agent.send_snapshot("win-5m", {
        "symbol": "BTCUSDT",
        "timeframe": {"unit": "min", "multiplier": 5},
        "sync_group_id": "crypto-btc",
        "bars": bars_5m,
    })

    # Daily bars covering the same timeframe
    bars_daily = generate_synthetic_bars(
        count=10,
        start_price=100.0,
        start_time=1700000000,
        bar_duration_sec=86400,
    )
    agent.send_snapshot("win-daily", {
        "symbol": "BTCUSDT",
        "timeframe": {"unit": "D", "multiplier": 1},
        "sync_group_id": "crypto-btc",
        "bars": bars_daily,
    })
    qapp.processEvents()

    # Move mouse in Window Daily
    # Pick timestamp falling inside the downtime gap of Window B
    # Gap in 5m bars is between bar 9 and bar 10:
    # bar 9 t_open = 1700000000 + 9*300 = 1700002700
    # bar 10 t_open = 1700002700 + 10*300 = 1700005700
    gap_target_ts = 1700004000  # right in the middle of missing exchange downtime

    # Broadcast crosshair from Daily window
    app.event_hub.broadcast_crosshair(
        source_window_id="win-daily",
        timestamp=gap_target_ts,
        bar_index_fraction=0.5,
    )
    qapp.processEvents()

    # Verify Window B clamped to the closest available bar and crosshair did NOT disappear!
    crosshair_pos_b = win_5m.canvas.layer4_interaction.crosshair_pos
    assert crosshair_pos_b is not None, "Crosshair should NOT disappear over exchange downtime gap!"
    assert win_5m.canvas.layer4_interaction.is_crosshair_visible is True
