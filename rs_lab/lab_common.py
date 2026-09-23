"""Shared helpers for the RS lab (test-lab only, never production).

Everything lives under rs_lab/ and prefixes DB objects with 'lab_' so the whole
experiment can be removed again without touching production code or data.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import duckdb
import httpx
import numpy as np
import pandas as pd

LAB_DIR = Path(os.environ.get("LAB_DIR", "/lab"))
CACHE_DIR = LAB_DIR / "cache"
OUT_DIR = LAB_DIR / "output"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

PARQUET_BASE = Path(os.environ.get("PARQUET_BASE_PATH", "/parquet"))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

WATCHLIST = "lab_highcap1000"
HIGHCAP_N = 1000

# Backtest window: last 2 years of trading. Warmup is loaded but never signalled.
WINDOW_YEARS = 2
WARMUP_START = "2020-01-01"
HORIZON_BARS = 63          # 3 months
BENCHMARK = "SPY"


def _headers(prefer: Optional[str] = None) -> Dict[str, str]:
    h = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY}
    if prefer:
        h["Prefer"] = prefer
    return h


def supabase_get(table: str, params: Dict[str, str]) -> list:
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{table}", params=params, headers=_headers(), timeout=180)
    r.raise_for_status()
    return r.json()


def supabase_upsert(table: str, rows: list, on_conflict: str, chunk: int = 500) -> int:
    n = 0
    for i in range(0, len(rows), chunk):
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params={"on_conflict": on_conflict},
            json=rows[i:i + chunk],
            headers=_headers("resolution=merge-duplicates,return=minimal"),
            timeout=180,
        )
        r.raise_for_status()
        n += len(rows[i:i + chunk])
    return n


def supabase_delete_list(list_name: str) -> None:
    r = httpx.delete(
        f"{SUPABASE_URL}/rest/v1/pca_watchlists",
        params={"list_name": f"eq.{list_name}"},
        headers=_headers("return=minimal"),
        timeout=60,
    )
    r.raise_for_status()


def feature_path(ticker: str, timeframe: str = "1D") -> Path:
    return PARQUET_BASE / ticker.upper() / f"{timeframe}_features.parquet"


def load_universe_meta() -> List[dict]:
    """Ticker with parquet data AND shares_outstanding, paginated."""
    out: List[dict] = []
    off = 0
    while True:
        rows = supabase_get("cda_master_universe", {
            "select": "ticker,currency,shares_outstanding",
            "has_parquet": "eq.true",
            "shares_outstanding": "not.is.null",
            "limit": "1000",
            "offset": str(off),
        })
        if not rows:
            break
        out.extend(rows)
        off += len(rows)
        if len(rows) < 1000:
            break
    return out


def _existing_paths(tickers: Iterable[str], timeframe: str = "1D"):
    paths = []
    for t in tickers:
        p = feature_path(t, timeframe)
        if p.exists():
            paths.append((t, str(p)))
    return paths


def load_last_close(tickers: Iterable[str], timeframe: str = "1D") -> pd.DataFrame:
    """Latest close + date per ticker (single DuckDB pass over the features files)."""
    paths = _existing_paths(tickers, timeframe)
    if not paths:
        raise RuntimeError("keine Parquet-Dateien gefunden")
    fl = "[" + ",".join("'" + p + "'" for _, p in paths) + "]"
    q = (
        "SELECT regexp_extract(filename, '([^/]+)/[^/]+$', 1) AS ticker, "
        "       max_by(close, timestamp) AS close, max(timestamp) AS d "
        f"FROM read_parquet({fl}, filename=true) GROUP BY 1"
    )
    con = duckdb.connect()
    try:
        return con.execute(q).df()
    finally:
        con.close()


def load_close_series(tickers: Iterable[str], start: Optional[str] = None,
                      timeframe: str = "1D") -> pd.DataFrame:
    """Long frame (d, ticker, close, volume) from start_date onward."""
    paths = _existing_paths(tickers, timeframe)
    if not paths:
        raise RuntimeError("keine Parquet-Dateien gefunden")
    fl = "[" + ",".join("'" + p + "'" for _, p in paths) + "]"
    where = ""
    if start:
        ts = int(pd.Timestamp(start, tz="UTC").timestamp())
        where = f" WHERE timestamp >= {ts}"
    q = (
        "SELECT timestamp//86400*86400 AS d, "
        "       regexp_extract(filename, '([^/]+)/[^/]+$', 1) AS ticker, "
        "       cast(open as double) AS open, cast(high as double) AS high, cast(low as double) AS low, "
        "       cast(close as double) AS close, cast(volume as double) AS volume, "
        "       cast(ibd_rs as double) AS ibd_rs, cast(adr_20 as double) AS adr_20 "
        f"FROM read_parquet({fl}, filename=true, union_by_name=true){where} ORDER BY d"
    )
    con = duckdb.connect()
    try:
        return con.execute(q).df()
    finally:
        con.close()


def month_end_flag(dates: pd.DatetimeIndex) -> np.ndarray:
    s = pd.Series(dates)
    return s.groupby([dates.year, dates.month]).transform("max").values == dates.values


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    print(f"  -> {path} ({len(df):,} rows)")
