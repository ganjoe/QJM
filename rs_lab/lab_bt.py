"""Gemeinsame Panel-/Forward-Helfer fuer das RS-Lab (Testlab)."""
from __future__ import annotations
import numpy as np
import pandas as pd
from lab_common import BENCHMARK, HORIZON_BARS, load_close_series

MIN_PARTICIPANTS = 300


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
              .reindex(index=calendar, columns=tickers).fillna(False).astype(bool))

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
    return dict(long=long, calendar=calendar, years=years, cl=cl, dq=dq, excess=excess,
                min_rel=min_rel, rel_dd=rel_dd, n=n, op=op)
