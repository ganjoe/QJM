import os
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Union

from chart_data import normalize_ts
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
DAYS_BACK_MAX_HISTORY = int(os.environ.get("DAYS_BACK_MAX_HISTORY", "2500"))


class IndicatorRequest(BaseModel):
    symbol: Optional[str] = Field(default=None, description="Stock ticker symbol (e.g. 'AAPL', '$STATS.MARKET_BREADTH')")
    values: Optional[List[float]] = Field(default=None, description="Direct array of numbers to calculate indicator on (e.g. custom series or live data)")
    timestamps: Optional[List[int]] = Field(default=None, description="Optional timestamps corresponding to values")
    timeframe: str = "1D"
    limit: int = Field(default=DEFAULT_CANDLE_LIMIT, ge=1, le=MAX_CANDLE_LIMIT)
    source: str = Field(default="close", description="Column to calculate on: close, open, high, low, volume, or any feature column like breadth_40_pct")
    # Im Batch-Modus nicht noetig: dort traegt jeder Request seinen eigenen Typ.
    # Der Altpfad (ohne requests) prueft weiterhin, dass er gesetzt ist.
    indicator_type: Optional[str] = Field(default=None, description="SMA, EMA, BOLLINGER, STOCHASTIC, ADR_PCT, or DAYS_BACK")
    period: Optional[int] = Field(default=None, description="Single lookback period (e.g. 20)")
    periods: Optional[List[int]] = Field(default=None, description="Multiple lookback periods for batch calculation (e.g. [10, 20, 50, 200])")
    std_dev: Optional[float] = Field(default=2.0, description="Standard deviation multiplier for Bollinger Bands")
    k_period: Optional[int] = Field(default=None, description="Stochastic %K lookback period (e.g. 14)")
    d_period: Optional[int] = Field(default=None, description="Stochastic %D smoothing period (e.g. 3)")
    slowing: Optional[int] = Field(default=None, description="Stochastic %K slowing period (e.g. 3)")
    # Batch-Form (Plan CHARTVIEWER_SPREAD_PANES_PLAN.md, Phase 2): der Aufrufer
    # benennt seine Reihen selbst, ein Render braucht EINEN Aufruf statt vier.
    requests: Optional[List["CalcRequest"]] = Field(
        default=None, description="Batch: Liste aus {key, indicator_type, params}. Ohne dieses Feld bleibt das Altverhalten unveraendert."
    )


class CalcRequest(BaseModel):
    """Eine angeforderte Reihe im Batch-Modus (Schluessel vergibt der Aufrufer)."""
    key: str = Field(..., description="Name, unter dem die Reihe zurueckkommt (z. B. 'spread_rel_SPY_RSP')")
    indicator_type: str = Field(..., description="SPREAD | SMA | EMA | BOLLINGER | ADR_PCT | STOCHASTIC | DAYS_BACK")
    params: Dict[str, Any] = Field(default_factory=dict, description="Parameter, z. B. {a, b, mode} oder {window, source}")


def load_raw_ohlcv(symbol: str, timeframe: str = "1D", limit: int = DEFAULT_CANDLE_LIMIT, lookback_buffer: Optional[int] = None) -> pd.DataFrame:
    base_file = PARQUET_BASE / symbol.upper() / f"{timeframe.upper()}.parquet"
    feat_file = PARQUET_BASE / symbol.upper() / f"{timeframe.upper()}_features.parquet"
    parquet_file = feat_file if feat_file.exists() else base_file

    if not parquet_file.exists():
        raise HTTPException(status_code=404, detail=f"Parquet data for {symbol} ({timeframe}) not found")

    db = duckdb.connect()
    # Read extra lookback so rolling averages or extremes on early candles have valid history
    buf = lookback_buffer if lookback_buffer is not None else 250
    buffer_limit = min(limit + buf, max(MAX_CANDLE_LIMIT, DAYS_BACK_MAX_HISTORY) + buf)
    query = f"SELECT * FROM read_parquet('{parquet_file}') ORDER BY timestamp DESC LIMIT {buffer_limit}"
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
    if "high" not in df.columns or "low" not in df.columns:
        raise HTTPException(status_code=400, detail="ADR_PCT requires 'high' and 'low' columns in dataset.")
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
    if "low" not in df.columns or "high" not in df.columns or "close" not in df.columns:
        raise HTTPException(status_code=400, detail="STOCHASTIC requires 'high', 'low', and 'close' columns.")
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


def calculate_days_back_array(values: np.ndarray) -> np.ndarray:
    """Calculates signed consecutive extreme days:
    +N: Today is higher than the previous N consecutive trading days (new N-day high).
    -N: Today is lower than the previous N consecutive trading days (new N-day low).
    0:  Unchanged or neutral (e.g. index 0 or stagnation where values[i] == values[i-1]).
    """
    n = len(values)
    days_back = np.zeros(n, dtype=np.float64)

    for i in range(1, n):
        val = values[i]
        prev = values[i - 1]

        if np.isnan(val) or np.isnan(prev):
            days_back[i] = np.nan
            continue

        if val > prev:
            # Positive trend: count consecutive prior bars strictly lower than val
            cnt = 0
            for k in range(i - 1, -1, -1):
                if np.isnan(values[k]):
                    continue
                if values[k] < val:
                    cnt += 1
                else:
                    break
            days_back[i] = float(cnt)
        elif val < prev:
            # Negative trend: count consecutive prior bars strictly higher than val
            cnt = 0
            for k in range(i - 1, -1, -1):
                if np.isnan(values[k]):
                    continue
                if values[k] > val:
                    cnt += 1
                else:
                    break
            days_back[i] = -float(cnt)
        else:
            days_back[i] = 0.0

    return days_back


