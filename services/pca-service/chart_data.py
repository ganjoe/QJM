import os
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import duckdb
from pydantic import BaseModel
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


# ---------------------------------------------------------------------------
# Feature Registry: Supabase-backed (pca_features table)
# ---------------------------------------------------------------------------

import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://host.docker.internal:8001")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

def _supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

def _supabase_get(table: str, params: str = "") -> list:
    """Query Supabase PostgREST API."""
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}" if params else f"{SUPABASE_URL}/rest/v1/{table}"
    try:
        resp = httpx.get(url, headers=_supabase_headers(), timeout=5.0)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Supabase query failed (%s): %s", table, e)
        return []



def _supabase_post(table: str, payload: dict):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    try:
        resp = httpx.post(url, headers=_supabase_headers(), json=payload, timeout=5.0)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Supabase POST failed (%s): %s", table, e)
        if hasattr(e, 'response') and e.response:
            logger.error("Response: %s", e.response.text)
        raise HTTPException(status_code=500, detail=f"Database insert failed: {e}")

def _supabase_delete(table: str, params: str):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    try:
        resp = httpx.delete(url, headers=_supabase_headers(), timeout=5.0)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Supabase DELETE failed (%s): %s", table, e)
        raise HTTPException(status_code=500, detail=f"Database delete failed: {e}")

def _auto_create_feature_if_missing(feature_id: str):
    import re
    res = _supabase_get("pca_features", f"canonical_id=eq.{feature_id}")
    if res:
        return
    
    sma_match = re.match(r"^sma_(\d+)$", feature_id)
    if sma_match:
        period = int(sma_match.group(1))
        payload = {
            "canonical_id": feature_id, "display_name": f"SMA {period}", "calc_type": "SMA",
            "calc_params": {"window": period, "source": "close"}, "plot_type": "overlay_line",
            "default_style": {"color": "#FFD600", "width": 2}, "mode": "online"
        }
        _supabase_post("pca_features", payload)
        return

    ema_match = re.match(r"^(?:ma_)?ema_(\d+)$", feature_id)
    if ema_match:
        period = int(ema_match.group(1))
        payload = {
            "canonical_id": feature_id, "display_name": f"EMA {period}", "calc_type": "EMA",
            "calc_params": {"window": period, "source": "close"}, "plot_type": "overlay_line",
            "default_style": {"color": "#FF6D00", "width": 2}, "mode": "online"
        }
        _supabase_post("pca_features", payload)
        return
        
    adr_sma_match = re.match(r"^adr_(\d+)_sma$", feature_id)
    if adr_sma_match:
        period = int(adr_sma_match.group(1))
        payload = {
            "canonical_id": feature_id, "display_name": f"ADR% SMA {period}", "calc_type": "ADR_SMA",
            "calc_params": {"window": period}, "plot_type": "sub_line",
            "default_style": {"color": "#B39DDB", "width": 2}, "mode": "online"
        }
        _supabase_post("pca_features", payload)
        return
        
    adr_pct_match = re.match(r"^adr_(\d+)_pct$", feature_id)
    if adr_pct_match:
        period = int(adr_pct_match.group(1))
        payload = {
            "canonical_id": feature_id, "display_name": f"ADR% {period}", "calc_type": "ADR_PCT",
            "calc_params": {"window": period}, "plot_type": "sub_line",
            "default_style": {"color": "#00BCD4", "width": 2}, "mode": "online"
        }
        _supabase_post("pca_features", payload)
        return


