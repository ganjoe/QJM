#!/usr/bin/env python3
"""
generate_market_breadth.py — Universe Market Breadth Calculator

Calculates the percentage and count of stocks above their 40 and 200 SMA
across the entire local Parquet universe.
Saves the result as a synthetic stock:
  /parquet/$STATS.MARKET_BREADTH/1D.parquet
  /parquet/$STATS.MARKET_BREADTH/1D_features.parquet

Columns:
  timestamp: int64 (seconds)
  open, high, low, close: float64 (mapped to 40 SMA pct for basic chart compatibility)
  volume: float64 (total stock count)
  breadth_40_pct: float64
  breadth_200_pct: float64
  count_40: int64
  count_200: int64
  total: int64
"""

import os
import sys
import time
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s"
)
logger = logging.getLogger("market_breadth")

DEFAULT_PARQUET_DIR = os.environ.get("PARQUET_BASE_PATH", "/home/daniel/stock-data-node/data/parquet")


def process_ticker_file(file_path: Path):
    """Reads timestamp and close from 1D.parquet and evaluates 40 & 200 SMA conditions."""
    try:
        tbl = pq.read_table(str(file_path), columns=["timestamp", "close"])
        if tbl.num_rows < 40:
            return None

        ts = tbl["timestamp"].to_numpy()
        close = tbl["close"].to_numpy(zero_copy_only=False).astype(float)

        # Handle millisecond timestamps if present
        if len(ts) > 0 and hasattr(ts[0], "timestamp"):
            ts_sec = np.array([int(t.timestamp()) for t in ts], dtype=np.int64)
        else:
            ts_sec = ts.astype(np.int64)
            if len(ts_sec) > 0 and ts_sec[0] > 10_000_000_000:
                ts_sec = ts_sec // 1000

        daily_ts = (ts_sec // 86400) * 86400

        valid_close = ~np.isnan(close)
        if np.count_nonzero(valid_close) < 40:
            return None

        # Calculate 40 SMA on close
        ma_40 = pd.Series(close).rolling(window=40).mean().to_numpy()
        valid_40 = ~np.isnan(ma_40)
        above_40 = np.zeros(len(close), dtype=bool)
        above_40[valid_40] = (close[valid_40] > ma_40[valid_40])

        # Calculate 200 SMA on close
        if len(close) >= 200:
            ma_200 = pd.Series(close).rolling(window=200).mean().to_numpy()
            valid_200 = ~np.isnan(ma_200)
            above_200 = np.zeros(len(close), dtype=bool)
            above_200[valid_200] = (close[valid_200] > ma_200[valid_200])
        else:
            valid_200 = np.zeros(len(close), dtype=bool)
            above_200 = np.zeros(len(close), dtype=bool)

        return daily_ts, valid_40, above_40, valid_200, above_200
    except Exception as e:
        return None


def generate_market_breadth(
    parquet_dir: str = DEFAULT_PARQUET_DIR,
    symbol: str = "$STATS.MARKET_BREADTH",
    max_workers: int = int(os.environ.get("BREADTH_WORKERS", "16")),
    min_stocks: int = int(os.environ.get("BREADTH_MIN_STOCKS", "50"))
) -> Path:

    base_path = Path(parquet_dir)
    logger.info("Starting market breadth calculation from %s", base_path)
    t0 = time.time()

    ticker_dirs = [
        d for d in base_path.iterdir()
        if d.is_dir() and not d.name.startswith("$")
    ]
    logger.info("Found %d ticker directories to inspect", len(ticker_dirs))

    target_files = []
    for d in ticker_dirs:
        f = d / "1D.parquet"
        if f.exists():
            target_files.append(f)

    logger.info("Found %d 1D.parquet files to process", len(target_files))

    counts_total_40: dict[int, int] = {}
    counts_above_40: dict[int, int] = {}
    counts_total_200: dict[int, int] = {}
    counts_above_200: dict[int, int] = {}

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_ticker_file, f) for f in target_files]
        for fut in as_completed(futures):
            res = fut.result()
            completed += 1
            if completed % 1000 == 0 or completed == len(target_files):
                logger.info("Progress: %d / %d files scanned", completed, len(target_files))

            if res is None:
                continue

            timestamps, valid_40, above_40, valid_200, above_200 = res

            for i, t in enumerate(timestamps):
                if valid_40[i]:
                    counts_total_40[t] = counts_total_40.get(t, 0) + 1
                    if above_40[i]:
                        counts_above_40[t] = counts_above_40.get(t, 0) + 1

                if valid_200[i]:
                    counts_total_200[t] = counts_total_200.get(t, 0) + 1
                    if above_200[i]:
                        counts_above_200[t] = counts_above_200.get(t, 0) + 1

    all_ts = sorted(set(counts_total_40.keys()).union(set(counts_total_200.keys())))
    logger.info("Aggregated %d distinct trading days", len(all_ts))

    rows = []
    for t in all_ts:
        tot_40 = counts_total_40.get(t, 0)
        tot_200 = counts_total_200.get(t, 0)

        if tot_40 < min_stocks and tot_200 < min_stocks:
            continue

        ab_40 = counts_above_40.get(t, 0)
        ab_200 = counts_above_200.get(t, 0)

        pct_40 = round((ab_40 / tot_40) * 100.0, 2) if tot_40 > 0 else np.nan
        pct_200 = round((ab_200 / tot_200) * 100.0, 2) if tot_200 > 0 else np.nan

        fallback_pct = pct_40 if not np.isnan(pct_40) else pct_200

        rows.append({
            "timestamp": int(t),
            "close": fallback_pct,
            "open": fallback_pct,
            "high": fallback_pct,
            "low": fallback_pct,
            "volume": float(max(tot_40, tot_200)),
            "breadth_40_pct": pct_40,
            "breadth_200_pct": pct_200,
            "count_40": int(ab_40),
            "count_200": int(ab_200),
            "total": int(max(tot_40, tot_200))
        })

    if not rows:
        raise RuntimeError("No valid data found to compute market breadth")

    df = pd.DataFrame(rows)
    df = df.sort_values("timestamp").reset_index(drop=True)

    target_dir = base_path / symbol
    target_dir.mkdir(parents=True, exist_ok=True)

    out_1d = target_dir / "1D.parquet"
    out_feat = target_dir / "1D_features.parquet"

    table = pa.Table.from_pandas(df, preserve_index=False)
    
    def safe_write(tbl: pa.Table, file_path: Path):
        temp_file = file_path.with_name(f"{file_path.name}.tmp.{os.getpid()}")
        try:
            pq.write_table(tbl, str(temp_file))
            try:
                os.chmod(str(temp_file), 0o666)
            except Exception:
                pass
            temp_file.replace(file_path)
            try:
                os.chmod(str(file_path), 0o666)
            except Exception:
                pass
        except Exception as e:
            # Fallback if temp file replace fails
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
            if file_path.exists():
                try:
                    file_path.unlink()
                except Exception:
                    pass
            pq.write_table(tbl, str(file_path))
            try:
                os.chmod(str(file_path), 0o666)
            except Exception:
                pass

    safe_write(table, out_1d)
    safe_write(table, out_feat)

    elapsed = time.time() - t0
    logger.info(
        "Successfully wrote %s (1D.parquet and 1D_features.parquet) with %d rows in %.2fs",
        symbol, len(df), elapsed
    )
    return out_1d


if __name__ == "__main__":
    p_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PARQUET_DIR
    generate_market_breadth(p_dir)
