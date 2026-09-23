#!/usr/bin/env python3
"""Phase 6 — Feinsuche um rs_to=90 + robuste Walk-Forward-Auswertung."""
from __future__ import annotations

import html
import json
import numpy as np
import pandas as pd

from lab_common import (BENCHMARK, CACHE_DIR, HORIZON_BARS, OUT_DIR, WARMUP_START,
                        load_close_series, write_parquet)

RS_DIR = CACHE_DIR / "lab_rs"
RS_DIR.mkdir(parents=True, exist_ok=True)
MIN_PARTICIPANTS = 300

FINE = [
    ("roc_63_126_252_w111",          (63, 126, 252),       (1, 1, 1)),
    ("roc_63_126_252_w112",          (63, 126, 252),       (1, 1, 2)),
    ("roc_63_126_252_w211",          (63, 126, 252),       (2, 1, 1)),
    ("roc_63_126_252_w122",          (63, 126, 252),       (1, 2, 2)),
    ("roc_63_126_252_w212",          (63, 126, 252),       (2, 1, 2)),
    ("roc_63_126_252_w113",          (63, 126, 252),       (1, 1, 3)),
    ("roc_63_126_252_w311",          (63, 126, 252),       (3, 1, 1)),
    ("roc_63_126_252_w123",          (63, 126, 252),       (1, 2, 3)),
    ("roc_50_100_200_w211",          (50, 100, 200),       (2, 1, 1)),
    ("roc_40_80_160_w211",           (40, 80, 160),        (2, 1, 1)),
    ("ibd_roc_63_126_189_252_w2111", (63, 126, 189, 252), (2, 1, 1, 1)),
    ("roc_63_126_189_252_w1111",     (63, 126, 189, 252), (1, 1, 1, 1)),
]
DAYS = (10, 20, 40)
FROM = (20, 30, 40)
TO = (85, 90, 95)


def pct_rank_rows(raw):
    rank = raw.rank(axis=1, na_option="keep")
    n = raw.notna().sum(axis=1)
    rating = rank.sub(1).div((n - 1).clip(lower=1), axis=0) * 98 + 1
    rating = rating.where(n > 1, 50.0).round().clip(1, 99)
    rating[n == 0] = np.nan
    return rating.astype("float32")


def build_panel(tickers, start):
    long = load_close_series(tickers, start=start)
    long = (long.dropna(subset=["open", "close"])
                .drop_duplicates(["ticker", "d"], keep="last")
                .sort_values(["ticker", "d"]).reset_index(drop=True))
    close = long["close"]
    long["_dv"] = close * long["volume"].fillna(0.0)
    g = long.groupby("ticker", sort=False)
    dv50 = g["_dv"].transform(lambda s: s.rolling(50, min_periods=1).mean())
    med21 = g["close"].transform(lambda s: s.rolling(21, min_periods=5).median())
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = close / med21
    bad = ((ratio > 5.0) | (ratio < 0.2)).fillna(False)
    bad_t = pd.DataFrame({"ticker": long["ticker"], "bad": bad}).groupby("ticker")["bad"].any()
    long["dq_ok"] = (close > 0.5) & (dv50 >= 1.0e5) & (~long["ticker"].map(bad_t))

    op = long.pivot_table(index="d", columns="ticker", values="open", aggfunc="last")
    cl = long.pivot_table(index="d", columns="ticker", values="close", aggfunc="last")
    counts = cl.notna().sum(axis=1)
    calendar = pd.Index(cl.index[counts >= MIN_PARTICIPANTS])
    op = op.reindex(index=calendar, columns=tickers)
    cl = cl.reindex(index=calendar, columns=tickers)
    dq = (long.pivot_table(index="d", columns="ticker", values="dq_ok", aggfunc="last")
              .reindex(index=calendar, columns=tickers).astype("boolean").fillna(False).astype(bool))

    spy = (load_close_series([BENCHMARK], start=start).dropna(subset=["open", "close"])
           .drop_duplicates(["d"]).sort_values("d").set_index("d"))
    so = spy["open"].reindex(calendar).to_numpy(float)
    sc = spy["close"].reindex(calendar).to_numpy(float)

    n = len(calendar)
    open_np, close_np = op.to_numpy(float), cl.to_numpy(float)
    entry = np.full_like(open_np, np.nan); exit_ = np.full_like(close_np, np.nan)
    se = np.full(n, np.nan); sx = np.full(n, np.nan)
    if n > HORIZON_BARS + 1:
        entry[: n - HORIZON_BARS] = open_np[1: n - HORIZON_BARS + 1]
        exit_[: n - HORIZON_BARS] = close_np[HORIZON_BARS: n]
        se[: n - HORIZON_BARS] = so[1: n - HORIZON_BARS + 1]
        sx[: n - HORIZON_BARS] = sc[HORIZON_BARS: n]
    with np.errstate(invalid="ignore", divide="ignore"):
        excess = (exit_ / entry - 1.0) - (sx / se - 1.0)[:, None]

    ratio = close_np / sc[:, None]
    ratio_entry = entry / se[:, None]
    min_rel = np.full_like(ratio, np.nan)
    rel_dd = np.full_like(ratio, np.nan)
    for t in range(n - HORIZON_BARS):
        w = ratio[t + 1: t + 1 + HORIZON_BARS]
        min_rel[t] = np.nanmin(w, axis=0) / ratio_entry[t] - 1.0
        rm = np.maximum.accumulate(w, axis=0)
        rel_dd[t] = np.nanmin(w / rm - 1.0, axis=0)

    years = pd.to_datetime(calendar, unit="s").year.to_numpy()
    return dict(long=long, calendar=calendar, years=years, dq=dq,
                excess=excess, min_rel=min_rel, rel_dd=rel_dd, n=n)


