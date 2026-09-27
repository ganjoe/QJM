"""Edge-Check (robust): ww_bdoh_1/2/3 (Stochastik 10/1) + IBD-RS-Filter, High-Cap-1000.

Datenhygiene:
  * Zeitraum ab 2007 (ab dann ist ibd_rs flaechendeckend point-in-time verfuegbar)
  * nur Bars mit close >= 1 USD und volume > 0
  * Forward-Returns je Horizont auf 0.5/99.5-Perzentil winsorized (Split-Artefakte)
  * Kennzahlen robust: Median, Trefferquote, 5%-getrimmter Mittelwert, marktrelative Sicht
    (Forward-Return minus Tagesmittel des Universums) und ticker-geclusterter Bootstrap.

Aufruf: docker exec qjm-pca-service python3 /app/analysis_bdoh_edge.py
"""
import json
import os
import time

import duckdb
import numpy as np
import pandas as pd

from scanners import StochX20Scanner

PARQUET = "/parquet"
TICKER_FILE = "/app/_highcap1000.json"
OUT = "/app/_analysis_out"
START_TS = int(pd.Timestamp("2007-01-01", tz="UTC").timestamp())
HORIZONS = [5, 10, 20, 40, 60]
RS_LEVEL = 85
CHUNK = 150
N_BOOT = 2000

os.makedirs(OUT, exist_ok=True)
SCANNER = StochX20Scanner()
PARAMS = SCANNER._params(min_history_bars=252)


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def load_chunk(tickers):
    paths = [p for p in (f"{PARQUET}/{t}/1D_features.parquet" for t in tickers) if os.path.exists(p)]
    if not paths:
        return None
    listing = ", ".join("'" + p + "'" for p in paths)
    con = duckdb.connect()
    try:
        return con.execute(f"""
            select regexp_extract(filename, '([^/]+)/1D_features[.]parquet$', 1) as ticker,
                   timestamp, high, low, close, volume, ibd_rs
            from read_parquet([{listing}], filename=true)
            where close is not null and close >= 1 and volume is not null and volume > 0
            order by ticker, timestamp
        """).df()
    finally:
        con.close()


def ticker_frame(ticker, g):
    g = g.reset_index(drop=True)
    if len(g) < 60:
        return None
    st = SCANNER._state(g, PARAMS)
    close = g["close"].astype(float)
    high = g["high"].astype(float)
    low = g["low"].astype(float)

    out = pd.DataFrame({
        "ticker": ticker,
        "ts": g["timestamp"].astype("int64"),
        "close": close.astype("float32"),
        "k": st["k"].astype("float32"),
        "rs": pd.to_numeric(g["ibd_rs"], errors="coerce").astype("float32"),
        "sig1": st["sig_x20"].to_numpy(dtype=bool),
        "sig2": st["sig_20_80"].to_numpy(dtype=bool),
        "sig3": st["sig_20_80_ath"].to_numpy(dtype=bool),
    })
    for h in HORIZONS:
        out["f%d" % h] = ((close.shift(-h) / close - 1.0) * 100.0).astype("float32")
    hi252 = high.rolling(252, min_periods=20).max()
    out["from_52wh"] = ((close / hi252 - 1.0) * 100.0).astype("float32")
    fwd_low = low[::-1].rolling(20).min()[::-1].shift(-1)
    fwd_high = high[::-1].rolling(20).max()[::-1].shift(-1)
    out["mae20"] = ((fwd_low / close - 1.0) * 100.0).astype("float32")
    out["mfe20"] = ((fwd_high / close - 1.0) * 100.0).astype("float32")
    return out


def build_bars():
    tickers = json.load(open(TICKER_FILE))
    frames, done, t0 = [], 0, time.time()
    for i in range(0, len(tickers), CHUNK):
        raw = load_chunk(tickers[i:i + CHUNK])
        done += len(tickers[i:i + CHUNK])
        if raw is None:
            continue
        for ticker, g in raw.groupby("ticker", sort=False):
            fr = ticker_frame(ticker, g)
            if fr is not None:
                frames.append(fr)
        log("loaded %d/%d" % (done, len(tickers)))
    bars = pd.concat(frames, ignore_index=True)
    bars = bars[bars["ts"] >= START_TS].reset_index(drop=True)
    wins = {}
    for h in HORIZONS:
        col = "f%d" % h
        lo, hi = bars[col].quantile(0.005), bars[col].quantile(0.995)
        wins[col] = (float(lo), float(hi))
        bars[col] = bars[col].clip(lo, hi)
        bars["x%d" % h] = (bars[col] - bars.groupby("ts")[col].transform("mean")).astype("float32")
    log("bars=%d tickers=%d %s..%s" % (len(bars), bars["ticker"].nunique(),
        pd.to_datetime(bars["ts"].min(), unit="s").date(), pd.to_datetime(bars["ts"].max(), unit="s").date()))
    return bars, wins


