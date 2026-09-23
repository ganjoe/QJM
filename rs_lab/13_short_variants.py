#!/usr/bin/env python3
"""Phase 13 — Kurzfristige Lab-RS-Varianten (10/20/50 etc.) berechnen."""
from __future__ import annotations

import json
import numpy as np
import pandas as pd

from lab_bt import build_panel
from lab_common import CACHE_DIR, WARMUP_START

RS_DIR = CACHE_DIR / "lab_rs"
RS_DIR.mkdir(parents=True, exist_ok=True)

## Harmonische Leiter zwischen kurz und lang: 20/40/80 .. 50/100/200
SHORT = [
    ("lab_rs_20_40_80",   (20, 40, 80),   (1, 1, 1)),
    ("lab_rs_30_60_120",  (30, 60, 120),  (1, 1, 1)),
    ("lab_rs_40_80_160",  (40, 80, 160),  (1, 1, 1)),
    ("lab_rs_50_100_200", (50, 100, 200), (1, 1, 1)),
]


def pct_rank_rows(raw):
    rank = raw.rank(axis=1, na_option="keep")
    n = raw.notna().sum(axis=1)
    rating = rank.sub(1).div((n - 1).clip(lower=1), axis=0) * 98 + 1
    rating = rating.where(n > 1, 50.0).round().clip(1, 99)
    rating[n == 0] = np.nan
    return rating.astype("float32")


def main() -> None:
    hc = pd.read_parquet(CACHE_DIR / "highcap1000.parquet")
    tickers = list(hc["ticker"])
    p = build_panel(tickers, WARMUP_START)
    cal, long, dq = p["calendar"], p["long"], p["dq"]
    print(f"Kalender {len(cal)} Tage, {len(tickers)} Ticker")

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
    for name, windows, weights in SHORT:
        raw = pd.DataFrame(0.0, index=cal, columns=tickers)
        anyv = pd.DataFrame(False, index=cal, columns=tickers)
        for w, k in zip(windows, weights):
            c = component(w)
            raw = raw.add(c.fillna(0.0) * k)
            anyv = anyv | c.notna()
        rating = pct_rank_rows(raw.where(anyv))
        out = rating.copy(); out.insert(0, "d", out.index)
        out.to_parquet(RS_DIR / f"{name}.parquet", index=False)
        meta[name] = {"metric": "roc", "windows": list(windows), "weights": list(weights), "combine": "v1"}
        print(f"  {name:<24} coverage {int(rating.iloc[-1].notna().sum())}")
    (RS_DIR / "variants.json").write_text(json.dumps(meta, indent=2))
    print("Varianten gesamt:", len(meta))


if __name__ == "__main__":
    main()
