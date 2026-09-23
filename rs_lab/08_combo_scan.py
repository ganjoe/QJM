#!/usr/bin/env python3
"""Phase 8 — Combo: MADBO (Kandidat) + Phoenix (Bestaetigung).

Frage: Findet Phoenix als Bestaetigung des MADBO-Scanners Charts, die den Markt
(out)performen besser als MADBO oder Phoenix allein?

MADBO wird hier identisch zur Produktion aus OHLCV gerechnet:
  MA-Faecher(10/20/50/100/200) komprimiert (< ATR(1)) UND Dollarvolumen > 2x
  dessen 50-Tage-Durchschnitt.
Combo = Phoenix-Trigger, nachdem innerhalb der letzten N Bars ein MADBO feuerte.
Forward 63 Bars Excess vs. SPY, Entry Folge-Open.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from lab_bt import build_panel
from lab_common import (CACHE_DIR, HORIZON_BARS, OUT_DIR, WARMUP_START,
                        supabase_upsert, write_parquet)

START = "2022-01-01"
MADBO_WINDOWS = (63, 126, 252)
# (Phoenix-Serie, days, from, to)
PHX = [
    ("roc_63_126_252_w122", 40, 30, 95),
    ("roc_63_126_189_252_w1111", 10, 40, 90),
    ("ibd_rs_prod", 20, 30, 80),
]


def wavg(vals, weights):
    vals = np.asarray(vals, float); weights = np.asarray(weights, float)
    m = np.isfinite(vals) & np.isfinite(weights) & (weights > 0)
    return float(np.sum(vals[m] * weights[m]) / np.sum(weights[m])) if m.any() else np.nan


def metrics(ex, mask, years, y_from):
    sel = mask & (years >= y_from)[:, None]
    ii, jj = np.where(sel)
    if len(ii) == 0:
        return dict(n=0, hit=np.nan, mean=np.nan, median=np.nan)
    v = ex[ii, jj]
    return dict(n=int(len(v)), hit=float(np.mean(v >= 0)), mean=float(np.nanmean(v)),
                median=float(np.nanmedian(v)))


def main() -> None:
    hc = pd.read_parquet(CACHE_DIR / "highcap1000.parquet")
    tickers = list(hc["ticker"])
    p = build_panel(tickers, START)
    cal, years, n = p["calendar"], p["years"], p["n"]
    long = p["long"]
    print(f"Kalender {pd.to_datetime(cal[0], unit='s').date()} .. {pd.to_datetime(cal[-1], unit='s').date()}")

    # ── MADBO (Kandidat) ──
    g = long.groupby("ticker", sort=False)["close"]
    sma = {w: g.transform(lambda s, w=w: s.rolling(w, min_periods=w).mean()) for w in (10, 20, 50, 100, 200)}
    spread = pd.concat(sma.values(), axis=1).max(axis=1) - pd.concat(sma.values(), axis=1).min(axis=1)
    prev = g.shift(1)
    tr = pd.concat([(long["high"] - long["low"]).abs(),
                    (long["high"] - prev).abs(), (long["low"] - prev).abs()], axis=1).max(axis=1)
    long["_dv"] = long["close"] * long["volume"].fillna(0.0)
    dv50 = long.groupby("ticker", sort=False)["_dv"].transform(lambda s: s.rolling(50, min_periods=1).mean())
    long["madbo"] = (spread < tr) & (long["_dv"] > 2.0 * dv50) & long["dq_ok"]
    madbo_w = (long.pivot_table(index="d", columns="ticker", values="madbo", aggfunc="last")
                   .reindex(index=cal, columns=tickers).astype("boolean").fillna(False).astype(bool))
    print(f"MADBO-Signale gesamt: {int(madbo_w.to_numpy().sum()):,}")

    madbo_recent = {}
    for w in MADBO_WINDOWS:
        madbo_recent[w] = (madbo_w.astype(float).rolling(w, min_periods=1).max().fillna(0.0) > 0).to_numpy(bool)

    phx = {}
    for name, days, rf, rt in PHX:
        df = pd.read_parquet(CACHE_DIR / "lab_rs" / f"{name}.parquet")
        if "d" in df.columns:
            df = df.set_index("d")
        mat = df.reindex(index=cal, columns=tickers).to_numpy(float)
        rm = pd.DataFrame(mat).rolling(days, min_periods=1).min().to_numpy()
        m = (mat >= rt) & (rm <= rf) & ~np.isnan(mat)
        prevm = np.zeros_like(m); prevm[1:] = m[:-1]
        phx[name] = m & ~prevm
    ibd = long.pivot_table(index="d", columns="ticker", values="ibd_rs", aggfunc="last").reindex(index=cal, columns=tickers)
    phx["ibd_rs_prod"] = None  # wird unten gesetzt, falls nicht als lab-Datei vorhanden
    # ibd_rs_prod Phoenix separat
    ibd_mat = ibd.to_numpy(float)
    rm = pd.DataFrame(ibd_mat).rolling(20, min_periods=1).min().to_numpy()
    m = (ibd_mat >= 80) & (rm <= 30) & ~np.isnan(ibd_mat)
    prevm = np.zeros_like(m); prevm[1:] = m[:-1]
    phx["ibd_rs_prod"] = m & ~prevm

    ex = p["excess"]
    rows = []
    for name, days, rf, rt in PHX:
        trig = phx[name]
        mo = metrics(ex, madbo_w.to_numpy(), years, 2023)
        po = metrics(ex, trig, years, 2023)
        rows.append({"kind": "MADBO only", "phoenix": "-", "window": "-", **mo})
        rows.append({"kind": "Phoenix only", "phoenix": name, "window": "-", **po})
        for w in MADBO_WINDOWS:
            cm = trig & madbo_recent[w]
            rows.append({"kind": "Combo (MADBO vor Phoenix)", "phoenix": name, "window": w,
                         **metrics(ex, cm, years, 2023)})
        # Kombination: MADBO, aber erst N Jahre? nicht noetig
    res = pd.DataFrame(rows)
    res["window"] = res["window"].astype(str)
    write_parquet(res, OUT_DIR / "combo_results.parquet")
    res.to_csv(OUT_DIR / "combo_results.csv", index=False)
    print("\nCombo-Ergebnis (ab 2023, 63d Excess vs SPY):")
    print(res.to_string(index=False))

    # ── Aktuelle Combo-Kandidaten + Watchlist ──
    last_ts = int(cal[-1])
    recent_cut = last_ts - 180 * 86400
    recent_idx = int(np.searchsorted(cal.values, recent_cut))
    cands = []
    for name, days, rf, rt in PHX:
        cm = phx[name] & madbo_recent[252]
        for j, t in enumerate(tickers):
            idxs = np.where(cm[recent_idx:, j])[0]
            if len(idxs):
                rows_j = recent_idx + idxs
                cands.append({"ticker": t, "phoenix": name,
                              "last_combo": pd.Timestamp(int(cal[rows_j[-1]]), unit="s").strftime("%Y-%m-%d"),
                              "n_combo_180d": int(len(rows_j))})
    cand = pd.DataFrame(cands).drop_duplicates(["ticker", "phoenix"])
    cand.to_csv(OUT_DIR / "combo_candidates.csv", index=False)
    uniq = sorted(cand["ticker"].unique())
    print(f"\nAktuelle Combo-Kandidaten (letzte 180 Tage, MADBO<=252d): {len(uniq)}")
    print(", ".join(uniq[:80]))
    if uniq:
        wl = [{"list_name": "lab_combo_recent", "ticker": t, "position": i} for i, t in enumerate(uniq)]
        supabase_upsert("pca_watchlists", wl, on_conflict="list_name,ticker")
        print(f"Watchlist lab_combo_recent: {len(uniq)} Ticker")
    print("-> output/combo_results.csv, combo_candidates.csv")


if __name__ == "__main__":
    main()
