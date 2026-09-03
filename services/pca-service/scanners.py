import os
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
import pandas as pd
import numpy as np
import duckdb
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("pca.scanners")
router = APIRouter()

PARQUET_BASE = Path(os.environ.get("PARQUET_BASE_PATH", "/parquet"))


# ─── Scanner Data Models ──────────────────────────────────────────────────────

class ScannerRunRequest(BaseModel):
    scanners: List[str] = Field(..., description="Names of scanners to run, e.g. ['minervini_trend', 'sma_cross']")
    tickers: List[str] = Field(..., description="List of ticker symbols to evaluate")
    timeframe: str = Field(default="1D", description="Timeframe (default 1D)")


class ScannerResult(BaseModel):
    ticker: str
    date: str
    timestamp: int
    matches: Dict[str, bool]
    scores: Dict[str, Optional[float]]
    details: Dict[str, Any]


# ─── Base Scanner Interface ───────────────────────────────────────────────────

class BaseScanner(ABC):
    name: str = ""
    description: str = ""

    @abstractmethod
    def evaluate(self, ticker: str, df: pd.DataFrame, feat_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        """
        Evaluates the scanner condition against the latest candle in the DataFrame.
        Returns:
            {
                "matched": bool,
                "score": Optional[float/int],
                "details": Dict[str, Any]
            }
        """
        pass


# ─── Built-In Scanners ────────────────────────────────────────────────────────

class MinerviniTrendScanner(BaseScanner):
    name = "minervini_trend"
    description = "Minervini Trend Template: 200 SMA trending up, Price > 150 & 200 SMA, 50 SMA > 150 & 200 SMA"

    def evaluate(self, ticker: str, df: pd.DataFrame, feat_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        if len(df) < 200:
            return {"matched": False, "score": 0, "details": {"error": "Not enough history (<200 bars)"}}

        close = df["close"]
        sma_50 = close.rolling(50).mean()
        sma_150 = close.rolling(150).mean()
        sma_200 = close.rolling(200).mean()

        c_last = float(close.iloc[-1])
        s50_last = float(sma_50.iloc[-1])
        s150_last = float(sma_150.iloc[-1])
        s200_last = float(sma_200.iloc[-1])
        s200_20d_ago = float(sma_200.iloc[-20]) if len(sma_200) >= 20 else s200_last

        # 52-week High/Low (assuming ~252 trading days)
        lookback = min(len(df), 252)
        high_52w = float(df["high"].iloc[-lookback:].max())
        low_52w = float(df["low"].iloc[-lookback:].min())

        cond1 = c_last > s150_last and c_last > s200_last
        cond2 = s150_last > s200_last
        cond3 = s200_last >= s200_20d_ago  # 200 SMA trending up
        cond4 = s50_last > s150_last and s50_last > s200_last
        cond5 = c_last >= (low_52w * 1.25)  # >= 25% above 52w low
        cond6 = c_last >= (high_52w * 0.75) # within 25% of 52w high

        criteria = [cond1, cond2, cond3, cond4, cond5, cond6]
        score = sum(1 for c in criteria if c)
        matched = (score >= 5)

        return {
            "matched": matched,
            "score": score,
            "details": {
                "score_max": 6,
                "price": round(c_last, 2),
                "sma_50": round(s50_last, 2),
                "sma_150": round(s150_last, 2),
                "sma_200": round(s200_last, 2),
                "high_52w": round(high_52w, 2),
                "low_52w": round(low_52w, 2),
                "criteria": {
                    "price_above_150_200": cond1,
                    "sma150_above_sma200": cond2,
                    "sma200_trending_up": cond3,
                    "sma50_above_150_200": cond4,
                    "above_25pct_52w_low": cond5,
                    "within_25pct_52w_high": cond6,
                }
            }
        }


class SmaCrossScanner(BaseScanner):
    name = "sma_cross"
    description = "Golden Cross / Spread between 50 SMA and 200 SMA"

    def evaluate(self, ticker: str, df: pd.DataFrame, feat_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        if len(df) < 200:
            return {"matched": False, "score": None, "details": {"error": "Not enough history (<200 bars)"}}

        close = df["close"]
        sma_50 = close.rolling(50).mean()
        sma_200 = close.rolling(200).mean()

        s50 = float(sma_50.iloc[-1])
        s200 = float(sma_200.iloc[-1])
        spread_pct = round(((s50 - s200) / s200) * 100, 2) if s200 != 0 else 0.0

        matched = (s50 > s200)
        return {
            "matched": matched,
            "score": spread_pct,
            "details": {
                "sma_50": round(s50, 2),
                "sma_200": round(s200, 2),
                "spread_pct": spread_pct,
                "golden_cross_active": matched,
            }
        }


# ─── Scanner Registry ─────────────────────────────────────────────────────────

class ScannerRegistry:
    def __init__(self):
        self._scanners: Dict[str, BaseScanner] = {}
        # Register defaults
        self.register(MinerviniTrendScanner())
        self.register(SmaCrossScanner())

    def register(self, scanner: BaseScanner):
        self._scanners[scanner.name.lower()] = scanner

    def get(self, name: str) -> Optional[BaseScanner]:
        return self._scanners.get(name.lower())

    def list_scanners(self) -> List[Dict[str, str]]:
        return [{"name": s.name, "description": s.description} for s in self._scanners.values()]


registry = ScannerRegistry()


def load_ticker_data_for_scan(ticker: str, timeframe: str = "1D") -> Optional[pd.DataFrame]:
    parquet_file = PARQUET_BASE / ticker.upper() / f"{timeframe.upper()}.parquet"
    if not parquet_file.exists():
        return None

    try:
        db = duckdb.connect()
        # Read up to 300 bars for scanner calculations
        query = f"SELECT timestamp, open, high, low, close, volume FROM read_parquet('{parquet_file}') ORDER BY timestamp DESC LIMIT 350"
        df = db.execute(query).df()
        db.close()
        if df.empty:
            return None
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        logger.error("Failed to load %s for scan: %s", ticker, e)
        return None


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/scanner/list")
async def list_available_scanners():
    return {"scanners": registry.list_scanners()}


@router.post("/scanner/run")
async def run_scanners_endpoint(req: ScannerRunRequest):
    selected_scanners: List[BaseScanner] = []
    for s_name in req.scanners:
        sc = registry.get(s_name)
        if not sc:
            raise HTTPException(status_code=400, detail=f"Scanner '{s_name}' not found. Available: {[s['name'] for s in registry.list_scanners()]}")
        selected_scanners.append(sc)

    results: List[Dict[str, Any]] = []

    for raw_ticker in req.tickers:
        ticker = raw_ticker.strip().upper()
        if not ticker:
            continue

        df = load_ticker_data_for_scan(ticker, req.timeframe)
        if df is None or df.empty:
            results.append({
                "ticker": ticker,
                "error": "No data found for ticker",
                "matches": {s.name: False for s in selected_scanners},
                "scores": {s.name: None for s in selected_scanners},
                "details": {}
            })
            continue

        last_row = df.iloc[-1]
        ts = last_row["timestamp"]
        ts_val = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts)
        date_str = datetime.fromtimestamp(ts_val, tz=timezone.utc).strftime("%Y-%m-%d")

        ticker_matches = {}
        ticker_scores = {}
        ticker_details = {}

        for sc in selected_scanners:
            res = sc.evaluate(ticker, df)
            ticker_matches[sc.name] = bool(res.get("matched", False))
            ticker_scores[sc.name] = res.get("score")
            ticker_details[sc.name] = res.get("details", {})

        results.append({
            "ticker": ticker,
            "date": date_str,
            "timestamp": ts_val,
            "matches": ticker_matches,
            "scores": ticker_scores,
            "details": ticker_details,
        })

    return {
        "status": "ok",
        "scanners": [s.name for s in selected_scanners],
        "count": len(results),
        "results": results,
    }
