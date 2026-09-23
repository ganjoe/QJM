#!/usr/bin/env python3
"""Phase 3 — Backtest: Phoenix-Trigger auf Lab-RS, 63-Tage-Excess vs. SPY.

Entry: Open des naechsten Handelstags nach dem Signal.
Exit : Close nach 63 Bars (~1 Quartal).
Primaer: Hit-Rate (Excess >= 0) und mittlerer/medianer Excess.
Sekundaer: relativer Drawdown vs. SPY im Haltefenster (mean_max_rel_dd, mean_min_rel).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from lab_common import (BENCHMARK, CACHE_DIR, HORIZON_BARS, OUT_DIR, WARMUP_START,
                        load_close_series, write_parquet)

RS_DIR = CACHE_DIR / "lab_rs"
DAYS_GRID = (10, 20, 30, 40)
FROM_GRID = (20, 30, 40)
TO_GRID = (70, 80, 90)


def load_panel(tickers, start):
    long = load_close_series(tickers, start=start)
    long = (long.dropna(subset=["close", "open"])
                .drop_duplicates(["ticker", "d"], keep="last")
                .sort_values(["ticker", "d"]))
    g = long.groupby("ticker", sort=False)
    # nur zum pivotieren
    op = long.pivot_table(index="d", columns="ticker", values="open", aggfunc="last")
    cl = long.pivot_table(index="d", columns="ticker", values="close", aggfunc="last")
    return op, cl


def rolling_ratio_dd(ratio: np.ndarray, e0: int, H: int) -> np.ndarray:
    """max Drawdown der Ratio-Kurve ueber [t+e0, t+e0+H-1] je Zeile t."""
    n, m = ratio.shape
    out = np.full((n, m), np.nan)
    for t in range(n - (e0 + H) + 1):
        w = ratio[t + e0: t + e0 + H]
        rm = np.maximum.accumulate(w, axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[t] = np.nanmin(w / rm - 1.0, axis=0)
    return out


def main() -> None:
    hc = pd.read_parquet(CACHE_DIR / "highcap1000.parquet")
    tickers = list(hc["ticker"])
    variants = json.loads((RS_DIR / "variants.json").read_text())
    rs_names = ["ibd_rs_prod"] + list(variants.keys())

    # Kalender + Close/Open der highcap + SPY
    op, cl = load_panel(tickers, WARMUP_START)
    spy_op, spy_cl = load_panel([BENCHMARK], WARMUP_START)
    calendar = pd.Index(cl.index)
    # nur Haupt-Handelskalender: alle Tage, an denen die Lab-RS-Datei Werte hat
    ref = pd.read_parquet(RS_DIR / "ibd_rs_prod.parquet")
    calendar = pd.Index(ref["d"].values if "d" in ref.columns else ref.index.values)
    op = op.reindex(index=calendar, columns=tickers)
    cl = cl.reindex(index=calendar, columns=tickers)
    spy_op = spy_op.reindex(index=calendar)[BENCHMARK]
    spy_cl = spy_cl.reindex(index=calendar)[BENCHMARK]
    print(f"Panel: {cl.shape[0]} Tage x {cl.shape[1]} Ticker | "
          f"{pd.to_datetime(calendar[0], unit='s').date()} .. {pd.to_datetime(calendar[-1], unit='s').date()}")

    n = len(calendar)
    H = HORIZON_BARS
    last_ts = pd.Timestamp(calendar[-1], unit="s", tz="UTC")
    start_ts = int((last_ts - pd.DateOffset(years=2)).timestamp())
    window_mask = np.array(calendar >= start_ts)
    horizon_ok = np.zeros(n, dtype=bool)
    horizon_ok[: n - H] = True   # braucht t+1 (entry) und t+H (exit)
    valid_t = window_mask & horizon_ok
    print(f"Backtest-Fenster: {pd.to_datetime(start_ts, unit='s').date()} .. {last_ts.date()} "
          f"({int(valid_t.sum())} Handelstage, H={H})")

    # Forward-Matrizen (Index = Signaldatum t)
    open_np = op.to_numpy(dtype=float)
    close_np = cl.to_numpy(dtype=float)
    so = spy_op.to_numpy(dtype=float)
    sc = spy_cl.to_numpy(dtype=float)

    entry_open = np.full_like(open_np, np.nan)
    exit_close = np.full_like(close_np, np.nan)
    spy_entry = np.full(n, np.nan)
    spy_exit = np.full(n, np.nan)
    if n > H + 1:
        # Signal t -> Entry-Open[t+1], Exit-Close[t+H]  (H Bars gehalten)
        entry_open[: n - H] = open_np[1: n - H + 1]
        exit_close[: n - H] = close_np[H: n]
        spy_entry[: n - H] = so[1: n - H + 1]
        spy_exit[: n - H] = sc[H: n]

    with np.errstate(invalid="ignore", divide="ignore"):
        stock_fwd = exit_close / entry_open - 1.0
        spy_fwd = spy_exit / spy_entry - 1.0
    excess = stock_fwd - spy_fwd[:, None]

    ratio = close_np / sc[:, None]
    ratio_entry = entry_open / spy_entry[:, None]
    with np.errstate(invalid="ignore", divide="ignore"):
        min_rel = np.full_like(ratio, np.nan)
        # min Ratio im Fenster [t+1, t+H] / Entry-Ratio
        # vektorisiert ueber Offsets
        for t in range(n - H):
            w = ratio[t + 1: t + 1 + H]
            min_rel[t] = np.nanmin(w, axis=0) / ratio_entry[t] - 1.0
    rel_dd = rolling_ratio_dd(ratio, 1, H)

    rows = []
    for name in rs_names:
        df = pd.read_parquet(RS_DIR / f"{name}.parquet")
        if "d" in df.columns:
            df = df.set_index("d")
        # WICHTIG: Spalten auf die Ticker-Reihenfolge von excess/rel_dd/min_rel ausrichten
        df = df.reindex(index=calendar, columns=tickers)
        mat = df.to_numpy(dtype=float)
        if name == "ibd_rs_prod":
            print("  ibd_rs_prod gelesen")
        for days in DAYS_GRID:
            roll_min = pd.DataFrame(mat).rolling(days, min_periods=1).min().to_numpy()
            for rf in FROM_GRID:
                for rt in TO_GRID:
                    matched = np.zeros_like(mat, dtype=bool)
                    m = (mat >= rt) & (roll_min <= rf)
                    m = m & ~np.isnan(mat)
                    prev = np.zeros_like(m)
                    prev[1:] = m[:-1]
                    matched = m & ~prev
                    matched &= valid_t[:, None]
                    ii, jj = np.where(matched)
                    if len(ii) == 0:
                        rows.append({"series": name, "days": days, "rs_from": rf, "rs_to": rt,
                                     "n": 0, "hit_rate": np.nan, "mean_excess": np.nan,
                                     "median_excess": np.nan, "p25_excess": np.nan,
                                     "mean_max_rel_dd": np.nan, "mean_min_rel": np.nan})
                        continue
                    ex = excess[ii, jj]
                    dd = rel_dd[ii, jj]
                    mr = min_rel[ii, jj]
                    rows.append({
                        "series": name, "days": days, "rs_from": rf, "rs_to": rt,
                        "n": int(len(ex)),
                        "hit_rate": float(np.mean(ex >= 0)),
                        "mean_excess": float(np.nanmean(ex)),
                        "median_excess": float(np.nanmedian(ex)),
                        "p25_excess": float(np.nanpercentile(ex, 25)),
                        "mean_max_rel_dd": float(np.nanmean(dd)),
                        "mean_min_rel": float(np.nanmean(mr)),
                    })
        print(f"  {name}: {sum(1 for r in rows if r['series']==name)} Configs")

    res = pd.DataFrame(rows)
    write_parquet(res, OUT_DIR / "backtest_results.parquet")
    res.to_csv(OUT_DIR / "backtest_results.csv", index=False)

    top = res[res["n"] >= 30].sort_values("hit_rate", ascending=False).head(25)
    print("\nTop 25 nach Hit-Rate (n>=30):")
    print(top[["series", "days", "rs_from", "rs_to", "n", "hit_rate", "mean_excess",
               "median_excess", "mean_max_rel_dd", "mean_min_rel"]].to_string(index=False))


if __name__ == "__main__":
    main()