def calculate_days_back(df: pd.DataFrame, source: str) -> Dict[str, List[Optional[float]]]:
    values = df[source].to_numpy(dtype=float)
    days_back = calculate_days_back_array(values)
    return {"days_back": [None if np.isnan(v) else float(v) for v in days_back]}


_SPREAD_MODES = ("rel", "ratio", "abs")


class CalcDataError(Exception):
    """Datenproblem EINER angeforderten Reihe - wird zu errors[] statt zu HTTP 4xx."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def compute_spread(df_a: pd.DataFrame, df_b: pd.DataFrame, mode: str):
    """Spread zweier Instrumente, ankerfrei und ohne Forward-Fill.

    rel   = 100 * (a / b - 1)   Vorzeichen, 0 = Paritaet (Default)
    ratio = a / b               immer positiv
    abs   = a - b               Kurspunkte
    Inner Join auf timestamp: eine Leg ohne Bar erzeugt eine Luecke, keinen
    erfundenen Wert. b == 0 oder NaN -> kein Punkt (niemals 0).
    """
    a = df_a[["timestamp", "close"]].rename(columns={"close": "a"})
    b = df_b[["timestamp", "close"]].rename(columns={"close": "b"})
    a = a.assign(timestamp=[normalize_ts(x) for x in a["timestamp"]])
    b = b.assign(timestamp=[normalize_ts(x) for x in b["timestamp"]])
    joined = a.merge(b, on="timestamp", how="inner").sort_values("timestamp")
    if joined.empty:
        raise CalcDataError("no_common_history", "keine gemeinsamen Handelstage beider Legs")
    av = joined["a"].to_numpy(dtype=float)
    bv = joined["b"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        if mode == "rel":
            vals = 100.0 * (av / bv - 1.0)
        elif mode == "ratio":
            vals = av / bv
        else:
            vals = av - bv
    vals = np.where(np.isfinite(vals), vals, np.nan)
    ts = [int(x) for x in joined["timestamp"]]
    out = [None if np.isnan(v) else round(float(v), 4) for v in vals]
    return ts, out


def _timestamps_of(df: pd.DataFrame, limit: int):
    tail = df.tail(min(int(limit), len(df)))
    return [normalize_ts(x) for x in tail["timestamp"]]


def _indicator_series(ind_type: str, df: pd.DataFrame, src: str, periods, std_dev=None,
                      k_period=None, d_period=None, slowing=None) -> Dict[str, List[Optional[float]]]:
    """Die EINE Rechenstelle je Indikator-Typ - Altpfad und Batch nutzen sie."""
    if ind_type == "SMA":
        if not periods:
            raise HTTPException(status_code=400, detail="Für SMA muss mindestens eine Periode (period oder periods) angegeben werden.")
        return calculate_sma(df, src, periods)
    if ind_type == "EMA":
        if not periods:
            raise HTTPException(status_code=400, detail="Für EMA muss mindestens eine Periode (period oder periods) angegeben werden.")
        return calculate_ema(df, src, periods)
    if ind_type == "BOLLINGER":
        if not periods:
            raise HTTPException(status_code=400, detail="Für BOLLINGER muss mindestens eine Periode (period oder periods) angegeben werden.")
        return calculate_bollinger(df, src, periods, std_dev if std_dev is not None else 2.0)
    if ind_type == "ADR_PCT":
        if not periods:
            raise HTTPException(status_code=400, detail="Für ADR_PCT muss mindestens eine Periode (period oder periods) angegeben werden.")
        return calculate_adr_pct(df, periods)
    if ind_type == "STOCHASTIC":
        return calculate_stochastic(df, k_period or 14, d_period or 3, slowing or 3)
    if ind_type == "DAYS_BACK":
        return calculate_days_back(df, src)
    raise HTTPException(status_code=400, detail=f"Unbekannter indicator_type '{ind_type}'. Gültig: SMA, EMA, BOLLINGER, STOCHASTIC, ADR_PCT, DAYS_BACK.")


def _periods_from(params: Dict[str, Any]):
    periods = [int(p) for p in (params.get("periods") or []) if int(p) > 0]
    if not periods and params.get("period"):
        periods = [int(params["period"])]
    return periods


def _calculate_request_batch(req: "IndicatorRequest") -> Dict[str, Any]:
    """Batch: je Request ein Schluessel; Datenprobleme landen in errors[] (HTTP 200).

    Der Aufrufer benennt seine Reihe (key), damit der Viewer keine Ergebnisnamen
    raten muss. Jede Reihe bringt ihre eigenen timestamps mit (beim Spread sind
    das die gemeinsamen Handelstage beider Legs).
    """
    tf = req.timeframe.upper()
    limit = min(max(1, req.limit), MAX_CANDLE_LIMIT)
    symbol = (req.symbol or "").upper()
    out_series: Dict[str, Any] = {}
    latest: Dict[str, Any] = {}
    errors: List[Dict[str, Any]] = []
    for item in req.requests or []:
        it = (item.indicator_type or "").upper()
        p = dict(item.params or {})
        try:
            if it == "SPREAD":
                a = str(p.get("a") or "").upper()
                b = str(p.get("b") or "").upper()
                mode = str(p.get("mode") or "rel").lower()
                if not a or not b:
                    raise CalcDataError("invalid_request", "SPREAD braucht params.a und params.b")
                if mode not in _SPREAD_MODES:
                    raise CalcDataError("invalid_request", f"unbekannter Spread-Modus '{mode}' (rel|ratio|abs)")
                try:
                    df_a = load_raw_ohlcv(a, tf, limit)
                    df_b = load_raw_ohlcv(b, tf, limit)
                except HTTPException as exc:
                    raise CalcDataError("unknown_instrument", f"Leg ohne Daten ({tf}): {exc.detail}")
                ts, values = compute_spread(df_a, df_b, mode)
                ts, values = ts[-limit:], values[-limit:]
            else:
                df = load_raw_ohlcv(symbol, tf, limit)
                src = str(p.get("source") or req.source or "close").lower()
                col_map = {c.lower(): c for c in df.columns}
                if src not in col_map:
                    raise CalcDataError("unknown_source_column", f"Quelle '{src}' fehlt in den Daten")
                series_dict = _indicator_series(
                    it, df, src, _periods_from(p), p.get("std_dev"),
                    p.get("k_period"), p.get("d_period"), p.get("slowing"),
                )
                values = next(iter(series_dict.values()))
                ts = _timestamps_of(df, limit)
                values = values[-len(ts):]
            out_series[item.key] = {"timestamps": ts, "values": values}
            latest[item.key] = values[-1] if values else None
        except CalcDataError as exc:
            errors.append({"key": item.key, "code": exc.code, "detail": exc.detail})
        except HTTPException as exc:
            errors.append({"key": item.key, "code": "no_data", "detail": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001 - eine kaputte Reihe darf den Rest nicht kippen
            errors.append({"key": item.key, "code": "calculation_failed", "detail": str(exc)[:200]})
    return {
        "symbol": symbol or None,
        "timeframe": tf,
        "count": len(out_series),
        "series": out_series,
        "latest_values": latest,
        "errors": errors,
    }


@router.post("/indicators/calculate")
async def calculate_indicator_endpoint(req: IndicatorRequest):
    if req.requests:
        return _calculate_request_batch(req)
    if not req.indicator_type:
        raise HTTPException(status_code=400, detail="indicator_type fehlt (Altpfad) - oder requests[] fuer den Batch-Modus nutzen.")
    ind_type = req.indicator_type.upper()
    src = req.source.lower()

    # Determine periods
    periods = []
    if req.periods:
        periods = [int(p) for p in req.periods if int(p) > 0]
    elif req.period:
        periods = [int(req.period)]

    # 1. Resolve Data: from provided values or from Parquet symbol
    if req.values is not None:
        if len(req.values) == 0:
            raise HTTPException(status_code=400, detail="Provided 'values' array is empty.")
        sym = "CUSTOM"
        tf = req.timeframe.upper()
        df = pd.DataFrame({src: [float(v) if v is not None else np.nan for v in req.values]})
        if req.timestamps and len(req.timestamps) == len(req.values):
            df["timestamp"] = req.timestamps
        else:
            df["timestamp"] = list(range(len(req.values)))
    elif req.symbol:
        sym = req.symbol.upper()
        tf = req.timeframe.upper()
        lookback = DAYS_BACK_MAX_HISTORY if ind_type == "DAYS_BACK" else 250
        df = load_raw_ohlcv(sym, tf, req.limit, lookback_buffer=lookback)
        
        # Match column case-insensitively
        col_map = {c.lower(): c for c in df.columns}
        if src not in col_map:
            raise HTTPException(status_code=400, detail=f"Source column '{src}' not found in data. Valid: {list(df.columns)}")
        actual_col = col_map[src]
        if actual_col != src:
            df[src] = df[actual_col]
    else:
        raise HTTPException(status_code=400, detail="Either 'symbol' or 'values' must be provided.")

    series_dict = _indicator_series(
        ind_type, df, src, periods, req.std_dev, req.k_period, req.d_period, req.slowing
    )

    # Slice to requested limit (from the end)
    req_limit = min(req.limit, len(df))
    trimmed_df = df.iloc[-req_limit:].reset_index(drop=True)

    timestamps = []
    for ts in trimmed_df["timestamp"]:
        if hasattr(ts, "timestamp"):
            timestamps.append(int(ts.timestamp()))
        else:
            ts_int = int(ts)
            if ts_int > 10_000_000_000:
                ts_int = ts_int // 1000
            timestamps.append(ts_int)

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
