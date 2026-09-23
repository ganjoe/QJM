#!/usr/bin/env python3
"""Phase 11 — Lab-RS vorberechnen und als 1D_lab_rs.parquet je Highcap-Ticker ablegen.

Schreibt zuerst in cache/lab_parquet/<TICKER>/. Das Kopieren in den Parquet-Basis-
Ordner macht der Aufrufer (Host), damit die Produktion nur additiv beruehrt wird.
Es werden die EXAKTEN Timestamps der 1D_features.parquet uebernommen.
"""
from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from lab_common import CACHE_DIR, feature_path

RS_DIR = CACHE_DIR / "lab_rs"
STAGE = CACHE_DIR / "lab_parquet"

# Zielspaltenname -> Lab-Datei
VARIANTS = [
    ("lab_rs",      "roc_63_126_252_w122"),
    ("lab_rs_112",  "roc_63_126_252_w112"),
    ("lab_rs_123",  "roc_63_126_252_w123"),
    ("lab_rs_1111", "roc_63_126_189_252_w1111"),
    ("lab_rs_212",  "roc_63_126_252_w212"),
    ("lab_rs_20_40_80",   "lab_rs_20_40_80"),
    ("lab_rs_30_60_120",  "lab_rs_30_60_120"),
    ("lab_rs_40_80_160",  "lab_rs_40_80_160"),
    ("lab_rs_50_100_200", "lab_rs_50_100_200"),
    ("lab_rs_n",             "lab_rs_n"),
    ("lab_rs_n_20_40_80",    "lab_rs_n_20_40_80"),
    ("lab_rs_n_30_60_120",   "lab_rs_n_30_60_120"),
    ("lab_rs_n_50_100_200",  "lab_rs_n_50_100_200"),
]


def main() -> None:
    hc = pd.read_parquet(CACHE_DIR / "highcap1000.parquet")
    tickers = list(hc["ticker"])

    mats = {}
    for col, fname in VARIANTS:
        df = pd.read_parquet(RS_DIR / f"{fname}.parquet")
        if "d" in df.columns:
            df = df.set_index("d")
        mats[col] = df.reindex(columns=tickers)
    print("Lab-Matrizen:", {k: v.shape for k, v in mats.items()})

    paths = [str(feature_path(t)) for t in tickers if feature_path(t).exists()]
    fl = "[" + ",".join("'" + p + "'" for p in paths) + "]"
    con = duckdb.connect()
    ts = con.execute(
        "SELECT timestamp AS ts, timestamp//86400*86400 AS d, "
        "regexp_extract(filename, '([^/]+)/[^/]+$', 1) AS ticker "
        f"FROM read_parquet({fl}, filename=true)"
    ).df()
    con.close()
    ts["d"] = ts["d"].astype("int64")
    print("Bar-Zeilen:", len(ts), "| Ticker:", ts["ticker"].nunique())

    idx = pd.MultiIndex.from_arrays([ts["d"].to_numpy(), ts["ticker"].to_numpy()])
    for col, _ in VARIANTS:
        lookup = mats[col].stack(dropna=True)
        lookup.index = lookup.index.set_names(["d", "ticker"])
        ts[col] = lookup.reindex(idx).to_numpy()
        print(f"  {col}: {int(ts[col].notna().sum()):,} Werte zugeordnet")

    STAGE.mkdir(parents=True, exist_ok=True)
    cols = [c for c, _ in VARIANTS]
    written = 0
    for t, g in ts.groupby("ticker"):
        keep = g[["ts"] + cols].dropna(subset=cols, how="all").sort_values("ts")
        if keep.empty:
            continue
        out = keep.rename(columns={"ts": "timestamp"})
        d = STAGE / t
        d.mkdir(parents=True, exist_ok=True)
        out.to_parquet(d / "1D_lab_rs.parquet", index=False)
        written += 1
    print(f"geschrieben: {written} Dateien -> {STAGE}")


if __name__ == "__main__":
    main()
