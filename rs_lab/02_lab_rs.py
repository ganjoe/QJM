#!/usr/bin/env python3
"""Phase 2 — Lab-RS-Familie (korrigiert).

Wichtig: Komponenten werden PRO TICKER auf dessen eigenen, lückenlosen Bars
berechnet (groupby rolling / shift). Erst danach wird auf den gemeinsamen
Haupt-Handelskalender (>=300 Teilnehmer) aligniert und cross-sectional gerankt.
Sonst zerreissen Fremdmarkt-Tage die rollierenden Fenster der US-Titel.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from lab_common import CACHE_DIR, WARMUP_START, load_close_series

RS_DIR = CACHE_DIR / "lab_rs"
RS_DIR.mkdir(parents=True, exist_ok=True)
MIN_PARTICIPANTS = 300

# (name, metric, windows, weights, combine)
VARIANTS = [
    # ibd-Original (Vergleichsanker)
    ("ibd_roc_63_126_189_252_w2111", "roc", (63, 126, 189, 252), (2, 1, 1, 1), "v1"),
    # 3-Perioden-Familien, Gewichte/Fenster variiert
    ("roc_63_126_252_w211",          "roc", (63, 126, 252),       (2, 1, 1),       "v1"),
    ("roc_20_50_100_w111",           "roc", (20, 50, 100),        (1, 1, 1),       "v1"),
    ("roc_20_50_100_w123",           "roc", (20, 50, 100),        (1, 2, 3),       "v1"),
    ("roc_20_50_100_w321",           "roc", (20, 50, 100),        (3, 2, 1),       "v1"),
    ("roc_50_20_20_w1_1_1.3",        "roc", (50, 20, 20),         (1, 1, 1.3),     "v1"),
    ("roc_10_20_50_w123",            "roc", (10, 20, 50),         (1, 2, 3),       "v1"),
    ("roc_5_20_60_w111",             "roc", (5, 20, 60),          (1, 1, 1),       "v1"),
    ("roc_20_60_120_w111",           "roc", (20, 60, 120),        (1, 1, 1),       "v1"),
    ("roc_21_63_126_w111",           "roc", (21, 63, 126),        (1, 1, 1),       "v1"),
    ("roc_63_126_252_w112",          "roc", (63, 126, 252),       (1, 1, 2),       "v1"),
    ("roc_10_50_200_w111",           "roc", (10, 50, 200),        (1, 1, 1),       "v1"),
    ("roc_20_100_200_w111",          "roc", (20, 100, 200),       (1, 1, 1),       "v1"),
    ("roc_50_100_200_w211",          "roc", (50, 100, 200),       (2, 1, 1),       "v1"),
    # V2 (rank-of-ranks)
    ("roc_20_50_100_w111_v2",        "roc", (20, 50, 100),        (1, 1, 1),       "v2"),
    ("roc_10_20_50_w123_v2",         "roc", (10, 20, 50),         (1, 2, 3),       "v2"),
    ("roc_50_20_20_w1_1_1.3_v2",     "roc", (50, 20, 20),         (1, 1, 1.3),     "v2"),
    ("roc_5_20_60_w111_v2",          "roc", (5, 20, 60),          (1, 1, 1),       "v2"),
    ("roc_10_50_200_w111_v2",        "roc", (10, 50, 200),        (1, 1, 1),       "v2"),
    ("roc_63_126_252_w211_v2",       "roc", (63, 126, 252),       (2, 1, 1),       "v2"),
]


def pct_rank_rows(raw: pd.DataFrame) -> pd.DataFrame:
    rank = raw.rank(axis=1, na_option="keep")
    n = raw.notna().sum(axis=1)
    rating = rank.sub(1).div((n - 1).clip(lower=1), axis=0) * 98 + 1
    rating = rating.where(n > 1, 50.0).round().clip(1, 99)
    rating[n == 0] = np.nan
    return rating.astype("float32")


def main() -> None:
    hc = pd.read_parquet(CACHE_DIR / "highcap1000.parquet")
    tickers = list(hc["ticker"])
    long = load_close_series(tickers, start=WARMUP_START)
    long = (long.dropna(subset=["close"])
                .drop_duplicates(["ticker", "d"], keep="last")
                .sort_values(["ticker", "d"])
                .reset_index(drop=True))
    print(f"Bars geladen: {len(long):,} | {len(tickers)} Ticker")

    # ── Datenqualitaet / Integritaets-Gate (analog Produktion) ──
    _close = long["close"]
    long["_dv"] = _close * long["volume"].fillna(0.0)
    _g = long.groupby("ticker", sort=False)
    _dv50 = _g["_dv"].transform(lambda s: s.rolling(50, min_periods=1).mean())
    _med21 = _g["close"].transform(lambda s: s.rolling(21, min_periods=5).median())
    with np.errstate(invalid="ignore", divide="ignore"):
        _ratio = _close / _med21
    _bad = ((_ratio > 5.0) | (_ratio < 0.2)).fillna(False)
    _bad_ticker = pd.DataFrame({"ticker": long["ticker"], "bad": _bad}).groupby("ticker")["bad"].any()
    long["dq_ok"] = (_close > 0.5) & (_dv50 >= 1.0e5) & (~long["ticker"].map(_bad_ticker))
    print(f"Datenqualitaet: {int(long['dq_ok'].sum()):,}/{len(long):,} Bars ok | "
          f"{int((~_bad_ticker).sum())}/{len(_bad_ticker)} Ticker ohne Scale-Jump")

    close_wide = long.pivot_table(index="d", columns="ticker", values="close", aggfunc="last").sort_index()
    counts = close_wide.notna().sum(axis=1)
    calendar = close_wide.index[counts >= MIN_PARTICIPANTS]
    print(f"Close-Matrix: {close_wide.shape} | Haupt-Kalender: {len(calendar)} Tage "
          f"({pd.to_datetime(calendar[0], unit='s').date()} .. {pd.to_datetime(calendar[-1], unit='s').date()})")

    # Baseline ibd_rs parallel auf denselben Kalender
    ibd = (long.pivot_table(index="d", columns="ticker", values="ibd_rs", aggfunc="last")
               .reindex(index=calendar, columns=close_wide.columns))
    ibd_out = ibd.copy()
    ibd_out.insert(0, "d", ibd_out.index)
    ibd_out.to_parquet(RS_DIR / "ibd_rs_prod.parquet", index=False)

    _dq_wide = (long.pivot_table(index="d", columns="ticker", values="dq_ok", aggfunc="last")
                    .reindex(index=calendar, columns=close_wide.columns)
                    .fillna(False).astype(bool))

    comp_cache: dict = {}

    def component(metric: str, w: int) -> pd.DataFrame:
        key = (metric, w)
        if key in comp_cache:
            return comp_cache[key]
        g = long.groupby("ticker", sort=False)["close"]
        if metric == "ma":
            sma = g.transform(lambda s: s.rolling(w, min_periods=w).mean())
            comp = long["close"] / sma - 1.0
        else:
            comp = long["close"] / g.shift(w) - 1.0
        cw = (pd.DataFrame({"d": long["d"], "ticker": long["ticker"], "c": comp})
                .pivot_table(index="d", columns="ticker", values="c", aggfunc="last")
                .reindex(index=calendar, columns=close_wide.columns))
        cw = cw.where(_dq_wide)
        comp_cache[key] = cw
        return cw

    meta = {}
    for name, metric, windows, weights, combine in VARIANTS:
        raw = pd.DataFrame(0.0, index=calendar, columns=close_wide.columns)
        any_valid = pd.DataFrame(False, index=calendar, columns=close_wide.columns)
        for w, k in zip(windows, weights):
            c = component(metric, w)
            if combine == "v2":
                c = pct_rank_rows(c).astype("float64")
            raw = raw.add(c.fillna(0.0) * k)
            any_valid = any_valid | c.notna()
        raw = raw.where(any_valid)
        rating = pct_rank_rows(raw)
        out = rating.copy()
        out.insert(0, "d", out.index)
        out.to_parquet(RS_DIR / f"{name}.parquet", index=False)
        meta[name] = {"metric": metric, "windows": list(windows),
                      "weights": list(weights), "combine": combine}
        print(f"  -> {name:<34} coverage last row: {int(rating.iloc[-1].notna().sum())}, "
              f"median calendar coverage: {int(rating.notna().sum(axis=1).median())}")

    (RS_DIR / "variants.json").write_text(json.dumps(meta, indent=2))
    print(f"\n{len(meta)} Varianten -> {RS_DIR}")


if __name__ == "__main__":
    main()