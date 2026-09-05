import os
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
import numpy as np
import pandas as pd
import duckdb
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("pca.indicators")
router = APIRouter()

PARQUET_BASE = Path(os.environ.get("PARQUET_BASE_PATH", "/parquet"))
DEFAULT_CANDLE_LIMIT = int(os.environ.get("DEFAULT_CANDLE_LIMIT", "200"))
MAX_CANDLE_LIMIT = int(os.environ.get("MAX_CANDLE_LIMIT", "2000"))


class IndicatorRequest(BaseModel):
    symbol: str
    timeframe: str = "1D"
    limit: int = Field(default=DEFAULT_CANDLE_LIMIT, ge=1, le=MAX_CANDLE_LIMIT)
    source: str = Field(default="close", description="Column to calculate on: close, open, high, low, volume")
    indicator_type: str = Field(..., description="SMA, EMA, BOLLINGER, or STOCHASTIC")
    period: Optional[int] = Field(default=None, description="Single lookback period (e.g. 20)")
    periods: Optional[List[int]] = Field(default=None, description="Multiple lookback periods for batch calculation (e.g. [10, 20, 50, 200])")
    std_dev: Optional[float] = Field(default=2.0, description="Standard deviation multiplier for Bollinger Bands")
    k_period: Optional[int] = Field(default=None, description="Stochastic %K lookback period (e.g. 14)")
    d_period: Optional[int] = Field(default=None, description="Stochastic %D smoothing period (e.g. 3)")
    slowing: Optional[int] = Field(default=None, description="Stochastic %K slowing period (e.g. 3)")


def load_raw_ohlcv(symbol: str, timeframe: str = "1D", limit: int = DEFAULT_CANDLE_LIMIT) -> pd.DataFrame:
    parquet_file = PARQUET_BASE / symbol.upper() / f"{timeframe.upper()}.parquet"
    if not parquet_file.exists():
        raise HTTPException(status_code=404, detail=f"Parquet data for {symbol} ({timeframe}) not found at {parquet_file}")

    db = duckdb.connect()
    # Read extra lookback so rolling averages on early candles have valid history
    buffer_limit = min(limit + 250, MAX_CANDLE_LIMIT + 250)
    query = f"SELECT timestamp, open, high, low, close, volume FROM read_parquet('{parquet_file}') ORDER BY timestamp DESC LIMIT {buffer_limit}"
    df = db.execute(query).df()
    db.close()

    if df.empty:
        raise HTTPException(status_code=404, detail=f"No data found in {parquet_file}")

    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def calculate_sma(df: pd.DataFrame, source: str, periods: List[int]) -> Dict[str, List[Optional[float]]]:
    result = {}
    for p in periods:
        if p <= 0:
            continue
        col_name = f"sma_{p}"
        series = df[source].rolling(window=p).mean()
        result[col_name] = [None if np.isnan(v) else round(float(v), 4) for v in series]
    return result


def calculate_ema(df: pd.DataFrame, source: str, periods: List[int]) -> Dict[str, List[Optional[float]]]:
    result = {}
    for p in periods:
        if p <= 0:
            continue
        col_name = f"ema_{p}"
        series = df[source].ewm(span=p, adjust=False).mean()
        result[col_name] = [None if np.isnan(v) else round(float(v), 4) for v in series]
    return result


def calculate_bollinger(df: pd.DataFrame, source: str, periods: List[int], std_dev: float) -> Dict[str, List[Optional[float]]]:
    result = {}
    for p in periods:
        if p <= 0:
            continue
        avg = df[source].rolling(window=p).mean()
        std = df[source].rolling(window=p).std()
        upper = avg + (std * std_dev)
        lower = avg - (std * std_dev)
        bandwidth = np.where(avg != 0, ((upper - lower) / avg) * 100, 0.0)

        result[f"bb_{p}_avg"] = [None if np.isnan(v) else round(float(v), 4) for v in avg]
        result[f"bb_{p}_upper"] = [None if np.isnan(v) else round(float(v), 4) for v in upper]
        result[f"bb_{p}_lower"] = [None if np.isnan(v) else round(float(v), 4) for v in lower]
        result[f"bb_{p}_bandwidth"] = [None if np.isnan(v) else round(float(v), 4) for v in bandwidth]
    return result


