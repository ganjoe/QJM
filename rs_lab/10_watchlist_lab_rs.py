#!/usr/bin/env python3
"""Phase 10 — Highcap-1000 als Watchlist im Viewer, inkl. Lab-RS-Spalten."""
from __future__ import annotations

import sys
import httpx
import numpy as np
import pandas as pd

from lab_common import CACHE_DIR

RS_DIR = CACHE_DIR / "lab_rs"
VIEWER = "http://host.docker.internal:8766/api/command"

# Spaltenname -> Lab-Datei
COLS = [
    ("RS_prod",  "ibd_rs_prod"),
    ("RS_lab",   "roc_63_126_252_w122"),
    ("RS_112",   "roc_63_126_252_w112"),
    ("RS_123",   "roc_63_126_252_w123"),
    ("RS_1111",  "roc_63_126_189_252_w1111"),
    ("RS_212",   "roc_63_126_252_w212"),
]


def main() -> None:
    hc = pd.read_parquet(CACHE_DIR / "highcap1000.parquet")
    tickers = list(hc["ticker"])
    rank = dict(zip(hc["ticker"], hc["rank"]))

    last = {}
    for col, fname in COLS:
        f = RS_DIR / f"{fname}.parquet"
        if not f.exists():
            print(f"  (fehlt: {fname})")
            continue
        df = pd.read_parquet(f)
        if "d" in df.columns:
            df = df.set_index("d")
        last[col] = df.reindex(columns=tickers).iloc[-1]

    rows = []
    for t in tickers:
        cells = {"Symbol": t, "Rank": int(rank[t])}
        for col, _ in COLS:
            if col not in last:
                continue
            v = last[col].get(t, np.nan)
            cells[col] = "" if pd.isna(v) else int(v)
        prod, lab = last.get("RS_prod", pd.Series()).get(t, np.nan), last.get("RS_lab", pd.Series()).get(t, np.nan)
        cells["Delta"] = "" if (pd.isna(prod) or pd.isna(lab)) else int(lab - prod)
        rows.append((lab, {"symbol": t, "cells": cells}))
    rows.sort(key=lambda x: (-999 if pd.isna(x[0]) else x[0]), reverse=True)
    payload_rows = [r for _, r in rows]
    columns = ["Symbol", "Rank", "RS_prod", "RS_lab", "RS_112", "RS_123", "RS_1111", "RS_212", "Delta"]

    payload = {"action": "OPEN_WATCHLIST", "list_id": "lab_highcap_rs",
               "display_name": "HighCap 1000 + Lab RS", "columns": columns,
               "rows": payload_rows, "color_flag": 0, "sort_column": "RS_lab",
               "sort_ascending": False, "position": {"x": 80, "y": 60},
               "size": {"width": 820, "height": 900}}
    r = httpx.post(VIEWER, json=payload, timeout=60)
    print("viewer:", r.status_code, r.text[:200])
    print(f"Watchlist 'lab_highcap_rs': {len(payload_rows)} Zeilen, {len(columns)} Spalten")

    top = pd.DataFrame([{**{"Ticker": rr["cells"]["Symbol"], "Rank": rr["cells"]["Rank"],
                            "RS_prod": rr["cells"].get("RS_prod"), "RS_lab": rr["cells"].get("RS_lab"),
                            "RS_1111": rr["cells"].get("RS_1111"), "Delta": rr["cells"].get("Delta")}}
                        for _, rr in rows[:30]])
    print("\nTop 30 nach Lab-RS (roc_63_126_252_w122):")
    print(top.to_string(index=False))


if __name__ == "__main__":
    main()