def dedupe(bars, mask, min_gap=20):
    """Nur das erste Signal pro Ticker je 20 Handestage (gegen ueberlappende Fenster)."""
    keep = np.zeros(len(bars), dtype=bool)
    sub = bars.loc[mask, ["ticker"]].copy()
    sub["pos"] = np.flatnonzero(mask.to_numpy())
    for _, grp in sub.groupby("ticker", sort=False):
        last = -10 ** 9
        for pos in grp["pos"].to_numpy():
            if pos - last >= min_gap:
                keep[pos] = True
                last = pos
    return pd.Series(keep, index=bars.index)


def describe(x):
    x = pd.Series(x).dropna().to_numpy()
    n = len(x)
    if n == 0:
        return None
    s = np.sort(x)
    k = int(n * 0.05)
    trimmed = s[k:n - k].mean() if n - 2 * k > 5 else s.mean()
    return {"n": n, "median": float(np.median(s)), "mean_trim": float(trimmed),
            "hit": float(100.0 * (s > 0).mean()), "p25": float(np.percentile(s, 25)),
            "p75": float(np.percentile(s, 75))}


def boot_median(bars, mask, col, n_boot=N_BOOT, seed=11):
    d = bars.loc[mask, ["ticker", col]].dropna()
    if d.empty:
        return None
    codes, _ = pd.factorize(d["ticker"])
    vals = d[col].to_numpy(dtype="float64")
    order = np.argsort(codes, kind="stable")
    codes, vals = codes[order], vals[order]
    bounds = np.searchsorted(codes, np.arange(codes.max() + 2))
    ntick = codes.max() + 1
    if ntick < 5:
        return None
    rng = np.random.default_rng(seed)
    meds = []
    for _ in range(n_boot):
        pick = rng.integers(0, ntick, ntick)
        parts = [vals[bounds[p]:bounds[p + 1]] for p in pick]
        pool = np.concatenate(parts) if parts else np.array([])
        if len(pool):
            meds.append(np.median(pool))
    if not meds:
        return None
    return {"median": float(np.median(vals)), "lo": float(np.percentile(meds, 2.5)),
            "hi": float(np.percentile(meds, 97.5)), "p_le_0": float((np.array(meds) <= 0).mean())}


