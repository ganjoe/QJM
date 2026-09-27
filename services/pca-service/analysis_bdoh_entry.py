"""Teil 2: Einstiegsvarianten + Signifikanz fuer ww_bdoh + IBD-RS (High-Cap-1000, ab 2007).

Einstiege:
  f_h   : Kauf zum Close des Signal-Balkens (der Sprung-Balken, %K > 80)
  p_h   : Kauf zum Close des VORBALKENS (= der %K < 20-Balken, das "Tief")
  a3_h  : Kauf 3 Balken nach dem Signal (Ruecksetzer abwarten)
Alle Reihen zusaetzlich marktrelativ (x = minus Tagesmittel des Universums).
"""
import json
import os
import time

import numpy as np
import pandas as pd

from analysis_bdoh_edge import (HORIZONS, OUT, RS_LEVEL, SCANNER, PARAMS, build_bars,
                                dedupe, describe, log)


def enrich(frames):
    return frames


def boot_diff_medians(bars, mask_a, mask_b, col, n_boot=2000, seed=23):
    """Ticker-geclusterter Bootstrap der Median-Differenz A - B."""
    def per_ticker(mask):
        d = bars.loc[mask, ["ticker", col]].dropna()
        codes, _ = pd.factorize(d["ticker"])
        vals = d[col].to_numpy(dtype="float64")
        order = np.argsort(codes, kind="stable")
        return codes[order], vals[order]

    ca, va = per_ticker(mask_a)
    cb, vb = per_ticker(mask_b)
    tick = sorted(set(ca.tolist()) | set(cb.tolist()))
    remap_a = {c: i for i, c in enumerate(sorted(set(ca.tolist())))}
    remap_b = {c: i for i, c in enumerate(sorted(set(cb.tolist())))}
    # gemeinsame Ticker-Indizierung
    common = {t: i for i, t in enumerate(tick)}
    ia = np.array([common[c] for c in ca])
    ib = np.array([common[c] for c in cb])
    n = len(tick)
    parts_a = [va[ia == i] for i in range(n)]
    parts_b = [vb[ib == i] for i in range(n)]
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(n_boot):
        pick = rng.integers(0, n, n)
        pa = np.concatenate([parts_a[p] for p in pick])
        pb = np.concatenate([parts_b[p] for p in pick])
        if len(pa) > 5 and len(pb) > 5:
            diffs.append(np.median(pa) - np.median(pb))
    if not diffs:
        return None
    d = np.array(diffs)
    return {"diff": float(np.median(va) - np.median(vb)), "lo": float(np.percentile(d, 2.5)),
            "hi": float(np.percentile(d, 97.5)), "p_le_0": float((d <= 0).mean())}