def get_feature_registry_from_db(mode: Optional[str] = None, calc_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch features from Supabase pca_features table."""
    filters = []
    if mode:
        filters.append(f"mode=eq.{mode}")
    if calc_type:
        filters.append(f"calc_type=eq.{calc_type}")
    filters.append("order=calc_type,canonical_id")
    return _supabase_get("pca_features", "&".join(filters))


@router.get("/features/registry")
async def get_features_registry(
    symbol: Optional[str] = Query(default=None, description="Optional symbol to inspect"),
    mode: Optional[str] = Query(default=None, description="Filter by mode: offline, online"),
    calc_type: Optional[str] = Query(default=None, description="Filter by calc_type: SMA, EMA, BOLLINGER, etc."),
):
    """Returns all registered features with display metadata from Supabase."""
    features = get_feature_registry_from_db(mode=mode, calc_type=calc_type)
    return {
        "features": features,
        "count": len(features),
        "plot_types": sorted(set(f.get("plot_type", "") for f in features)),
        "calc_types": sorted(set(f.get("calc_type", "") for f in features)),
    }


# ---------------------------------------------------------------------------
# Indicator Presets: Supabase-backed (pca_feature_sets + pca_feature_set_members)
# ---------------------------------------------------------------------------


class PresetMember(BaseModel):
    feature_id: str
    sort_order: int = 0
    style_override: dict = {}

class PresetCreate(BaseModel):
    id: str
    display_name: str
    description: str = ""
    topbar_metrics: list[str] = []
    members: list[PresetMember] = []

@router.get("/presets")
async def get_presets():
    """Returns all available indicator presets from Supabase."""
    sets = _supabase_get("pca_feature_sets", "order=id")
    # Count members per set
    members = _supabase_get("pca_feature_set_members", "select=set_id")
    member_counts = {}
    for m in members:
        sid = m.get("set_id")
        member_counts[sid] = member_counts.get(sid, 0) + 1

    return {
        "presets": {
            s["id"]: {
                "display_name": s.get("display_name", s["id"]),
                "description": s.get("description", ""),
                "indicator_count": member_counts.get(s["id"], 0),
            }
            for s in sets
        },
        "count": len(sets),
    }


@router.post("/presets")
async def create_preset(preset: PresetCreate):
    for m in preset.members:
        _auto_create_feature_if_missing(m.feature_id)
        
    set_payload = {
        "id": preset.id,
        "display_name": preset.display_name,
        "description": preset.description,
        "topbar_metrics": preset.topbar_metrics
    }
    _supabase_post("pca_feature_sets", set_payload)
    
    for m in preset.members:
        member_payload = {
            "set_id": preset.id,
            "feature_id": m.feature_id,
            "sort_order": m.sort_order,
            "style_override": m.style_override
        }
        _supabase_post("pca_feature_set_members", member_payload)
    return {"status": "success", "id": preset.id}

@router.put("/presets/{preset_name}")
async def update_preset(preset_name: str, preset: PresetCreate):
    for m in preset.members:
        _auto_create_feature_if_missing(m.feature_id)
        
    url = f"{SUPABASE_URL}/rest/v1/pca_feature_sets?id=eq.{preset_name}"
    set_payload = {
        "display_name": preset.display_name,
        "description": preset.description,
        "topbar_metrics": preset.topbar_metrics
    }
    try:
        resp = httpx.patch(url, headers=_supabase_headers(), json=set_payload, timeout=5.0)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Supabase PATCH failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to update preset")
        
    _supabase_delete("pca_feature_set_members", f"set_id=eq.{preset_name}")
    
    for m in preset.members:
        member_payload = {
            "set_id": preset_name, # use URL parameter as safe fallback
            "feature_id": m.feature_id,
            "sort_order": m.sort_order,
            "style_override": m.style_override
        }
        _supabase_post("pca_feature_set_members", member_payload)
    return {"status": "success", "id": preset_name}

@router.delete("/presets/{preset_name}")
async def delete_preset(preset_name: str):
    _supabase_delete("pca_feature_sets", f"id=eq.{preset_name}")
    return {"status": "success"}

@router.get("/presets/{preset_name}")
async def get_preset(preset_name: str):
    """Returns the full configuration for a specific preset, with resolved feature styles."""
    sets = _supabase_get("pca_feature_sets", f"id=eq.{preset_name}")
    if not sets:
        raise HTTPException(status_code=404, detail=f"Preset '{preset_name}' not found.")
    preset = sets[0]

    # Fetch members with join to pca_features
    members = _supabase_get(
        "pca_feature_set_members",
        f"set_id=eq.{preset_name}&select=feature_id,sort_order,style_override,pca_features(canonical_id,calc_type,calc_params,plot_type,default_style,mode)&order=sort_order"
    )

    indicators = []
    for m in members:
        feature = m.get("pca_features", {})
        if not feature:
            continue

        # Merge: default_style from feature, overridden by style_override from set member
        default_style = feature.get("default_style", {}) or {}
        style_override = m.get("style_override", {}) or {}
        merged_style = {**default_style, **style_override}

        # Use canonical_id as column reference (will match Parquet column after Phase 5)
        canonical_id = feature.get("canonical_id", m.get("feature_id", ""))

        # For Bollinger bands, the column in Parquet is bb_XX_upper
        if feature.get("calc_type") == "BOLLINGER":
            window = feature.get("calc_params", {}).get("window", 20)
            column = f"bb_{window}_upper"
        else:
            column = canonical_id

        indicators.append({
            "column": column,
            "canonical_id": canonical_id,
            "calc_type": feature.get("calc_type"),
            "mode": feature.get("mode"),
            "style": merged_style,
        })

    return {
        "name": preset_name,
        "display_name": preset.get("display_name", preset_name),
        "description": preset.get("description", ""),
        "indicators": indicators,
        "topbar_metrics": preset.get("topbar_metrics", []),
    }