def main():
    bars, wins = build_bars()
    rs = bars["rs"]
    rs_ok = rs > RS_LEVEL
    base = pd.Series(True, index=bars.index)
    combos = {
        "alle Bars (Baseline)": base,
        "RS > %d" % RS_LEVEL: rs_ok,
        "ww_bdoh_1": bars["sig1"],
        "ww_bdoh_2": bars["sig2"],
        "ww_bdoh_3": bars["sig3"],
        "ww_bdoh_2|3": bars["sig2"] | bars["sig3"],
        "ww_bdoh_1 + RS": bars["sig1"] & rs_ok,
        "ww_bdoh_2 + RS": bars["sig2"] & rs_ok,
        "ww_bdoh_3 + RS": bars["sig3"] & rs_ok,
        "ww_bdoh_2|3 + RS": (bars["sig2"] | bars["sig3"]) & rs_ok,
    }
    dedup = {k: dedupe(bars, m) if k != "alle Bars (Baseline)" else m for k, m in combos.items()}

    rows = []
    for name, mask in combos.items():
        for h in HORIZONS:
            raw = describe(bars.loc[mask, "f%d" % h])
            rel = describe(bars.loc[mask, "x%d" % h])
            dd = describe(bars.loc[dedup[name], "x%d" % h])
            if raw is None:
                continue
            rows.append({"group": name, "h": h, "n": raw["n"], "median": raw["median"], "hit": raw["hit"],
                         "p25": raw["p25"], "p75": raw["p75"], "mean_trim": raw["mean_trim"],
                         "rel_median": rel["median"], "rel_hit": rel["hit"], "rel_mean_trim": rel["mean_trim"],
                         "dedup_n": dd["n"] if dd else 0,
                         "dedup_rel_median": dd["median"] if dd else np.nan,
                         "dedup_rel_hit": dd["hit"] if dd else np.nan})
    res = pd.DataFrame(rows)
    res.to_csv(f"{OUT}/edge_stats.csv", index=False)

    boot = {}
    for name in ["alle Bars (Baseline)", "RS > %d" % RS_LEVEL, "ww_bdoh_2", "ww_bdoh_3", "ww_bdoh_2|3",
                 "ww_bdoh_1 + RS", "ww_bdoh_2 + RS", "ww_bdoh_3 + RS", "ww_bdoh_2|3 + RS"]:
        boot[name] = {h: boot_median(bars, combos[name], "x%d" % h) for h in HORIZONS}
    json.dump(boot, open(f"{OUT}/edge_bootstrap.json", "w"), indent=1)

    sens = []
    base_mask = bars["sig2"] | bars["sig3"]
    for lvl in [0, 70, 75, 80, 85, 90, 95]:
        m = base_mask & (rs > lvl) if lvl else base_mask
        for h in [10, 20, 40]:
            d = describe(bars.loc[m, "x%d" % h])
            if d:
                sens.append({"rs_level": lvl, "h": h, "n": d["n"], "rel_median": d["median"],
                             "rel_hit": d["hit"], "rel_mean_trim": d["mean_trim"]})
    pd.DataFrame(sens).to_csv(f"{OUT}/edge_rs_sensitivity.csv", index=False)

    ctx = []
    for name, mask in combos.items():
        d = bars.loc[mask]
        ctx.append({"group": name, "n": int(mask.sum()), "median_from_52wh": float(d["from_52wh"].median()),
                    "median_mae20": float(d["mae20"].median()), "median_mfe20": float(d["mfe20"].median()),
                    "median_k": float(d["k"].median()), "median_rs": float(d["rs"].median()),
                    "median_close": float(d["close"].median())})
    pd.DataFrame(ctx).to_csv(f"{OUT}/edge_context.csv", index=False)

    y = bars.loc[dedup["ww_bdoh_2|3 + RS"], ["ts", "x20", "f20"]].dropna().copy()
    y["year"] = pd.to_datetime(y["ts"], unit="s").dt.year
    yearly = y.groupby("year").agg(n=("f20", "size"), median_raw=("f20", "median"),
                                   median_rel=("x20", "median"), hit=("f20", lambda s: 100.0 * (s > 0).mean()))
    yearly.to_csv(f"{OUT}/edge_yearly.csv")

    last_ts = int(bars["ts"].max())
    recent = bars[(bars["ts"] >= last_ts - 60 * 86400) & (bars["sig2"] | bars["sig3"])].copy()
    recent["date"] = pd.to_datetime(recent["ts"], unit="s").dt.strftime("%Y-%m-%d")
    recent[["ticker", "date", "sig2", "sig3", "k", "rs", "from_52wh", "f5", "f10", "f20"]].sort_values(
        ["date", "ticker"], ascending=[False, True]).to_csv(f"{OUT}/bdoh_recent_signals.csv", index=False)

    allsig = bars[bars["sig2"] | bars["sig3"]].copy()
    allsig["date"] = pd.to_datetime(allsig["ts"], unit="s").dt.strftime("%Y-%m-%d")
    allsig.to_csv(f"{OUT}/bdoh_all_signals.csv", index=False)

    json.dump({"bars": int(len(bars)), "tickers": int(bars["ticker"].nunique()),
               "first": str(pd.to_datetime(bars["ts"].min(), unit="s").date()),
               "last": str(pd.to_datetime(bars["ts"].max(), unit="s").date()),
               "winsor": wins, "counts": {k: int(v.sum()) for k, v in combos.items()},
               "counts_dedup": {k: int(v.sum()) for k, v in dedup.items()}},
              open(f"{OUT}/edge_meta.json", "w"), indent=1)
    log("done")


if __name__ == "__main__":
    main()
