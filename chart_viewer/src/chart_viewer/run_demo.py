"""Standalone demonstration of the Chart Viewer with an In-Process Agent."""

import sys
import random
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from chart_viewer.transport.in_process import create_in_process_pair
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.agent.synthetic_feed import generate_synthetic_bars
from chart_viewer.ui.app import ViewerApp


def main():
    app = QApplication(sys.argv)

    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    viewer_app = ViewerApp(transport=viewer_transport)

    # Start viewer and agent
    viewer_app.start()
    agent.start()

    # Agent opens Window 1: Daily BTCUSDT
    agent.open_window(
        window_id="daily-win",
        symbol="BTCUSDT",
        timeframe_unit="D",
        multiplier=1,
        sync_group_id="crypto-group",
        position={"x": 50, "y": 50},
        size={"width": 900, "height": 650},
    )

    # Agent opens Window 2: 5-Min BTCUSDT
    agent.open_window(
        window_id="5m-win",
        symbol="BTCUSDT",
        timeframe_unit="min",
        multiplier=5,
        sync_group_id="crypto-group",
        position={"x": 980, "y": 50},
        size={"width": 900, "height": 650},
    )

    # Generate historical candles
    bars_daily = generate_synthetic_bars(count=200, start_price=64000.0, bar_duration_sec=86400)
    agent.send_snapshot("daily-win", {
        "symbol": "BTCUSDT",
        "timeframe": {"unit": "D", "multiplier": 1},
        "sync_group_id": "crypto-group",
        "bars": bars_daily,
        "annotations": [
            {
                "id": "key-support",
                "type": "hline",
                "anchors": [{"price": 62000.0}],
                "style": {"color": "#00E676", "width": 2},
            }
        ],
    })

    bars_5m = generate_synthetic_bars(count=300, start_price=64500.0, bar_duration_sec=300)
    agent.send_snapshot("5m-win", {
        "symbol": "BTCUSDT",
        "timeframe": {"unit": "min", "multiplier": 5},
        "sync_group_id": "crypto-group",
        "bars": bars_5m,
        "annotations": [],
    })

    # Simulate live incoming ticks at 30 Hz
    last_price = 64500.0
    tick_timer = QTimer()

    def send_live_tick():
        nonlocal last_price
        delta = (random.random() - 0.495) * 5.0
        last_price += delta
        agent.send_tick("5m-win", price=round(last_price, 2), volume=float(random.randint(1, 20)))

    tick_timer.timeout.connect(send_live_tick)
    tick_timer.start(33)  # ~30 Hz

    print("Chart Viewer Demo running. Close windows to exit.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
