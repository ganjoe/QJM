"""Teil 3: kausale Zerlegung -- findet der Scanner Tiefs, und ist der Sprung informativ?

Gruppen (alle ab 2007, High-Cap-1000, dedupliziert):
  * alle Bars / RS>85                                  -- Baselines
  * %K < 20 + RS>85                                    -- kausal kaufbar: ueberverkauft im Uptrend
  * %K < 20 + RS>85, am Folgetag ein bdoh-Sprung       -- Teilschnitt, aber Einstieg am Tief = Look-ahead
  * ww_bdoh_2|3 + RS>85                                -- Einstieg zum Close des Sprung-Balkens (realistisch)
Zusaetzlich: wie gross ist der Sprung-Balken selbst (close_t / close_t-1)?

Signifikanz: ticker-geclusterter Bootstrap (equal weight pro Ticker, 4000 Resamples),
Differenz der Ticker-Median-Mittelwerte gegen RS>85 bzw. alle Bars.
"""
import json
import numpy as np
import pandas as pd

from analysis_bdoh_edge import OUT, RS_LEVEL, build_bars, dedupe, describe, log

HORIZONS = [5, 10, 20, 40]


def ticker_med(df, mask, col):
    d = df.loc[mask, ["ticker", col]].dropna()
    if d.empty:
        return pd.Series(dtype=float)
    return d.groupby("ticker")[col].median()


def boot(a, b, n_boot=4000, seed=5):
    tick = sorted(set(a.index) | set(b.index))
    av = a.reindex(tick).to_numpy(dtype=float)
    bv = b.reindex(tick).to_numpy(dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(tick), size=(n_boot, len(tick)))
    A, B = av[idx], bv[idx]
    ca, cb = (~np.isnan(A)).sum(1), (~np.isnan(B)).sum(1)
    ma = np.where(ca > 0, np.nansum(A, 1) / np.maximum(ca, 1), np.nan)
    mb = np.where(cb > 0, np.nansum(B, 1) / np.maximum(cb, 1), np.nan)
    d = ma - mb
    d = d[~np.isnan(d)]
    if len(d) == 0:
        return None
    return {"diff": float(np.nanmean(av) - np.nanmean(bv)), "lo": float(np.percentile(d, 2.5)),
            "hi": float(np.percentile(d, 97.5)), "p_le_0": float((d <= 0).mean())}


def main():
    bars, wins = build_bars()
    bars["r1"] = (bars.groupby("ticker")["close"].pct_change() * 100.0).astype("float32")
    rs_ok = bars["rs"] > RS_LEVEL
    sig23 = bars["sig2"] | bars["sig3"]
    low = bars["k"] < 20.0
    # Folgebalken ist ein bdoh-Sprung?
    nxt = sig23.groupby(bars["ticker"]).shift(-1).fillna(False).astype(bool)

    groups = {
        "alle Bars": pd.Series(True, index=bars.index),
        "RS>85": rs_ok,
        "RS>85 (60d)": bars.groupby("ticker")["rs"].transform(lambda s: s.rolling(60, min_periods=1).max()) > RS_LEVEL,
        "%K<20": low,
        "%K<20 + RS": low & rs_ok,
        "%K<20 + RS, Folgetag Sprung (Look-ahead)": low & rs_ok & nxt,
        "ww_bdoh_2|3": sig23,
        "ww_bdoh_2|3 + RS": sig23 & rs_ok,
        "ww_bdoh_1 + RS": bars["sig1"] & rs_ok,
        "ww_bdoh_2 + RS": bars["sig2"] & rs_ok,
        "ww_bdoh_3 + RS": bars["sig3"] & rs_ok,
    }

    rows = []
    for name, mask in groups.items():
        m = dedupe(bars, mask) if name != "alle Bars" else mask
        tm = {h: ticker_med(bars, m, "x%d" % h) for h in HORIZONS}
        for h in HORIZONS:
            st = describe(bars.loc[m, "x%d" % h])
            raw = describe(bars.loc[m, "f%d" % h])
            if st is None:
                continue
            bl = {k: boot(tm[h], ticker_med(bars, bm, "x%d" % h), seed=5)
                  for k, bm in (("vs alle Bars", groups["alle Bars"]), ("vs RS>85", rs_ok))}
            rows.append({
                "group": name, "h": h, "n": st["n"], "pooled_rel_median": st["median"],
                "ticker_eq_rel_median": float(np.nanmean(tm[h].to_numpy(dtype=float))) if len(tm[h]) else np.nan,
                "rel_hit": st["hit"], "raw_median": raw["median"],
                "d_all": bl["vs alle Bars"]["diff"] if bl["vs alle Bars"] else np.nan,
                "d_all_lo": bl["vs alle Bars"]["lo"] if bl["vs alle Bars"] else np.nan,
                "d_all_hi": bl["vs alle Bars"]["hi"] if bl["vs alle Bars"] else np.nan,
                "d_rs": bl["vs RS>85"]["diff"] if bl["vs RS>85"] else np.nan,
                "d_rs_lo": bl["vs RS>85"]["lo"] if bl["vs RS>85"] else np.nan,
                "d_rs_hi": bl["vs RS>85"]["hi"] if bl["vs RS>85"] else np.nan,
            })
    pd.DataFrame(rows).to_csv(f"{OUT}/edge_causal.csv", index=False)

    # Wie gross ist der Sprung-Balken selbst?
    jump = []
    for name, mask in [("ww_bdoh_2|3", sig23), ("ww_bdoh_2|3 + RS", sig23 & rs_ok),
                       ("ww_bdoh_1 + RS", bars["sig1"] & rs_ok), ("%K<20 + RS", low & rs_ok)]:
        m = dedupe(bars, mask)
        r = bars.loc[m, "r1"].dropna()
        jump.append({"group": name, "n": len(r), "median_bar_return": float(r.median()),
                     "mean_bar_return": float(r.mean()), "share_gt5pct": float(100 * (r > 5).mean()),
                     "share_gt10pct": float(100 * (r > 10).mean())})
    pd.DataFrame(jump).to_csv(f"{OUT}/edge_jumpbar.csv", index=False)
    log("done part 3")


if __name__ == "__main__":
    main()