def main():
    bars, wins = build_bars()
    # Zusatzspalten: Einstieg zum Vorbalken-Close und 3 Balken spaeter
    log("building entry variants ...")
    extra = []
    for ticker, g in bars.groupby("ticker", sort=False):
        g = g.reset_index(drop=True)
        close = g["close"].astype(float)
        d = {"ticker": ticker, "ts": g["ts"].astype("int64")}
        for h in HORIZONS:
            d["p%d" % h] = close.shift(-(h - 1)) / close.shift(1) - 1.0
            d["a3_%d" % h] = close.shift(-(h + 3)) / close.shift(-3) - 1.0
        extra.append(pd.DataFrame(d))
    ex = pd.concat(extra, ignore_index=True)
    bars = bars.merge(ex, on=["ticker", "ts"], how="left")
    for h in HORIZONS:
        for pre in ("p", "a3_"):
            col = "%s%d" % (pre, h)
            bars[col] = bars[col] * 100.0
            lo, hi = bars[col].quantile(0.005), bars[col].quantile(0.995)
            bars[col] = bars[col].clip(lo, hi)
            bars["%sx%d" % (pre, h)] = bars[col] - bars.groupby("ts")[col].transform("mean")

    rs = bars["rs"]
    rs_ok = rs > RS_LEVEL
    rs_recent = bars.groupby("ticker")["rs"].transform(lambda s: s.rolling(60, min_periods=1).max()) > RS_LEVEL
    sig23 = bars["sig2"] | bars["sig3"]
    groups = {
        "alle Bars": pd.Series(True, index=bars.index),
        "RS>85": rs_ok,
        "RS>85 (letzte 60 Bars)": rs_recent,
        "ww_bdoh_2|3": sig23,
        "ww_bdoh_2|3 + RS": sig23 & rs_ok,
        "ww_bdoh_2|3 + RS(60d)": sig23 & rs_recent,
        "ww_bdoh_1 + RS": bars["sig1"] & rs_ok,
        "ww_bdoh_2 + RS": bars["sig2"] & rs_ok,
        "ww_bdoh_3 + RS": bars["sig3"] & rs_ok,
    }
    ded = {k: (dedupe(bars, m) if k != "alle Bars" else m) for k, m in groups.items()}

    rows = []
    for name, mask in groups.items():
        for h in HORIZONS:
            entry_specs = [("signal-close", "x%d" % h), ("tief-close (Vorbalken)", "px%d" % h),
                           ("+3 Bars", "a3_x%d" % h)]
            for label, col in entry_specs:
                st = describe(bars.loc[ded[name], col])
                if st is None:
                    continue
                rows.append({"group": name, "entry": label, "h": h, "n": st["n"], "median": st["median"],
                             "hit": st["hit"], "p25": st["p25"], "p75": st["p75"], "mean_trim": st["mean_trim"]})
    pd.DataFrame(rows).to_csv(f"{OUT}/edge_entries.csv", index=False)

    # Tail-Statistik (was bringt die Bewegung wirklich?)
    tails = []
    for name, mask in groups.items():
        d = bars.loc[ded[name]]
        for h in [10, 20, 40]:
            s = d["f%d" % h].dropna()
            if len(s) == 0:
                continue
            tails.append({"group": name, "h": h, "n": len(s),
                          "p_gt10": float(100 * (s > 10).mean()), "p_gt20": float(100 * (s > 20).mean()),
                          "p_gt50": float(100 * (s > 50).mean()),
                          "p_lt_10": float(100 * (s < -10).mean()), "p_lt_20": float(100 * (s < -20).mean()),
                          "median_mfe20": float(d["mfe20"].median()), "median_mae20": float(d["mae20"].median())})
    pd.DataFrame(tails).to_csv(f"{OUT}/edge_tails.csv", index=False)

    # Signifikanz der Differenz: Signal+RS vs. Baseline / vs. RS>85
    tests = []
    for label, mask_a, mask_b in [
        ("ww_bdoh_2|3 + RS  vs  alle Bars", sig23 & rs_ok, pd.Series(True, index=bars.index)),
        ("ww_bdoh_2|3 + RS  vs  RS>85", sig23 & rs_ok, rs_ok),
        ("ww_bdoh_2|3       vs  alle Bars", sig23, pd.Series(True, index=bars.index)),
        ("ww_bdoh_1 + RS    vs  RS>85", bars["sig1"] & rs_ok, rs_ok),
        ("ww_bdoh_2 + RS    vs  RS>85", bars["sig2"] & rs_ok, rs_ok),
        ("ww_bdoh_3 + RS    vs  RS>85", bars["sig3"] & rs_ok, rs_ok),
    ]:
        for h in [5, 10, 20, 40]:
            for entry, col in [("signal-close", "x%d" % h), ("tief-close", "px%d" % h)]:
                r = boot_diff_medians(bars, dedupe(bars, mask_a), mask_b, col)
                if r:
                    tests.append({"test": label, "entry": entry, "h": h, **r})
    pd.DataFrame(tests).to_csv(f"{OUT}/edge_significance.csv", index=False)

    # Jahresstabilitaet (dedupliziert)
    y = bars.loc[ded["ww_bdoh_2|3 + RS"], ["ts", "x20", "f20"]].dropna().copy()
    y["year"] = pd.to_datetime(y["ts"], unit="s").dt.year
    y.groupby("year").agg(n=("f20", "size"), median=("f20", "median"), rel_median=("x20", "median"),
                          hit=("f20", lambda s: 100.0 * (s > 0).mean())).to_csv(f"{OUT}/edge_yearly2.csv")
    log("done")


if __name__ == "__main__":
    main()