def calculate_adr_pct(df: pd.DataFrame, periods: List[int]) -> Dict[str, List[Optional[float]]]:
    """Calculate Average Daily Range percentage (ADR%).
    ADR% = SMA of (High / Low - 1) * 100
    If period=1, it is just the daily range percentage.
    """
    # Daily range percentage
    dr_pct = (df["high"] / df["low"] - 1.0) * 100.0
    series_dict = {}
    for p in periods:
        if p == 1:
            res = dr_pct
        else:
            res = dr_pct.rolling(window=p).mean()
        series_dict[f"adr_{p}_pct"] = [None if np.isnan(v) else round(float(v), 4) for v in res]
    return series_dict


def calculate_stochastic(df: pd.DataFrame, k_period: int, d_period: int, slowing: int) -> Dict[str, List[Optional[float]]]:
    low_min = df["low"].rolling(window=k_period).min()
    high_max = df["high"].rolling(window=k_period).max()

    denom = high_max - low_min
    raw_k = np.where(denom != 0, 100 * ((df["close"] - low_min) / denom), 50.0)
    raw_k_series = pd.Series(raw_k)

    # Slowing
    if slowing > 1:
        slow_k = raw_k_series.rolling(window=slowing).mean()
    else:
        slow_k = raw_k_series

    # %D line
    slow_d = slow_k.rolling(window=d_period).mean()

    return {
        f"stoch_k_{k_period}_{slowing}": [None if np.isnan(v) else round(float(v), 4) for v in slow_k],
        f"stoch_d_{d_period}": [None if np.isnan(v) else round(float(v), 4) for v in slow_d],
    }


@router.post("/indicators/calculate")
async def calculate_indicator_endpoint(req: IndicatorRequest):
    sym = req.symbol.upper()
    tf = req.timeframe.upper()
    src = req.source.lower()
    ind_type = req.indicator_type.upper()

    # Determine periods
    periods = []
    if req.periods:
        periods = [int(p) for p in req.periods if int(p) > 0]
    elif req.period:
        periods = [int(req.period)]

    df = load_raw_ohlcv(sym, tf, req.limit)
    if src not in df.columns:
        raise HTTPException(status_code=400, detail=f"Source column '{src}' not found in OHLCV data. Valid: {list(df.columns)}")

    series_dict = {}

    if ind_type == "SMA":
        if not periods:
            raise HTTPException(status_code=400, detail="Für SMA muss mindestens eine Periode (period oder periods) angegeben werden.")
        series_dict = calculate_sma(df, src, periods)

    elif ind_type == "EMA":
        if not periods:
            raise HTTPException(status_code=400, detail="Für EMA muss mindestens eine Periode (period oder periods) angegeben werden.")
        series_dict = calculate_ema(df, src, periods)

    elif ind_type == "BOLLINGER":
        if not periods:
            raise HTTPException(status_code=400, detail="Für BOLLINGER muss mindestens eine Periode (period oder periods) angegeben werden.")
        std = req.std_dev if req.std_dev is not None else 2.0
        series_dict = calculate_bollinger(df, src, periods, std)

    elif ind_type == "ADR_PCT":
        if not periods:
            raise HTTPException(status_code=400, detail="Für ADR_PCT muss mindestens eine Periode (period oder periods) angegeben werden.")
        series_dict = calculate_adr_pct(df, periods)

    elif ind_type == "STOCHASTIC":
        k_p = req.k_period or 14
        d_p = req.d_period or 3
        sl = req.slowing or 3
        series_dict = calculate_stochastic(df, k_p, d_p, sl)

    else:
        raise HTTPException(status_code=400, detail=f"Unbekannter indicator_type '{ind_type}'. Gültig: SMA, EMA, BOLLINGER, STOCHASTIC.")

    # Slice to requested limit (from the end)
    req_limit = min(req.limit, len(df))
    trimmed_df = df.iloc[-req_limit:].reset_index(drop=True)
    timestamps = [int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts) for ts in trimmed_df["timestamp"]]

    trimmed_series = {}
    latest_values = {}
    for col_name, val_list in series_dict.items():
        sliced = val_list[-req_limit:]
        trimmed_series[col_name] = sliced
        latest_values[col_name] = sliced[-1] if len(sliced) > 0 else None

    return {
        "symbol": sym,
        "timeframe": tf,
        "source": src,
        "indicator_type": ind_type,
        "count": len(timestamps),
        "timestamps": timestamps,
        "series": trimmed_series,
        "latest_values": latest_values,
    }
