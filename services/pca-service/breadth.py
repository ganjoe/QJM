import os
import logging
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import pyarrow.parquet as pq
import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("pca.breadth")
router = APIRouter()

# Support both inside docker (/parquet) and host path
PARQUET_BASE = Path(os.environ.get("PARQUET_BASE_PATH", "/parquet" if Path("/parquet").exists() else "/home/daniel/stock-data-node/data/parquet"))
DEFAULT_WORKERS = int(os.environ.get("BREADTH_MAX_WORKERS", "16"))
DEFAULT_MIN_STOCKS = int(os.environ.get("BREADTH_MIN_STOCKS", "50"))


class BreadthRequest(BaseModel):
    ma_type: str = Field(default="SMA", description="SMA or EMA")
    period: int = Field(default=200, ge=1, le=1000, description="MA period (e.g. 200, 100, 50)")
    lookback_days: int = Field(default=5, ge=1, le=500, description="Number of recent trading days to return")
    timeframe: str = Field(default="1D", description="Timeframe (default: 1D)")
    tickers: Optional[List[str]] = Field(default=None, description="Optional list of tickers. If omitted, scans universe.")


def get_available_tickers() -> List[str]:
    if not PARQUET_BASE.exists():
        return []
    return [d.name for d in PARQUET_BASE.iterdir() if d.is_dir() and not d.name.startswith("$")]


def evaluate_ticker_breadth(ticker: str, timeframe: str, ma_type: str, period: int, lookback_days: int) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    parquet_file = PARQUET_BASE / ticker.upper() / f"{timeframe.upper()}.parquet"
    if not parquet_file.exists():
        return None

    try:
        tbl = pq.read_table(str(parquet_file), columns=["timestamp", "close"])
        if tbl.num_rows < period:
            return None

        close = tbl["close"].to_numpy(zero_copy_only=False).astype(float)
        ts = tbl["timestamp"].to_numpy()

        valid = ~np.isnan(close)
        if not np.any(valid):
            return None

        close = close[valid]
        ts = ts[valid]

        if len(close) < period:
            return None

        if len(ts) > 0 and hasattr(ts[0], "timestamp"):
            ts_sec = np.array([int(t.timestamp()) for t in ts], dtype=np.int64)
        else:
            ts_sec = ts.astype(np.int64)
            if len(ts_sec) > 0 and ts_sec[0] > 10_000_000_000:
                ts_sec = ts_sec // 1000

        daily_ts = (ts_sec // 86400) * 86400

        if ma_type.upper() == "EMA":
            ma = pd.Series(close).ewm(span=period, adjust=False).mean().to_numpy()
        else:
            ma = pd.Series(close).rolling(window=period).mean().to_numpy()

        above = close > ma
        slice_len = min(lookback_days, len(daily_ts))
        return daily_ts[-slice_len:], above[-slice_len:]
    except Exception as e:
        logger.debug("Error evaluating ticker %s: %s", ticker, e)
        return None


@router.post("/indicators/breadth")
async def calculate_market_breadth_endpoint(req: BreadthRequest):
    t0 = time.perf_counter()
    tickers = req.tickers
    if not tickers:
        tickers = get_available_tickers()

    tickers = [t for t in tickers if not t.startswith("$")]
    total_universe = len(tickers)

    if total_universe == 0:
        return {
            "status": "ok",
            "ma_type": req.ma_type.upper(),
            "period": req.period,
            "lookback_days": req.lookback_days,
            "total_universe": 0,
            "elapsed_seconds": 0.0,
            "series": []
        }

    counts_above: Dict[int, int] = {}
    counts_total: Dict[int, int] = {}

    with ThreadPoolExecutor(max_workers=DEFAULT_WORKERS) as executor:
        futures = [
            executor.submit(evaluate_ticker_breadth, t, req.timeframe, req.ma_type, req.period, req.lookback_days)
            for t in tickers
        ]
        for fut in as_completed(futures):
            res = fut.result()
            if res is None:
                continue
            timestamps, above_flags = res
            for t_val, is_above in zip(timestamps, above_flags):
                t_int = int(t_val)
                counts_total[t_int] = counts_total.get(t_int, 0) + 1
                if is_above:
                    counts_above[t_int] = counts_above.get(t_int, 0) + 1

    valid_ts = [t for t in sorted(counts_total.keys()) if counts_total[t] >= DEFAULT_MIN_STOCKS]
    all_ts = valid_ts[-req.lookback_days:]

    series = []
    for t_int in all_ts:
        tot = counts_total[t_int]
        if tot < DEFAULT_MIN_STOCKS:
            continue
        ab = counts_above.get(t_int, 0)
        pct = round((ab / tot) * 100.0, 2) if tot > 0 else 0.0
        dt = datetime.fromtimestamp(t_int, tz=timezone.utc).strftime("%Y-%m-%d")
        series.append({
            "timestamp": t_int,
            "date": dt,
            "percentage": pct,
            "count": ab,
            "total": tot
        })

    elapsed = round(time.perf_counter() - t0, 3)
    logger.info("Breadth %s(%d) computed for %d tickers in %.3fs", req.ma_type, req.period, total_universe, elapsed)

    return {
        "status": "ok",
        "ma_type": req.ma_type.upper(),
        "period": req.period,
        "lookback_days": req.lookback_days,
        "total_universe": total_universe,
        "elapsed_seconds": elapsed,
        "series": series
    }
