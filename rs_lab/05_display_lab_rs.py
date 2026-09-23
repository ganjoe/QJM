#!/usr/bin/env python3
"""Phase 5 — Lab-RS als Overlay-Schar im vorhandenen Chart Viewer (RAM-only).

Pusht direkt an den Viewer-Server (POST /api/command, OPEN_WINDOW) eine
Preis-Serie plus alle Lab-RS-Varianten in einer eigenen Pane 'lab_rs'.
Keine Persistenz, kein Produktionscode.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from lab_common import CACHE_DIR, PARQUET_BASE

RS_DIR = CACHE_DIR / "lab_rs"
VIEWER = "http://host.docker.internal:8766/api/command"
COLORS = ["#00E5FF", "#FF6D00", "#76FF03", "#D500F9", "#FFD600", "#FF1744",
          "#00BFA5", "#F50057", "#651FFF", "#FF9100", "#1DE9B6", "#C6FF00",
          "#8D6E63", "#40C4FF", "#B2FF59", "#FF80AB"]


def load_bars(ticker: str, n: int) -> pd.DataFrame:
    import duckdb
    p = PARQUET_BASE / ticker.upper() / "1D_features.parquet"
    if not p.exists():
        raise SystemExit(f"keine Parquet-Daten fuer {ticker}")
    con = duckdb.connect()
    df = con.execute(
        f"SELECT timestamp//86400*86400 AS t, open, high, low, close, volume "
        f"FROM read_parquet('{p}') ORDER BY timestamp DESC LIMIT {int(n)}"
    ).df()
    con.close()
    return df.sort_values("t").reset_index(drop=True)


def main() -> None:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "CRM").upper()
    n_bars = 400
    if "--bars" in sys.argv:
        n_bars = int(sys.argv[sys.argv.index("--bars") + 1])
    want = None
    if "--variants" in sys.argv:
        want = set(sys.argv[sys.argv.index("--variants") + 1].split(","))

    bars = load_bars(ticker, n_bars)
    print(f"{ticker}: {len(bars)} Bars {pd.to_datetime(bars['t'].iloc[0], unit='s').date()} .. "
          f"{pd.to_datetime(bars['t'].iloc[-1], unit='s').date()}")

    variants = json.loads((RS_DIR / "variants.json").read_text())
    names = ["ibd_rs_prod"] + list(variants)
    if want:
        names = [x for x in names if x in want or x == "ibd_rs_prod"]

    t0, t1 = int(bars["t"].iloc[0]), int(bars["t"].iloc[-1])
    overlays = []
    latest = {}
    for i, name in enumerate(names):
        f = RS_DIR / f"{name}.parquet"
        if not f.exists():
            continue
        df = pd.read_parquet(f)
        if "d" in df.columns:
            df = df.set_index("d")
        if ticker not in df.columns:
            continue
        s = df[ticker].dropna()
        s = s[(s.index >= t0) & (s.index <= t1)]
        if s.empty:
            continue
        pts = [{"t": int(ts), "value": float(v)} for ts, v in s.items()]
        is_base = name == "ibd_rs_prod"
        overlays.append({
            "overlay_id": f"lab_rs_{name}",
            "type": "line",
            "style": {"color": "#9E9E9E" if is_base else COLORS[i % len(COLORS)],
                      "width": 1, "dash": is_base},
            "values": pts,
            "pane": "lab_rs",
            "origin": "bottom",
        })
        latest[name] = float(s.iloc[-1])

    bars_payload = [{"t_open": int(r.t), "t_close": int(r.t) + 86400,
                     "open": float(r.open), "high": float(r.high), "low": float(r.low),
                     "close": float(r.close), "volume": float(r.volume or 0)}
                    for r in bars.itertuples()]
    win = f"win_lab_rs_{ticker.lower()}"
    payload = {"action": "OPEN_WINDOW", "window_id": win,
               "symbol": f"{ticker} + lab_rs", "timeframe": {"unit": "D", "multiplier": 1},
               "bars": bars_payload, "overlays": overlays, "annotations": [],
               "position": {"x": 60, "y": 60}, "size": {"width": 1180, "height": 720}}
    r = httpx.post(VIEWER, json=payload, timeout=30)
    print("viewer:", r.status_code, r.text[:300])
    print(f"Schar: {len(overlays)} Kurven in Pane 'lab_rs' fuer {ticker}")
    print("aktuellste Werte:", {k: round(v, 1) for k, v in latest.items()})


if __name__ == "__main__":
    main()
