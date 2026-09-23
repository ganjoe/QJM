#!/usr/bin/env python3
"""Phase 1 — High-Cap-1000 (USD) auf Basis von shares_outstanding x letzter Close.

Schreibt:
  cache/highcap1000.parquet   (rank, ticker, close, shares, market_cap, d)
  output/highcap1000.csv
  Supabase-Watchlist 'lab_highcap1000'
"""
from __future__ import annotations

import pandas as pd

from lab_common import (CACHE_DIR, HIGHCAP_N, WATCHLIST, load_last_close,
                        load_universe_meta, supabase_upsert, write_parquet)


def main() -> None:
    meta = load_universe_meta()
    print(f"Master-Universe (has_parquet & shares_outstanding): {len(meta):,}")

    meta = [m for m in meta
            if (m.get("currency") or "").upper() == "USD" and m.get("shares_outstanding")]
    shares = {m["ticker"]: float(m["shares_outstanding"]) for m in meta}
    tickers = sorted(shares)
    print(f"USD-Ticker mit Shares: {len(tickers):,}")

    last = load_last_close(tickers)
    print(f"letzte Closes geladen: {len(last):,}")

    last["shares"] = last["ticker"].map(shares)
    last["market_cap"] = last["close"].astype(float) * last["shares"].astype(float)
    last = (last.dropna(subset=["market_cap"])
                .query("market_cap > 0")
                .sort_values("market_cap", ascending=False)
                .reset_index(drop=True))
    hc = last.head(HIGHCAP_N).copy()
    hc["rank"] = range(1, len(hc) + 1)
    hc["d"] = pd.to_datetime(hc["d"], unit="s", utc=True)

    write_parquet(hc[["rank", "ticker", "close", "shares", "market_cap", "d"]],
                  CACHE_DIR / "highcap1000.parquet")
    hc[["rank", "ticker", "market_cap", "close"]].to_csv(CACHE_DIR / "highcap1000.csv", index=False)

    rows = [{"list_name": WATCHLIST, "ticker": t, "position": int(r)}
            for t, r in zip(hc["ticker"], hc["rank"])]
    n = supabase_upsert("pca_watchlists", rows, on_conflict="list_name,ticker")
    print(f"Watchlist '{WATCHLIST}' upserted: {n} Ticker")

    show = hc.head(30).copy()
    show["market_cap_B"] = (show["market_cap"] / 1e9).round(1)
    show["close"] = show["close"].round(2)
    print("\nTop 30 nach Market Cap (USD):")
    print(show[["rank", "ticker", "market_cap_B", "close"]].to_string(index=False))
    print(f"\nMarket-Cap-Range: {hc['market_cap'].iloc[0]/1e9:.1f}B (1) .. {hc['market_cap'].iloc[-1]/1e9:.2f}B (#{len(hc)})")


if __name__ == "__main__":
    main()
