#!/usr/bin/env python3
"""Diagnose: Verteilung + groesste Einzeltrades einer Serie/Config."""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from lab_common import CACHE_DIR, HORIZON_BARS, WARMUP_START, load_close_series

VARIANT = sys.argv[1] if len(sys.argv) > 1 else "roc_63_126_252_w112"
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 40
RF = int(sys.argv[3]) if len(sys.argv) > 3 else 40
RT = int(sys.argv[4]) if len(sys.argv) > 4 else 90

HC = pd.read_parquet(CACHE_DIR / "highcap1000.parquet"); tickers = list(HC["ticker"])
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
mat = df.reindex(index=cal, columns=tickers).to_numpy(float)
rm = pd.DataFrame(mat).rolling(DAYS, min_periods=1).min().to_numpy()
m = (mat >= RT) & (rm <= RF) & ~np.isnan(mat)
prev = np.zeros_like(m); prev[1:] = m[:-1]
ii, jj = np.where(m & ~prev)
n = len(cal); H = HORIZON_BARS
last_ts = pd.Timestamp(int(cal[-1]), unit="s"); start_ts = int((last_ts - pd.DateOffset(years=2)).timestamp())
ok = np.array(cal >= start_ts); ok[n - H:] = False
keep = [(int(a), int(b)) for a, b in zip(ii, jj) if ok[a]]

def fd(i): return pd.Timestamp(int(cal[i]), unit="s").strftime("%Y-%m-%d")
recs = []
for a, b in keep:
    t = tickers[b]; e, x = a + 1, a + H
    so, sc = op.iloc[e, b], cl.iloc[x, b]
    bo, bc = spy_o.iloc[e], spy_c.iloc[x]
    if not all(np.isfinite([so, sc, bo, bc])) or so <= 0 or bo <= 0: continue
    sret, bret = sc / so - 1, bc / bo - 1
    recs.append((t, fd(a), fd(e), sret, bret, sret - bret))
d = pd.DataFrame(recs, columns=["ticker", "signal", "entry", "stock", "spy", "excess"])
print(f"{VARIANT} days={DAYS} {RF}->{RT}: {len(d)} Trades")
print("Excess-Quantile:", {q: f"{np.percentile(d['excess'], q)*100:+.1f}%" for q in (5, 25, 50, 75, 95)})
print(f"Hit: {np.mean(d['excess']>=0):.3f} | Mittel: {d['excess'].mean()*100:+.1f}% | Median: {d['excess'].median()*100:+.1f}%")
print("\nTop 12 Excess-Trades:")
for _, r in d.sort_values("excess", ascending=False).head(12).iterrows():
    print(f"  {r['ticker']:<7} sig {r['signal']} entry {r['entry']} stock {r['stock']*100:+8.1f}% spy {r['spy']*100:+5.1f}% excess {r['excess']*100:+8.1f}%")
print("\nBottom 5:")
for _, r in d.sort_values("excess").head(5).iterrows():
    print(f"  {r['ticker']:<7} sig {r['signal']} stock {r['stock']*100:+8.1f}% spy {r['spy']*100:+5.1f}% excess {r['excess']*100:+8.1f}%")
