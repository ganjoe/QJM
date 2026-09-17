"""
services/pca-service/benchmark_scanner.py — Performance Profiling & Optimization Benchmark

Benchmarks different loading and scanning strategies for the 5,000+ Parquet files.
"""

import time
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import duckdb
import pandas as pd
import pyarrow.parquet as pq

PARQUET_BASE = Path("/parquet") if Path("/parquet").is_dir() else Path("/home/daniel/stock-data-node/data/parquet")

def benchmark():
    print(f"Profiling Parquet base: {PARQUET_BASE}")
    tickers = [d.name for d in PARQUET_BASE.iterdir() if d.is_dir()][:100]
    print(f"Benchmarking on sample of {len(tickers)} tickers...\n")

    # Strategy 1: Individual duckdb.connect() per ticker (current implementation)
    t0 = time.perf_counter()
    def load_duckdb_conn(t):
        p = PARQUET_BASE / t / "1D_features.parquet"
        if not p.exists():
            p = PARQUET_BASE / t / "1D.parquet"
        if not p.exists():
            return None
        db = duckdb.connect()
        try:
            return db.execute(f"SELECT close, high, low, open, volume FROM read_parquet('{p}') ORDER BY timestamp DESC LIMIT 10").df()
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=16) as ex:
        res1 = list(ex.map(load_duckdb_conn, tickers))
    t1 = time.perf_counter()
    dur1 = t1 - t0
    print(f"Strategy 1 (duckdb.connect per ticker, 16 threads): {dur1:.3f}s for {len(tickers)} tickers ({len(tickers)/dur1:.1f} t/s)")
    print(f" -> Extrapolated for 5,000 tickers: {(dur1 / len(tickers) * 5000):.1f} seconds! (TOO SLOW!)\n")

    # Strategy 2: Single persistent DuckDB connection with cursors
    t0 = time.perf_counter()
    shared_db = duckdb.connect()
    def load_duckdb_shared(t):
        p = PARQUET_BASE / t / "1D_features.parquet"
        if not p.exists():
            p = PARQUET_BASE / t / "1D.parquet"
        if not p.exists():
            return None
        cur = shared_db.cursor()
        try:
            return cur.execute(f"SELECT close, high, low, open, volume FROM read_parquet('{p}') ORDER BY timestamp DESC LIMIT 10").df()
        finally:
            cur.close()

    with ThreadPoolExecutor(max_workers=16) as ex:
        res2 = list(ex.map(load_duckdb_shared, tickers))
    t1 = time.perf_counter()
    dur2 = t1 - t0
    print(f"Strategy 2 (Shared DuckDB cursor, 16 threads): {dur2:.3f}s for {len(tickers)} tickers ({len(tickers)/dur2:.1f} t/s)")

    # Strategy 3: Direct PyArrow Parquet File Read (tail reads without SQL parser overhead)
    t0 = time.perf_counter()
    def load_pyarrow(t):
        p = PARQUET_BASE / t / "1D_features.parquet"
        if not p.exists():
            p = PARQUET_BASE / t / "1D.parquet"
        if not p.exists():
            return None
        try:
            # Read only the schema and row groups
            parquet_file = pq.ParquetFile(p)
            num_rows = parquet_file.metadata.num_rows
            if num_rows == 0:
                return None
            # Read only required columns
            table = parquet_file.read()
            df = table.to_pandas()
            return df.tail(10)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=16) as ex:
        res3 = list(ex.map(load_pyarrow, tickers))
    t1 = time.perf_counter()
    dur3 = t1 - t0
    print(f"Strategy 3 (PyArrow ParquetFile, 16 threads): {dur3:.3f}s for {len(tickers)} tickers ({len(tickers)/dur3:.1f} t/s)")

    # Strategy 4: PyArrow read only last row group / columns needed
    t0 = time.perf_counter()
    cols = ["timestamp", "close", "high", "low", "open", "volume"]
    def load_pyarrow_cols(t):
        p = PARQUET_BASE / t / "1D_features.parquet"
        if not p.exists():
            p = PARQUET_BASE / t / "1D.parquet"
        if not p.exists():
            return None
        try:
            pf = pq.ParquetFile(p)
            # Read table
            t = pf.read(columns=[c for c in cols if c in pf.schema.names])
            return t.slice(max(0, t.num_rows - 10), 10).to_pandas()
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=16) as ex:
        res4 = list(ex.map(load_pyarrow_cols, tickers))
    t1 = time.perf_counter()
    dur4 = t1 - t0
    print(f"Strategy 4 (PyArrow selective columns + slice, 16 threads): {dur4:.3f}s for {len(tickers)} tickers ({len(tickers)/dur4:.1f} t/s)")
    print(f" -> Extrapolated for 5,000 tickers: {(dur4 / len(tickers) * 5000):.2f} seconds!")

if __name__ == "__main__":
    benchmark()
