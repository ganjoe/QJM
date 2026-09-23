#!/usr/bin/env python3
"""Phase 9 — Signal-Chart im Viewer: Preis + MADBO-Marker + Phoenix-Marker + RS-Schar.

Zeigt genau den Fall, der fuer die Chart-Suche zaehlt: Wann feuerte MADBO
(Kandidat) und wann Phoenix (Bestaetigung)?
"""
from __future__ import annotations

import json
import sys

import httpx
import numpy as np
import pandas as pd

from lab_bt import build_panel
from lab_common import CACHE_DIR, PARQUET_BASE

RS_DIR = CACHE_DIR / "lab_rs"
VIEWER = "http://host.docker.internal:8766/api/command"
COLORS = ["#00E5FF", "#FF6D00", "#76FF03", "#D500F9", "#FFD600", "#FF1744",
          "#00BFA5", "#F50057", "#651FFF", "#FF9100", "#1DE9B6", "#C6FF00",
          "#8D6E63", "#40C4FF", "#B2FF59", "#FF80AB"]


def main() -> None:
    import duckdb
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "MU").upper()
    n_bars = 700
    if "--bars" in sys.argv:
        n_bars = int(sys.argv[sys.argv.index("--bars") + 1])
    p = PARQUET_BASE / ticker / "1D_features.parquet"
    con = duckdb.connect()
    df = con.execute(f"SELECT timestamp//86400*86400 AS t, open, high, low, close, volume, ibd_rs "
                     f"FROM read_parquet('{p}') ORDER BY timestamp DESC LIMIT {n_bars}").df()
    con.close()
    df = df.sort_values("t").reset_index(drop=True)
    g = df["close"]
    sma = {w: g.rolling(w).mean() for w in (10, 20, 50, 100, 200)}
    spread = pd.concat(sma.values(), axis=1).max(axis=1) - pd.concat(sma.values(), axis=1).min(axis=1)
    prev = g.shift(1)
    tr = pd.concat([(df["high"] - df["low"]).abs(), (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    dv = g * df["volume"]; dv50 = dv.rolling(50).mean()
    df["madbo"] = (spread < tr) & (dv > 2 * dv50)
    rm = df["ibd_rs"].rolling(20, min_periods=1).min()
    m = (df["ibd_rs"] >= 80) & (rm <= 30)
    df["phx"] = m & ~m.shift(1, fill_value=False)
    df["date"] = pd.to_datetime(df["t"], unit="s").dt.strftime("%Y-%m-%d")

    print(f"{ticker}: {len(df)} Bars {df['date'].iloc[0]} .. {df['date'].iloc[-1]}")
    print("MADBO:", list(df[df["madbo"]]["date"]))
    print("Phoenix(ibd_rs 20/30/80):", list(df[df["phx"]]["date"]))

    bars = [{"t_open": int(r.t), "t_close": int(r.t) + 86400, "open": float(r.open),
             "high": float(r.high), "low": float(r.low), "close": float(r.close),
             "volume": float(r.volume or 0)} for r in df.itertuples()]

    anns = []
    for r in df[df["madbo"]].itertuples():
        anns.append({"id": f"madbo_{int(r.t)}", "type": "trade_marker",
                     "anchors": [{"t": int(r.t), "price": float(r.close), "mode": "data"}],
                     "style": {"color": "#FFB300", "width": 2, "action": "BUY", "text": "M"}, "persistent": True})
    for r in df[df["phx"]].itertuples():
        anns.append({"id": f"phx_{int(r.t)}", "type": "trade_marker",
                     "anchors": [{"t": int(r.t), "price": float(r.close), "mode": "data"}],
                     "style": {"color": "#00E676", "width": 3, "action": "BUY", "text": "PHX"}, "persistent": True})

    overlays = []
    variants = json.loads((RS_DIR / "variants.json").read_text()) if (RS_DIR / "variants.json").exists() else {}
    t0, t1 = int(df["t"].iloc[0]), int(df["t"].iloc[-1])
    names = ["ibd_rs_prod"] + list(variants)
    for i, name in enumerate(names):
        f = RS_DIR / f"{name}.parquet"
        if not f.exists():
            continue
        s = pd.read_parquet(f)
        if "d" in s.columns:
            s = s.set_index("d")
        if ticker not in s.columns:
            continue
        ser = s[ticker].dropna()
        ser = ser[(ser.index >= t0) & (ser.index <= t1)]
        if ser.empty:
            continue
        base = name == "ibd_rs_prod"
        overlays.append({"overlay_id": f"lab_rs_{name}", "type": "line",
                         "style": {"color": "#9E9E9E" if base else COLORS[i % len(COLORS)],
                                   "width": 1, "dash": base},
                         "values": [{"t": int(ts), "value": float(v)} for ts, v in ser.items()],
                         "pane": "lab_rs", "origin": "bottom"})

    payload = {"action": "OPEN_WINDOW", "window_id": f"win_signals_{ticker.lower()}",
               "symbol": f"{ticker} MADBO+PHX", "timeframe": {"unit": "D", "multiplier": 1},
               "bars": bars, "overlays": overlays, "annotations": anns,
               "position": {"x": 60, "y": 60}, "size": {"width": 1200, "height": 760}}
    r = httpx.post(VIEWER, json=payload, timeout=30)
    print("viewer:", r.status_code, r.text[:200])
    print(f"Marker: {len([a for a in anns if a['id'].startswith('madbo')])} MADBO, "
          f"{len([a for a in anns if a['id'].startswith('phx')])} PHX | RS-Kurven: {len(overlays)}")


if __name__ == "__main__":
    main()
