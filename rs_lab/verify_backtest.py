#!/usr/bin/env python3
"""Sanity-Check: einzelne Trades des Backtests gegen Roh-Parquet verifizieren."""
from __future__ import annotations
import duckdb
import numpy as np
import pandas as pd
from lab_common import CACHE_DIR, HORIZON_BARS, WARMUP_START, load_close_series

VARIANT, DAYS, RF, RT = "ibd_rs_prod", 20, 20, 80

HC = pd.read_parquet(CACHE_DIR / "highcap1000.parquet")
tickers = list(HC["ticker"])
long = load_close_series(tickers, start=WARMUP_START).dropna(subset=["open", "close"])
long = long.drop_duplicates(["ticker", "d"], keep="last").sort_values(["ticker", "d"])
op = long.pivot_table(index="d", columns="ticker", values="open", aggfunc="last")
cl = long.pivot_table(index="d", columns="ticker", values="close", aggfunc="last")
spy = load_close_series(["SPY"], start=WARMUP_START).dropna(subset=["open", "close"]).drop_duplicates(["d"]).sort_values("d")
ref = pd.read_parquet(CACHE_DIR / "lab_rs" / "ibd_rs_prod.parquet")
cal = pd.Index(ref["d"].values if "d" in ref.columns else ref.index.values)
op = op.reindex(index=cal, columns=tickers); cl = cl.reindex(index=cal, columns=tickers)
spy_o = spy.set_index("d")["open"].reindex(cal); spy_c = spy.set_index("d")["close"].reindex(cal)

df = pd.read_parquet(CACHE_DIR / "lab_rs" / f"{VARIANT}.parquet")
if "d" in df.columns: df = df.set_index("d")
mat = df.reindex(cal)[tickers].to_numpy(float)
rm = pd.DataFrame(mat).rolling(DAYS, min_periods=1).min().to_numpy()
m = (mat >= RT) & (rm <= RF) & ~np.isnan(mat)
prev = np.zeros_like(m); prev[1:] = m[:-1]
n = len(cal); H = HORIZON_BARS
last_ts = pd.Timestamp(cal[-1], unit="s", tz="UTC")
start_ts = int((last_ts - pd.DateOffset(years=2)).timestamp())
ok = np.array(cal >= start_ts); ok[n - H:] = False
ii, jj = np.where(m & ~prev)
keep = [(int(a), int(b)) for a, b in zip(ii, jj) if ok[a]]
print(f"Config {VARIANT} days={DAYS} {RF}->{RT}: {len(keep)} Signale in den letzten 2 Jahren")

def fdate(idx):
    return pd.Timestamp(int(cal[idx]), unit="s", tz="UTC").strftime("%Y-%m-%d")

print(f"{'signal':>10} {'ticker':<7} {'entry':>10} {'e_open':>9} {'exit':>10} {'x_close':>9} {'stock':>8} {'spy':>8} {'excess':>8}")
for a, b in keep[:10]:
    t = tickers[b]; entry, exit_ = a + 1, a + H
    so, sc = op.iloc[entry, b], cl.iloc[exit_, b]
    bo, bc = spy_o.iloc[entry], spy_c.iloc[exit_]
    sret, bret = sc / so - 1, bc / bo - 1
    print(f"{fdate(a):>10} {t:<7} {fdate(entry):>10} {so:>9.2f} {fdate(exit_):>10} {sc:>9.2f} {sret*100:>7.1f}% {bret*100:>7.1f}% {(sret-bret)*100:>7.1f}%")

a, b = keep[0]; t = tickers[b]
con = duckdb.connect()
rows = con.execute(f"SELECT timestamp, open, close FROM read_parquet('/parquet/{t}/1D_features.parquet') ORDER BY timestamp").fetchall()
dates = [int(r[0]) for r in rows]
ei, xi = dates.index(int(cal[a + 1])), dates.index(int(cal[a + H]))
print(f"\nDuckDB-Check {t}: entry {fdate(a+1)} open={rows[ei][1]:.2f} | exit {fdate(a+H)} close={rows[xi][2]:.2f} "
      f"| ret={rows[xi][2]/rows[ei][1]-1:.4f}")