def wavg(vals, weights):
    vals = np.asarray(list(vals), float); weights = np.asarray(list(weights), float)
    m = np.isfinite(vals) & np.isfinite(weights) & (weights > 0)
    return float(np.sum(vals[m] * weights[m]) / np.sum(weights[m])) if m.any() else np.nan


def main() -> None:
    hc = pd.read_parquet(CACHE_DIR / "highcap1000.parquet")
    tickers = list(hc["ticker"])
    p = build_panel(tickers, WARMUP_START)
    cal, years, n = p["calendar"], p["years"], p["n"]
    print(f"Kalender: {len(cal)} Tage {pd.to_datetime(cal[0], unit='s').date()} .. {pd.to_datetime(cal[-1], unit='s').date()}")

    long = p["long"]
    comp_cache = {}

    def component(w):
        if w in comp_cache:
            return comp_cache[w]
        g = long.groupby("ticker", sort=False)["close"]
        comp = long["close"] / g.shift(w) - 1.0
        cw = (pd.DataFrame({"d": long["d"], "ticker": long["ticker"], "c": comp})
                .pivot_table(index="d", columns="ticker", values="c", aggfunc="last")
                .reindex(index=cal, columns=tickers))
        cw = cw.where(p["dq"])
        comp_cache[w] = cw
        return cw

    meta = {}; mats = {}
    for name, windows, weights in FINE:
        raw = pd.DataFrame(0.0, index=cal, columns=tickers)
        anyv = pd.DataFrame(False, index=cal, columns=tickers)
        for w, k in zip(windows, weights):
            c = component(w)
            raw = raw.add(c.fillna(0.0) * k); anyv = anyv | c.notna()
        rating = pct_rank_rows(raw.where(anyv))
        out = rating.copy(); out.insert(0, "d", out.index)
        out.to_parquet(RS_DIR / f"{name}.parquet", index=False)
        mats[name] = rating.to_numpy(float)
        meta[name] = {"metric": "roc", "windows": list(windows), "weights": list(weights), "combine": "v1"}
    ibd = (long.pivot_table(index="d", columns="ticker", values="ibd_rs", aggfunc="last")
               .reindex(index=cal, columns=tickers).to_numpy(float))
    mats["ibd_rs_prod"] = ibd
    (RS_DIR / "variants.json").write_text(json.dumps(meta, indent=2))

    horizon_ok = np.zeros(n, dtype=bool); horizon_ok[: n - HORIZON_BARS] = True
    signal_from = np.array(cal >= int(pd.Timestamp("2021-01-01").timestamp()))
    valid_t = horizon_ok & signal_from
    fold_years = sorted(set(years[valid_t]))

    rows = []
    for name, mat in mats.items():
        for days in DAYS:
            rm = pd.DataFrame(mat).rolling(days, min_periods=1).min().to_numpy()
            for rf in FROM:
                for rt in TO:
                    m = (mat >= rt) & (rm <= rf) & ~np.isnan(mat)
                    prev = np.zeros_like(m); prev[1:] = m[:-1]
                    ii, jj = np.where(m & ~prev & valid_t[:, None])
                    if len(ii) == 0:
                        continue
                    ex = p["excess"][ii, jj]
                    rec = {"series": name, "days": days, "rs_from": rf, "rs_to": rt,
                           "n": int(len(ex)), "hit": float(np.mean(ex >= 0)),
                           "mean": float(np.nanmean(ex)), "median": float(np.nanmedian(ex))}
                    for y in fold_years:
                        mask = years[ii] == y
                        rec[f"n_{y}"] = int(mask.sum())
                        rec[f"hit_{y}"] = float(np.mean(ex[mask] >= 0)) if mask.sum() else np.nan
                        rec[f"mean_{y}"] = float(np.nanmean(ex[mask])) if mask.sum() else np.nan
                    rows.append(rec)
    res = pd.DataFrame(rows).reset_index(drop=True)
    write_parquet(res, OUT_DIR / "fine_results.parquet")
    res.to_csv(OUT_DIR / "fine_results.csv", index=False)
    print(f"{len(res)} Feinsuche-Configs | {len(fold_years)} Jahre {fold_years}")

    # ── Walk-Forward: mehrere Regeln ──
    def prior(y):
        py = [yy for yy in fold_years if yy < y]
        tn = sum(res[f"n_{yy}"] for yy in py)
        num = sum(res[f"hit_{yy}"].fillna(0) * res[f"n_{yy}"] for yy in py)
        return tn, num / tn.replace(0, np.nan)

    def run_rule(min_n, min_hit, test_years):
        recs = []
        for y in test_years:
            tn, th = prior(y)
            ok = tn >= min_n
            if not bool(ok.any()):
                continue
            idx = th[ok].idxmax()
            if not np.isfinite(th[idx]) or th[idx] < min_hit:
                continue
            r = res.loc[idx]
            recs.append({"test_year": y, "series": r["series"], "days": int(r["days"]),
                         "rs_from": int(r["rs_from"]), "rs_to": int(r["rs_to"]),
                         "train_n": int(tn[idx]), "train_hit": float(th[idx]),
                         "oos_n": int(r[f"n_{y}"]), "oos_hit": r[f"hit_{y}"], "oos_mean": r[f"mean_{y}"]})
        return pd.DataFrame(recs)

    test_years = [y for y in fold_years if y >= 2022]
    rules = []
    for name, mn, mh in [("expanding_n100_h50", 100, 0.50), ("expanding_n300_h50", 300, 0.50),
                         ("expanding_n300_h52", 300, 0.52), ("expanding_n50_h50", 50, 0.50)]:
        g = run_rule(mn, mh, test_years)
        if g.empty:
            continue
        n_ = g["oos_n"].sum()
        rules.append({"rule": name, "years_traded": len(g), "n": int(n_),
                      "hit": float((g["oos_hit"] * g["oos_n"]).sum() / n_),
                      "mean": wavg(g["oos_mean"], g["oos_n"])})
    rules_df = pd.DataFrame(rules)
    rules_df.to_csv(OUT_DIR / "walkforward_rules.csv", index=False)

    # Fixed-Config: auf 2021-2023 waehlen, 2024-2026 messen (echtes OOS)
    def fixed_oos(series_filter=None, label=""):
        ty = [y for y in fold_years if y <= 2023]
        ey = [y for y in fold_years if y >= 2024]
        tn = sum(res[f"n_{y}"] for y in ty)
        num = sum(res[f"hit_{y}"].fillna(0) * res[f"n_{y}"] for y in ty)
        th = num / tn.replace(0, np.nan)
        pool = res[tn >= 100]
        if series_filter:
            pool = pool[pool["series"] == series_filter]
        if pool.empty:
            return None
        idx = th[pool.index].idxmax()
        r = res.loc[idx]
        n_ = sum(r[f"n_{y}"] for y in ey)
        wins = sum(r[f"hit_{y}"] * r[f"n_{y}"] for y in ey)
        mean = wavg([r[f"mean_{y}"] for y in ey], [r[f"n_{y}"] for y in ey])
        return {"label": label or r["series"], "series": r["series"], "days": int(r["days"]),
                "rs_from": int(r["rs_from"]), "rs_to": int(r["rs_to"]),
                "train_hit": float(th[idx]), "oos_n": int(n_), "oos_hit": float(wins / n_), "oos_mean": mean}

    fixed = [fixed_oos(None, "WF-fixed (best 2021-2023)"), fixed_oos("ibd_rs_prod", "ibd_rs_prod Baseline")]
    fixed = [f for f in fixed if f]
    fixed_df = pd.DataFrame(fixed)
    fixed_df.to_csv(OUT_DIR / "walkforward_fixed.csv", index=False)

    print("\nWalk-Forward-Regeln (OOS 2022-2026):")
    print(rules_df.to_string(index=False))
    print("\nFixed-Config OOS 2024-2026:")
    print(fixed_df.to_string(index=False))

    top = res[res["n"] >= 100].sort_values("hit", ascending=False).head(12)
    fold_cols = [f"hit_{y}" for y in fold_years]
    print("\nFold-Stabilitaet Top-Configs:")
    print(top[["series", "days", "rs_from", "rs_to", "n", "hit"] + fold_cols].to_string(index=False))

    def tbl(d, cols, fmt=None):
        fmt = fmt or {}
        th_ = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td>{html.escape(str(fmt.get(c, lambda x: x)(r[c])))}</td>" for c in cols) + "</tr>" for _, r in d.iterrows())
        return f"<table><tr>{th_}</tr>{body}</table>"

    f = {"hit": lambda x: f"{x:.3f}", "mean": lambda x: f"{x*100:+.1f}%", "mean_y": lambda x: f"{x*100:+.1f}%",
         "train_hit": lambda x: f"{x:.3f}", "oos_hit": lambda x: f"{x:.3f}", "oos_mean": lambda x: f"{x*100:+.1f}%",
         "train_n": lambda x: f"{int(x):,}", "n": lambda x: f"{int(x):,}"}
    css = "body{font:13px/1.5 ui-monospace,Menlo,monospace;background:#111;color:#ddd;margin:24px} table{border-collapse:collapse;margin:8px 0 20px} td,th{border:1px solid #333;padding:3px 7px;text-align:right} th{background:#1c1c1c;color:#9cf} td:first-child,th:first-child{text-align:left} h1,h2{color:#fff}"
    doc = ("<!doctype html><html><head><meta charset='utf-8'><title>RS-Lab Fein + Walk-Forward</title>"
           f"<style>{css}</style></head><body><h1>RS-Lab: Feinsuche to=90 + Walk-Forward</h1>"
           f"<p>Kalender {pd.to_datetime(cal[0], unit='s').date()}..{pd.to_datetime(cal[-1], unit='s').date()}, H={HORIZON_BARS}, "
           "Entry Folge-Open, Benchmark SPY, Datenqualitaets-Gate aktiv.</p>"
           "<h2>Walk-Forward-Regeln (OOS 2022-2026)</h2>" + tbl(rules_df, ["rule", "years_traded", "n", "hit", "mean"], f)
           + "<h2>Fixed-Config OOS 2024-2026 (Auswahl 2021-2023)</h2>"
           + tbl(fixed_df, ["label", "series", "days", "rs_from", "rs_to", "train_hit", "oos_n", "oos_hit", "oos_mean"], f)
           + "<h2>Fold-Stabilitaet: Top-Configs (Hit je Jahr)</h2>"
           + tbl(top, ["series", "days", "rs_from", "rs_to", "n", "hit"] + fold_cols, f)
           + "</body></html>")
    (OUT_DIR / "lab_fine_walkforward.html").write_text(doc, encoding="utf-8")
    print("\n-> output/lab_fine_walkforward.html")
    print("-> output/walkforward_rules.csv, walkforward_fixed.csv, fine_results.csv")


if __name__ == "__main__":
    main()
