import os
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import duckdb
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger("pca.chart_data")
router = APIRouter()

PARQUET_BASE = Path(os.environ.get("PARQUET_BASE_PATH", "/parquet"))
DEFAULT_CANDLE_LIMIT = int(os.environ.get("DEFAULT_CANDLE_LIMIT", "200"))
MAX_CANDLE_LIMIT = int(os.environ.get("MAX_CANDLE_LIMIT", "2000"))

BASE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _parquet_path(symbol: str, timeframe: str = "1D", features: bool = False) -> Path:
    suffix = f"{timeframe}_features" if features else timeframe
    return PARQUET_BASE / symbol.upper() / f"{suffix}.parquet"


def read_chart_data(symbol: str, timeframe: str = "1D", limit: int = DEFAULT_CANDLE_LIMIT, features: bool = True) -> Dict[str, Any]:
    """
    Reads OHLCV and feature data directly from Parquet files via DuckDB.
    Detects staleness/lag if features lag behind the latest OHLCV candle.
    """
    symbol = symbol.upper()
    timeframe = timeframe.upper()
    limit = min(max(1, limit), MAX_CANDLE_LIMIT)

    base_path = _parquet_path(symbol, timeframe, features=False)
    feat_path = _parquet_path(symbol, timeframe, features=True)

    if not base_path.exists():
        return {
            "status": "missing",
            "symbol": symbol,
            "timeframe": timeframe,
            "count": 0,
            "columns": BASE_COLUMNS,
            "data": [],
            "notice": f"Keine {timeframe}-Daten für {symbol} gefunden.",
            "features_stale": False,
            "lag_bars": 0,
            "lag_days": 0,
        }

    has_features = features and feat_path.exists()
    target_path = feat_path if has_features else base_path

    try:
        db = duckdb.connect()
        cols = "timestamp, open, high, low, close, volume"
        columns = list(BASE_COLUMNS)

        if has_features:
            schema_res = db.execute(f"DESCRIBE SELECT * FROM read_parquet('{target_path}') LIMIT 1").fetchall()
            all_cols = [r[0] for r in schema_res]
            extra_cols = [c for c in all_cols if c not in set(BASE_COLUMNS) and c not in ("t", "ticker")]
            if extra_cols:
                cols += ", " + ", ".join(f'"{c}"' for c in extra_cols)
                columns.extend(extra_cols)

        query = f"SELECT {cols} FROM read_parquet('{target_path}') ORDER BY timestamp DESC LIMIT {limit}"
        rows = db.execute(query).fetchall()
        db.close()

        # Reverse so data is chronologically ascending (oldest to newest)
        rows.reverse()

        formatted_data = []
        for r in rows:
            row_list = list(r)
            ts = row_list[0]
            if hasattr(ts, "timestamp"):
                ts = int(ts.timestamp())
                row_list[0] = ts
            elif isinstance(ts, int) and ts > 10000000000:
                row_list[0] = int(ts / 1000)
            formatted_data.append(row_list)

        # Staleness analysis for features
        features_stale = False
        lag_bars = 0
        lag_days = 0
        notice = None

        if has_features and len(formatted_data) > 0 and len(columns) > len(BASE_COLUMNS):
            feature_idx_start = len(BASE_COLUMNS)
            last_row = formatted_data[-1]
            last_ts = last_row[0]

            def has_valid_features(row):
                vals = row[feature_idx_start:]
                return any(v is not None and v == v for v in vals)

            if not has_valid_features(last_row):
                # Search backwards for the most recent candle with valid features
                valid_idx = None
                for i in range(len(formatted_data) - 1, -1, -1):
                    if has_valid_features(formatted_data[i]):
                        valid_idx = i
                        break

                if valid_idx is not None:
                    valid_ts = formatted_data[valid_idx][0]
                    lag_bars = (len(formatted_data) - 1) - valid_idx
                    lag_days = max(0, int((last_ts - valid_ts) // 86400))
                    features_stale = True

                    valid_date_str = datetime.fromtimestamp(valid_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    last_date_str = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    notice = (
                        f"⚠️ Features sind vom {valid_date_str} "
                        f"({lag_days} Tage / {lag_bars} Kerzen hinter jüngster Kurskerze vom {last_date_str})."
                    )
                else:
                    features_stale = True
                    notice = "⚠️ Keine gültigen Feature-Werte im abgefragten Zeitraum gefunden."

        return {
            "status": "ok",
            "symbol": symbol,
            "timeframe": timeframe,
            "count": len(formatted_data),
            "columns": columns,
            "data": formatted_data,
            "features_stale": features_stale,
            "lag_bars": lag_bars,
            "lag_days": lag_days,
            "notice": notice,
        }

    except Exception as e:
        logger.exception("DuckDB read error for %s: %s", symbol, e)
        raise HTTPException(status_code=500, detail=f"Database error reading {symbol}: {str(e)}")


def list_available_feature_names(symbol: Optional[str] = None) -> List[str]:
    """
    Returns the list of available precalculated feature column names from Parquet.
    """
    target_dir = None
    if symbol:
        cand = PARQUET_BASE / symbol.upper() / "1D_features.parquet"
        if cand.exists():
            target_dir = cand
    if not target_dir:
        # Find first existing 1D_features.parquet
        for ticker_dir in PARQUET_BASE.iterdir():
            if ticker_dir.is_dir():
                cand = ticker_dir / "1D_features.parquet"
                if cand.exists():
                    target_dir = cand
                    break

    if not target_dir or not target_dir.exists():
        return []

    try:
        db = duckdb.connect()
        schema_res = db.execute(f"DESCRIBE SELECT * FROM read_parquet('{target_dir}') LIMIT 1").fetchall()
        db.close()
        all_cols = [r[0] for r in schema_res]
        feature_cols = [c for c in all_cols if c not in set(BASE_COLUMNS) and c not in ("t", "ticker")]
        return sorted(feature_cols)
    except Exception as e:
        logger.error("Error inspecting features schema: %s", e)
        return []


@router.get("/chartdata")
async def get_chart_data(
    symbol: str = Query(..., description="Ticker symbol, e.g. MSFT"),
    timeframe: str = Query(default="1D", description="Timeframe, default 1D"),
    limit: int = Query(default=DEFAULT_CANDLE_LIMIT, ge=1, le=MAX_CANDLE_LIMIT, description="Number of candles"),
    features: bool = Query(default=True, description="Include precalculated feature columns"),
):
    return read_chart_data(symbol, timeframe, limit, features)


@router.get("/features/schema")
async def get_features_schema(symbol: Optional[str] = Query(default=None, description="Optional symbol to inspect")):
    features = list_available_feature_names(symbol)
    return {"features": features, "count": len(features)}
