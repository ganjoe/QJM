"""Synthetic market data feed generator for testing and benchmarks (Section 9 & 13)."""

from __future__ import annotations
import math
import random
from typing import List, Dict


def generate_synthetic_bars(
    count: int,
    start_price: float = 100.0,
    start_time: int = 1700000000,
    bar_duration_sec: int = 86400,
    downtime_gap_at: int | None = None,
    downtime_gap_bars: int = 5,
) -> List[Dict]:
    """Generate synthetic OHLCV candles with continuous arithmetic time indices."""
    bars = []
    curr_price = start_price
    curr_time = start_time

    # Use deterministic pseudo-random sequence
    rng = random.Random(42)

    for i in range(count):
        # Simulate exchange downtime gap (Section 2.1 & 4)
        if downtime_gap_at is not None and i == downtime_gap_at:
            curr_time += downtime_gap_bars * bar_duration_sec

        delta = (rng.random() - 0.49) * 2.0
        open_p = curr_price
        close_p = max(0.01, open_p + delta)
        high_p = max(open_p, close_p) + rng.random() * 1.5
        low_p = max(0.01, min(open_p, close_p) - rng.random() * 1.5)
        volume = float(rng.randint(100, 10000))

        bars.append({
            "t_open": curr_time,
            "t_close": curr_time + bar_duration_sec,
            "open": round(open_p, 4),
            "high": round(high_p, 4),
            "low": round(low_p, 4),
            "close": round(close_p, 4),
            "volume": volume,
        })

        curr_price = close_p
        curr_time += bar_duration_sec

    return bars


def generate_500k_snapshot(symbol: str = "BTCUSDT") -> Dict:
    """Generate a 500,000 bars snapshot for Section 9 benchmark."""
    bars = generate_synthetic_bars(
        count=500_000,
        start_price=50000.0,
        start_time=1600000000,
        bar_duration_sec=60,  # 1-minute bars
    )
    return {
        "symbol": symbol,
        "timeframe": {"unit": "min", "multiplier": 1},
        "y_axis_mode": "linear",
        "bars": bars,
        "overlays": [],
        "annotations": [],
    }
