#!/usr/bin/env python3
"""Phase 14 — ADR-neutrale Rangfolge (Option A).

Statt global zu ranken, wird pro Datum innerhalb von ADR-Terzilen gerankt.
Damit erzeugt hoher ADR nicht automatisch Extrem-Perzentile.
"""
from __future__ import annotations

import json
import numpy as np
import pandas as pd

from lab_bt import build_panel
from lab_common import CACHE_DIR, WARMUP_START

RS_DIR = CACHE_DIR / "lab_rs"
RS_DIR.mkdir(parents=True, exist_ok=True)

NEUTRAL = [
    ("lab_rs_n",             (63, 126, 252), (2, 1, 1)),
    ("lab_rs_n_20_40_80",    (20, 40, 80),   (1, 1, 1)),
    ("lab_rs_n_30_60_120",   (30, 60, 120),  (1, 1, 1)),
    ("lab_rs_n_50_100_200",  (50, 100, 200), (1, 1, 1)),
]
BUCKETS = 3


def adr_neutral_rank(raw: pd.DataFrame, adr: pd.DataFrame, nb: int = 3) -> pd.DataFrame:
    out = pd.DataFrame(np.nan, index=raw.index, columns=raw.columns)
    for d in raw.index:
        r = raw.loc[d]
        a = adr.loc[d]
        valid = r.notna() & a.notna()
        if int(valid.sum()) < nb * 3:
            continue
        try:
            b = pd.qcut(a[valid], nb, labels=False, duplicates="drop")
        except Exception:
            b = pd.Series(0, index=a[valid].index)
        for bv in pd.unique(b):
            idx = b.index[b == bv]
            vals = r[idx]
            if len(vals) < 2:
                out.loc[d, idx] = 50.0
                continue
            rank = vals.rank()
            out.loc[d, idx] = ((rank - 1) / (len(vals) - 1) * 98 + 1).round().clip(1, 99)
    return out.astype("float32")


def main() -> None:
    hc = pd.read_parquet(CACHE_DIR / "highcap1000.parquet")
    tickers = list(hc["ticker"])
    p = build_panel(tickers, WARMUP_START)
    cal, long, dq = p["calendar"], p["long"], p["dq"]
    adr = (long.pivot_table(index="d", columns="ticker", values="adr_20", aggfunc="last")
               .reindex(index=cal, columns=tickers))
    adr = adr.where(dq)
    print(f"Kalender {len(cal)} Tage | ADR-Matrix {adr.shape}")

    comp_cache = {}

    def component(w):
        if w in comp_cache:
            return comp_cache[w]
        g = long.groupby("ticker", sort=False)["close"]
        c = long["close"] / g.shift(w) - 1.0
        cw = (pd.DataFrame({"d": long["d"], "ticker": long["ticker"], "c": c})
                .pivot_table(index="d", columns="ticker", values="c", aggfunc="last")
                .reindex(index=cal, columns=tickers))
        cw = cw.where(dq)
        comp_cache[w] = cw
        return cw

    meta = json.loads((RS_DIR / "variants.json").read_text())
    for name, windows, weights in NEUTRAL:
        raw = pd.DataFrame(0.0, index=cal, columns=tickers)
        anyv = pd.DataFrame(False, index=cal, columns=tickers)
        for w, k in zip(windows, weights):
            c = component(w)
            raw = raw.add(c.fillna(0.0) * k)
            anyv = anyv | c.notna()
        rating = adr_neutral_rank(raw.where(anyv), adr, BUCKETS)
        out = rating.copy(); out.insert(0, "d", out.index)
        out.to_parquet(RS_DIR / f"{name}.parquet", index=False)
        meta[name] = {"metric": "roc", "windows": list(windows), "weights": list(weights),
                      "combine": "v1", "rank": "adr_neutral", "buckets": BUCKETS}
        print(f"  {name:<24} coverage {int(rating.iloc[-1].notna().sum())}")
    (RS_DIR / "variants.json").write_text(json.dumps(meta, indent=2))
    print("Varianten gesamt:", len(meta))


if __name__ == "__main__":
    main()
