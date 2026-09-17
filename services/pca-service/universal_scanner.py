"""
services/pca-service/universal_scanner.py — Universal Multi-Factor Stock Scanner

Combines Parquet time-series data & technical features (1D.parquet, 1D_features.parquet)
with Supabase master universe fundamentals (cda_master_universe) into a single,
fully dynamic screening engine.

Zero hardcoded filters or forced defaults:
- Evaluates arbitrary user-defined conditions and SQL/DuckDB expressions.
- Introspects data schemas dynamically at runtime (Parquet + Supabase + Computed).
- Minimalist/token-efficient help output (Name + expanded abbreviation or single word).
"""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import duckdb
import httpx
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from scan_watchlist import (
    SCAN_WATCHLIST_NAME,
    SCAN_WATCHLIST_SOURCE,
    fetch_scan_watchlist,
    sync_scan_watchlist,
)

logger = logging.getLogger("pca.universal_scanner")

router = APIRouter(tags=["universal_scanner"])

# --- Environment & Configuration (RULE[user_global]: No magic numbers) ---
def _first_existing_dir(*candidates: Optional[str]) -> Path:
    for cand in candidates:
        if cand and Path(cand).is_dir():
            return Path(cand)
    return Path("/parquet")

PARQUET_BASE = _first_existing_dir(
    os.environ.get("PARQUET_BASE_PATH"),
    "/parquet",
    "/home/daniel/stock-data-node/data/parquet",
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://host.docker.internal:8001").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

UNIVERSAL_SCANNER_DEFAULT_LIMIT = int(os.environ.get("UNIVERSAL_SCANNER_DEFAULT_LIMIT", "50"))
UNIVERSAL_SCANNER_MAX_LIMIT = int(os.environ.get("UNIVERSAL_SCANNER_MAX_LIMIT", "1000"))
UNIVERSAL_SCANNER_METADATA_TTL_SEC = float(os.environ.get("UNIVERSAL_SCANNER_METADATA_TTL_SEC", "300.0"))
UNIVERSAL_SCANNER_WORKERS = int(os.environ.get("UNIVERSAL_SCANNER_WORKERS", "16"))
POSTGREST_UNIVERSE_LIMIT = int(os.environ.get("POSTGREST_UNIVERSE_LIMIT", "20000"))
UNIVERSAL_SCANNER_SCHEMA_TTL_SEC = float(os.environ.get("UNIVERSAL_SCANNER_SCHEMA_TTL_SEC", "300.0"))
# Neutral score for candles without a high-low range (flat / illiquid day). A DCR/WCR of a
# zero-range bar is undefined, so a neutral 50 % is reported instead of silently producing a
# bullish/bearish (0 % / 100 %) signal. The DCR/WCR scanners use the same convention.
UNIVERSAL_SCANNER_FLAT_RANGE_SCORE = float(os.environ.get("UNIVERSAL_SCANNER_FLAT_RANGE_SCORE", "50.0"))
_RANGE_EPSILON = 1e-6

# Virtual bookkeeping series (`$STATS.ASSETS_VALUE`, `$STATS.CASH_QUOTE`, `$STATS.MARKET_BREADTH`,
# `$STATS.NAV`, `$STATS.PNL`) exist in the Parquet store as real directories and are therefore
# synced into `cda_master_universe` as if they were instruments. They are not tradeable: every
# universe source (master universe metadata, explicit `tickers`, watchlists, Parquet fallback)
# must drop them. The technical scanners and the breadth endpoint already do the same.
VIRTUAL_TICKER_PREFIXES = ("$",)


def is_virtual_ticker(ticker: Optional[str]) -> bool:
    """Whether a symbol is a virtual bookkeeping series rather than a tradeable instrument."""
    return bool(ticker) and ticker.startswith(VIRTUAL_TICKER_PREFIXES)

# Minimalist expanded acronym / single word descriptions for known fields
KNOWN_ABBREVIATIONS: Dict[str, str] = {
    "ticker": "Symbol",
    "close": "Schlusskurs",
    "open": "Eröffnungskurs",
    "high": "Tageshoch",
    "low": "Tagestief",
    "volume": "Volumen",
    "dollar_volume": "Tagesumsatz",
    "ma_sma_50_dollar_volume": "50d-Durchschnittsumsatz",
    "dcr": "Day Close Range",
    "wcr": "Week Close Range",
    "daily_range": "Tageskerzenspanne",
    "adr_20": "Average Daily Range",
    "ibd_rs": "IBD Relative Strength",
    "minervini_score": "Minervini Trend Score",
    "minervini_trend_template": "Minervini Trend Template",
    "ma_sma_10": "10 SMA",
    "ma_sma_20": "20 SMA",
    "ma_sma_50": "50 SMA",
    "ma_sma_100": "100 SMA",
    "ma_sma_150": "150 SMA",
    "ma_sma_200": "200 SMA",
    "bb_20_avg": "Bollinger Band Mitte",
    "bb_20_upper": "Bollinger Band Oben",
    "bb_20_lower": "Bollinger Band Unten",
    "bb_20_bandwidth": "Bollinger Bandbreite",
    "stock_10_1_k": "Stochastik %K",
    "stock_10_1_d": "Stochastik %D",
    "breadth_minervini": "Minervini Marktbreite",
    "breadth_minervini_pct": "Minervini Marktbreite %",
    "shares_outstanding": "Aktienanzahl",
    "currency": "Währung",
    "eps": "Earnings Per Share",
    "revenue": "Jahresumsatz",
    "earnings": "Quartalszahlen-Datum",
    "market_cap": "Marktkapitalisierung",
    "pe_ratio": "Kurs-Gewinn-Verhältnis",
    "days_to_earnings": "Tage bis Quartalszahlen",
    "price_to_sma50_pct": "Abstand 50 SMA",
    "price_to_sma200_pct": "Abstand 200 SMA",
}

# Runtime computed fields definition
COMPUTED_FIELDS: Dict[str, str] = {
    "dcr": "float",
    "wcr": "float",
    "market_cap": "float",
    "pe_ratio": "float",
    "days_to_earnings": "int",
    "price_to_sma50_pct": "float",
    "price_to_sma200_pct": "float",
}

# Supabase columns that are infrastructure/bookkeeping and must never be exposed as filterable
# stock metadata. Everything else on cda_master_universe is discovered dynamically at runtime.
SUPABASE_SYSTEM_COLUMNS = {"ticker", "has_parquet", "last_updated", "created_at", "utime"}

# Fallback catalog used before the first successful cda_master_universe fetch (or when it fails).
KNOWN_SUPABASE_FIELDS: List[Tuple[str, str]] = [
    ("shares_outstanding", "float"),
    ("currency", "string"),
    ("eps", "float"),
    ("revenue", "float"),
    ("earnings", "date"),
]
KNOWN_SUPABASE_TYPES: Dict[str, str] = {name: typ for name, typ in KNOWN_SUPABASE_FIELDS}

# In-memory caches. A timestamp of 0.0 means "never populated"; empty-but-populated results are
# cached as well (negative caching) so a failing Supabase endpoint is not hit on every request.
_metadata_cache: Tuple[float, Dict[str, Dict[str, Any]]] = (0.0, {})
_parquet_fields_cache: Tuple[float, List[Tuple[str, str]]] = (0.0, [])


def _supabase_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if SUPABASE_KEY:
        headers["apikey"] = SUPABASE_KEY
        headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
    return headers


# --- Dynamic Schema Introspection ---
def _infer_supabase_type(column: str, value: Any) -> str:
    """Infers the catalog type of a Supabase column from a sample value (known types win)."""
    if column in KNOWN_SUPABASE_TYPES:
        return KNOWN_SUPABASE_TYPES[column]
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return "date"
    return "string"


def _supabase_field_catalog() -> List[Tuple[str, str]]:
    """Returns the filterable business columns of cda_master_universe.

    Discovery is driven by the columns actually returned by the last metadata fetch, so a new
    Supabase column becomes filterable without a code change. Before the first fetch (or when the
    fetch fails) the known fallback catalog is used.
    """
    _, cached = _metadata_cache
    if cached:
        sample = next(iter(cached.values()))
        catalog: List[Tuple[str, str]] = []
        for column, value in sample.items():
            if column in SUPABASE_SYSTEM_COLUMNS:
                continue
            catalog.append((column, _infer_supabase_type(column, value)))
        if catalog:
            return catalog
    return list(KNOWN_SUPABASE_FIELDS)


def _parquet_field_catalog() -> List[Tuple[str, str]]:
    """Introspects a sample Parquet file once per schema TTL and caches the column types."""
    global _parquet_fields_cache
    now = time.time()
    last_ts, cached = _parquet_fields_cache
    if cached and (now - last_ts < UNIVERSAL_SCANNER_SCHEMA_TTL_SEC):
        return cached

    columns: List[Tuple[str, str]] = []
    try:
        sample_file = None
        if PARQUET_BASE.is_dir():
            for p in PARQUET_BASE.glob("*/*_features.parquet"):
                sample_file = p
                break
            if not sample_file:
                for p in PARQUET_BASE.glob("*/*.parquet"):
                    sample_file = p
                    break

        if sample_file and sample_file.exists():
            db = duckdb.connect()
            try:
                schema = db.execute(f"DESCRIBE SELECT * FROM read_parquet('{sample_file}')").fetchall()
                for col_name, col_type, *_ in schema:
                    c_clean = col_name.strip()
                    if c_clean in ("t", "ticker"):
                        continue
                    t_lower = str(col_type).lower()
                    mapped_type = "float"
                    if "bool" in t_lower:
                        mapped_type = "bool"
                    elif "int" in t_lower:
                        mapped_type = "int"
                    elif "varchar" in t_lower or "text" in t_lower:
                        mapped_type = "string"
                    elif "date" in t_lower or "time" in t_lower:
                        mapped_type = "date"
                    columns.append((c_clean, mapped_type))
            finally:
                db.close()
    except Exception as e:
        logger.warning("Dynamic Parquet schema introspection fallback: %s", e)
        return cached

    if columns:
        _parquet_fields_cache = (now, columns)
    return columns


def get_dynamic_fields() -> List[Dict[str, Any]]:
    """Builds the live field catalog from Parquet, Supabase and computed fields."""
    fields_dict: Dict[str, Dict[str, Any]] = {}

    fields_dict["ticker"] = {
        "name": "ticker",
        "type": "string",
        "source": "identity",
        "description": KNOWN_ABBREVIATIONS.get("ticker", ""),
    }

    for c_name, c_type in _parquet_field_catalog():
        fields_dict[c_name] = {
            "name": c_name,
            "type": c_type,
            "source": "parquet",
            "description": KNOWN_ABBREVIATIONS.get(c_name, ""),
        }

    for c_name, c_type in _supabase_field_catalog():
        fields_dict[c_name] = {
            "name": c_name,
            "type": c_type,
            "source": "supabase",
            "description": KNOWN_ABBREVIATIONS.get(c_name, ""),
        }

    for c_name, c_type in COMPUTED_FIELDS.items():
        fields_dict[c_name] = {
            "name": c_name,
            "type": c_type,
            "source": "computed",
            "description": KNOWN_ABBREVIATIONS.get(c_name, ""),
        }

    return list(fields_dict.values())


# --- Supabase Metadata Fetching & Caching ---
async def fetch_master_universe_metadata(force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """Fetches fundamental metadata from cda_master_universe with TTL + negative caching.

    The full row (select=*) is fetched so every current business column is available to the
    dynamic field catalog; infrastructure columns are stripped again when building records.
    """
    global _metadata_cache
    now = time.time()
    last_ts, cached = _metadata_cache
    if not force_refresh and last_ts > 0 and (now - last_ts < UNIVERSAL_SCANNER_METADATA_TTL_SEC):
        return cached

    url = f"{SUPABASE_URL}/rest/v1/cda_master_universe"
    params = {
        "has_parquet": "eq.true",
        "select": "*",
        "limit": str(POSTGREST_UNIVERSE_LIMIT),
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params, headers=_supabase_headers())
            resp.raise_for_status()
            data = resp.json()
            mapping: Dict[str, Dict[str, Any]] = {}
            skipped_virtual = 0
            for row in data:
                t = str(row.get("ticker", "")).strip().upper()
                if not t:
                    continue
                if is_virtual_ticker(t):
                    skipped_virtual += 1
                    continue
                mapping[t] = row
            _metadata_cache = (now, mapping)
            logger.info(
                "Loaded metadata for %d tickers from cda_master_universe (%d virtual series skipped)",
                len(mapping),
                skipped_virtual,
            )
            return mapping
    except Exception as e:
        logger.error("Failed to fetch cda_master_universe metadata: %s", e)
        if cached:
            logger.warning("Using stale cda_master_universe metadata cache.")
            return cached
        # Negative cache: avoid hammering a failing endpoint on every request.
        _metadata_cache = (now, {})
        return {}


# --- Single Ticker Data Loading ---
def _coerce_metadata_value(column: str, value: Any) -> Any:
    """Coerces a raw PostgREST value into a JSON-friendly, filterable Python scalar."""
    if value is None:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    if isinstance(value, str):
        return value.strip().upper() if column == "currency" else value
    return str(value)


def load_ticker_record(ticker: str, metadata: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Loads latest bar and features for a single ticker and computes derived fields."""
    ticker_clean = ticker.strip().upper()
    ticker_dir = PARQUET_BASE / ticker_clean
    if not ticker_dir.is_dir():
        return None

    feat_path = ticker_dir / "1D_features.parquet"
    base_path = ticker_dir / "1D.parquet"
    target = feat_path if feat_path.exists() else base_path
    if not target.exists():
        return None

    try:
        # Fast direct PyArrow read without SQL parser or DuckDB connection overhead
        tbl = pq.read_table(target)
        n = tbl.num_rows
        if n == 0:
            return None
        # Parquet files are already sorted chronologically (timestamp ASC)
        # Take the last 10 rows (or all if < 10)
        start_idx = max(0, n - 10)
        df = tbl.slice(start_idx, n - start_idx).to_pandas()
    except Exception as e:
        logger.debug("Failed to read parquet for %s: %s", ticker_clean, e)
        return None

    if df is None or df.empty:
        return None

    latest = df.iloc[-1].to_dict()

    record: Dict[str, Any] = {"ticker": ticker_clean}

    # Populate raw parquet fields from latest candle
    for k, v in latest.items():
        if k in ("t", "ticker"):
            continue
        if pd.isna(v):
            record[k] = None
        elif isinstance(v, (np.bool_, bool)):
            # bool MUST be checked before int: Python bool is a subclass of int and would
            # otherwise be silently serialized as 0/1.
            record[k] = bool(v)
        elif isinstance(v, (np.integer, int)):
            record[k] = int(v)
        elif isinstance(v, (np.floating, float)):
            record[k] = round(float(v), 4)
        else:
            record[k] = str(v)

    c = float(record.get("close") or 0.0)
    h = float(record.get("high") or c)
    l = float(record.get("low") or c)

    # 1. Compute DCR (Day Close Range in %) with division-by-zero safety
    day_rng = h - l
    if day_rng <= _RANGE_EPSILON:
        record["dcr"] = UNIVERSAL_SCANNER_FLAT_RANGE_SCORE
    else:
        dcr_val = ((c - l) / day_rng) * 100.0
        record["dcr"] = round(max(0.0, min(100.0, dcr_val)), 2)

    # 2. Compute WCR (Week Close Range in %)
    try:
        dt_last = datetime.fromtimestamp(int(latest["timestamp"]), tz=timezone.utc)
        iso_last = dt_last.isocalendar()[:2]
        week_bars = [
            df.iloc[i] for i in range(len(df))
            if datetime.fromtimestamp(int(df["timestamp"].iloc[i]), tz=timezone.utc).isocalendar()[:2] == iso_last
        ]
        if week_bars:
            wh = max(float(b["high"]) for b in week_bars)
            wl = min(float(b["low"]) for b in week_bars)
            wk_rng = wh - wl
            if wk_rng <= _RANGE_EPSILON:
                record["wcr"] = UNIVERSAL_SCANNER_FLAT_RANGE_SCORE
            else:
                wcr_val = ((c - wl) / wk_rng) * 100.0
                record["wcr"] = round(max(0.0, min(100.0, wcr_val)), 2)
        else:
            record["wcr"] = record["dcr"]
    except Exception:
        record["wcr"] = record.get("dcr", UNIVERSAL_SCANNER_FLAT_RANGE_SCORE)

    # 3. Merge Supabase fundamental metadata generically so a newly added Supabase column stays
    #    filterable without a code change. Parquet values and system columns always win.
    meta = metadata or {}
    for col, value in meta.items():
        if col in SUPABASE_SYSTEM_COLUMNS or col in record:
            continue
        record[col] = _coerce_metadata_value(col, value)

    # Guarantee that every catalogued metadata column exists (as None) so the DuckDB frame keeps a
    # stable schema across a mixed universe of tickers with and without Supabase rows.
    for col, _typ in _supabase_field_catalog():
        record.setdefault(col, None)

    shares = record.get("shares_outstanding")
    eps = record.get("eps")

    # 4. Computed multi-factor fields
    if shares is not None and shares > 0 and c > 0:
        record["market_cap"] = round(c * shares, 2)
    else:
        record["market_cap"] = None

    if eps is not None and eps > 0 and c > 0:
        record["pe_ratio"] = round(c / eps, 2)
    else:
        record["pe_ratio"] = None

    if record["earnings"]:
        try:
            # Parse ISO date/timestamp
            earn_str = record["earnings"].replace("Z", "+00:00")
            dt_earn = datetime.fromisoformat(earn_str)
            dt_now = datetime.now(timezone.utc)
            record["days_to_earnings"] = (dt_earn.date() - dt_now.date()).days
        except Exception:
            record["days_to_earnings"] = None
    else:
        record["days_to_earnings"] = None

    sma50 = record.get("ma_sma_50")
    if sma50 and sma50 > 0 and c > 0:
        record["price_to_sma50_pct"] = round(((c - sma50) / sma50) * 100.0, 2)
    else:
        record["price_to_sma50_pct"] = None

    sma200 = record.get("ma_sma_200")
    if sma200 and sma200 > 0 and c > 0:
        record["price_to_sma200_pct"] = round(((c - sma200) / sma200) * 100.0, 2)
    else:
        record["price_to_sma200_pct"] = None

    return record


# --- Request & Response Models ---
class FilterCondition(BaseModel):
    field: str
    op: str = Field(description="Operator: '>=', '<=', '>', '<', '==', '=', '!=', 'in', 'between', 'is_not_null'")
    value: Optional[Union[float, int, str, bool, List[Any]]] = None
    field_compare: Optional[str] = Field(default=None, description="Optional other column name to compare against")


class UniversalScannerRequest(BaseModel):
    tickers: Optional[List[str]] = None
    watchlists: Optional[List[str]] = None
    filters: Optional[List[FilterCondition]] = None
    expression: Optional[str] = Field(default=None, description="DuckDB-compatible filter expression string")
    sort_by: Optional[str] = Field(default="dcr", description="Field name to sort by")
    sort_direction: Optional[str] = Field(default="desc", description="'desc' or 'asc'")
    limit: Optional[int] = Field(default=None, description="Max results to return")
    help: Optional[bool] = Field(default=False, description="If true, returns the dynamic field catalog")


class PerformanceReport(BaseModel):
    total_seconds: float
    universe_resolve_seconds: float
    data_loading_seconds: float
    filter_evaluation_seconds: float
    throughput_tickers_per_sec: float
    tickers_evaluated: int
    tickers_matched: int
    summary: str


class ScanWatchlistInfo(BaseModel):
    """State of the rolling auto-watchlist that always holds the last scan result."""
    list_name: str
    status: str
    count: int
    matched_total: int
    response_count: int
    truncated: bool
    updated_at: Optional[str] = None
    message: Optional[str] = None


class UniversalScannerResponse(BaseModel):
    status: str
    count: int
    total_evaluated: int
    fields: Optional[List[Dict[str, Any]]] = None
    records: List[Dict[str, Any]]
    matched_tickers: List[str]
    applied_filters: List[str]
    performance: Optional[PerformanceReport] = None
    watchlist: Optional[ScanWatchlistInfo] = None


# --- Auto-Watchlist helpers ---
def _scan_query_meta(req: "UniversalScannerRequest") -> Dict[str, Any]:
    """Serialises the scan query so the watchlist metadata explains what the list contains."""
    return {
        "filters": [f.model_dump() for f in (req.filters or [])],
        "expression": req.expression,
        "sort_by": req.sort_by,
        "sort_direction": req.sort_direction,
        "limit": req.limit,
        "tickers": req.tickers,
        "watchlists": req.watchlists,
    }


async def _sync_watchlist(
    tickers: List[str],
    req: "UniversalScannerRequest",
    *,
    evaluated_count: int,
    response_count: int,
) -> Dict[str, Any]:
    """Persists the complete scan result into the rolling auto-watchlist.

    ``sync_scan_watchlist`` already never raises; the extra guard keeps the scanner endpoint
    independent of the watchlist write under all circumstances.
    """
    try:
        return await sync_scan_watchlist(
            tickers,
            scanner=SCAN_WATCHLIST_SOURCE,
            query_meta=_scan_query_meta(req),
            evaluated_count=evaluated_count,
            response_count=response_count,
        )
    except Exception as e:  # noqa: BLE001 - the scan result must stay usable
        logger.error("Unexpected auto-watchlist failure: %s", e)
        return {
            "list_name": SCAN_WATCHLIST_NAME,
            "status": "error",
            "count": 0,
            "matched_total": len(tickers),
            "response_count": response_count,
            "truncated": False,
            "updated_at": None,
            "message": f"Auto-Watchlist nicht aktualisiert: {e}",
        }


# --- Endpoints ---
@router.get("/scanner/universal/fields")
async def list_universal_scanner_fields():
    """Returns the live dynamic catalog of all available fields."""
    await fetch_master_universe_metadata()
    fields = get_dynamic_fields()
    return {
        "fields": fields,
        "count": len(fields),
    }


@router.get("/scanner/universal/watchlist")
async def get_universal_scanner_watchlist():
    """Returns the rolling auto-watchlist of the universal scanner (tickers + run metadata).

    The list always holds the complete result set of the most recent scan run.
    """
    return await fetch_scan_watchlist()


def _sql_literal(value: Any) -> str:
    """Formats a Python scalar as a safe DuckDB literal (numbers unquoted, everything else quoted)."""
    if isinstance(value, bool):
        return str(value).upper()
    if isinstance(value, (int, float)):
        return repr(float(value))
    safe_str = str(value).replace("'", "''")
    return f"'{safe_str}'"


# Tokens a filter expression may reference besides the live field catalog. Everything else is
# rejected, which blocks DuckDB table functions (read_text, read_parquet, ...), subqueries
# (SELECT/FROM) and other escape hatches that could read arbitrary files.
_ALLOWED_EXPRESSION_TOKENS = {
    "and", "or", "not", "in", "is", "null", "true", "false", "between", "like", "ilike",
    "case", "when", "then", "else", "end", "cast", "as",
    "abs", "round", "floor", "ceil", "ceiling", "greatest", "least", "coalesce", "nullif",
    "if", "sign", "sqrt", "exp", "ln", "log", "log10", "power", "pow", "mod",
    "int", "integer", "bigint", "double", "float", "decimal", "numeric", "real",
    "varchar", "text", "date", "timestamp", "boolean", "bool",
}


def _validate_expression(expression: str, valid_fields: Dict[str, str]) -> str:
    """Validates a raw filter expression against the live field catalog.

    Only catalogued field references, plain literals, comparison/logical operators and a small
    whitelist of scalar functions are permitted.
    """
    expr_clean = expression.strip()
    lowered = expr_clean.lower()
    for token in (";", "--", "/*", "*/"):
        if token in lowered:
            raise HTTPException(status_code=400, detail=f"Illegal expression token '{token}'.")

    # Double-quoted identifiers must name a real field.
    for quoted in re.findall(r'"([^"]+)"', expr_clean):
        if quoted not in valid_fields:
            raise HTTPException(status_code=400, detail=f"Unknown field '{quoted}' in expression.")

    scanned = re.sub(r'"[^"]+"', " ", expr_clean)                          # drop quoted identifiers
    scanned = re.sub(r"'(?:''|[^'])*'", " ", scanned)                      # drop string literals
    scanned = re.sub(r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b", " ", scanned)  # drop numeric literals
    scanned = re.sub(r"\.\d+", " ", scanned)                               # drop leading-dot numbers

    valid_fields_lower = {name.lower() for name in valid_fields}
    for identifier in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", scanned):
        if identifier.lower() in _ALLOWED_EXPRESSION_TOKENS:
            continue
        if identifier.lower() in valid_fields_lower:
            continue
        raise HTTPException(
            status_code=400,
            detail=f"Unknown field or function '{identifier}' in expression. "
                   f"Only catalogued fields, literals and basic comparison operators are allowed.",
        )
    return expr_clean


def _build_duckdb_where(
    filters: Optional[List[FilterCondition]],
    expression: Optional[str],
    valid_fields: Dict[str, str],
) -> Tuple[str, List[str]]:
    """Builds and validates a DuckDB SQL WHERE clause from filters and/or expression."""
    clauses: List[str] = []
    applied_descriptions: List[str] = []

    if filters:
        for f in filters:
            fname = f.field.strip()
            if fname not in valid_fields:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid filter field '{fname}'. Use /api/scanner/universal/fields for available fields.",
                )

            op_clean = f.op.strip().lower()
            if f.field_compare:
                fcname = f.field_compare.strip()
                if fcname not in valid_fields:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid comparison field '{fcname}'.",
                    )
                sql_op = {"==": "=", "=": "=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}.get(op_clean)
                if not sql_op:
                    raise HTTPException(status_code=400, detail=f"Unsupported cross-column operator '{f.op}'.")
                clauses.append(f'("{fname}" IS NOT NULL AND "{fcname}" IS NOT NULL AND "{fname}" {sql_op} "{fcname}")')
                applied_descriptions.append(f"{fname} {sql_op} {fcname}")
                continue

            if op_clean in ("is_not_null", "not_null"):
                clauses.append(f'"{fname}" IS NOT NULL')
                applied_descriptions.append(f"{fname} IS NOT NULL")
            elif op_clean in ("is_null", "null"):
                clauses.append(f'"{fname}" IS NULL')
                applied_descriptions.append(f"{fname} IS NULL")
            elif op_clean == "between":
                if not isinstance(f.value, (list, tuple)) or len(f.value) != 2:
                    raise HTTPException(status_code=400, detail="'between' operator requires a 2-element array [min, max].")
                v1, v2 = f.value
                clauses.append(f'("{fname}" IS NOT NULL AND "{fname}" BETWEEN {_sql_literal(v1)} AND {_sql_literal(v2)})')
                applied_descriptions.append(f"{fname} BETWEEN {v1} AND {v2}")
            elif op_clean in ("in", "not_in"):
                if not isinstance(f.value, (list, tuple)):
                    raise HTTPException(status_code=400, detail=f"'{op_clean}' operator requires an array of values.")
                if len(f.value) == 0:
                    raise HTTPException(status_code=400, detail=f"'{op_clean}' operator requires at least one value.")
                formatted_vals = [_sql_literal(val) for val in f.value]
                sql_op = "IN" if op_clean == "in" else "NOT IN"
                clauses.append(f'("{fname}" IS NOT NULL AND "{fname}" {sql_op} ({", ".join(formatted_vals)}))')
                applied_descriptions.append(f'{fname} {sql_op} ({", ".join(formatted_vals)})')
            elif op_clean in ("==", "=", "!=", ">", ">=", "<", "<="):
                sql_op = "=" if op_clean in ("==", "=") else op_clean
                if f.value is None:
                    if sql_op == "=":
                        clauses.append(f'"{fname}" IS NULL')
                        applied_descriptions.append(f"{fname} IS NULL")
                    elif sql_op == "!=":
                        clauses.append(f'"{fname}" IS NOT NULL')
                        applied_descriptions.append(f"{fname} IS NOT NULL")
                    else:
                        raise HTTPException(status_code=400, detail=f"Operator '{f.op}' requires a value.")
                elif isinstance(f.value, bool):
                    clauses.append(f'"{fname}" {sql_op} {_sql_literal(f.value)}')
                    applied_descriptions.append(f"{fname} {sql_op} {f.value}")
                elif isinstance(f.value, (int, float)):
                    clauses.append(f'("{fname}" IS NOT NULL AND "{fname}" {sql_op} {_sql_literal(f.value)})')
                    applied_descriptions.append(f"{fname} {sql_op} {f.value}")
                else:
                    safe_str = str(f.value).replace("'", "''")
                    clauses.append(f'("{fname}" IS NOT NULL AND "{fname}" {sql_op} \'{safe_str}\')')
                    applied_descriptions.append(f"{fname} {sql_op} '{safe_str}'")
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported operator '{f.op}'.")

    if expression and expression.strip():
        expr_clean = _validate_expression(expression, valid_fields)
        clauses.append(f"({expr_clean})")
        applied_descriptions.append(expr_clean)

    where_sql = " AND ".join(clauses) if clauses else "1=1"
    return where_sql, applied_descriptions


@router.post("/scanner/universal", response_model=UniversalScannerResponse)
async def run_universal_scanner(req: UniversalScannerRequest):
    """Runs a multi-factor scan over Parquet data & Supabase fundamentals with dynamic filters."""
    t_start = time.perf_counter()

    # Resolve Supabase metadata first so the dynamic field catalog already contains every
    # business column that cda_master_universe currently exposes.
    metadata_map = await fetch_master_universe_metadata()
    dynamic_fields = get_dynamic_fields()
    valid_fields = {f["name"]: f["type"] for f in dynamic_fields}

    if req.help:
        return UniversalScannerResponse(
            status="help",
            count=0,
            total_evaluated=0,
            fields=dynamic_fields,
            records=[],
            matched_tickers=[],
            applied_filters=[],
        )

    # 1. Resolve Universe of Tickers (explicit tickers and watchlists are combined).
    target_tickers: List[str] = []
    if req.tickers:
        target_tickers.extend(t.strip().upper() for t in req.tickers if t and t.strip())

    if req.watchlists:
        from watchlists_api import resolve_watchlist_reference
        for w in req.watchlists:
            res = await resolve_watchlist_reference(w)
            target_tickers.extend(str(t).strip().upper() for t in (res.get("tickers") or []))

    # If neither tickers nor watchlists provided, default to the full available universe.
    if not target_tickers:
        if metadata_map:
            target_tickers = sorted(metadata_map.keys())
        elif PARQUET_BASE.is_dir():
            # Exclude the virtual '$STATS.*' bookkeeping series from the tradeable universe.
            target_tickers = sorted(
                d.name.upper()
                for d in PARQUET_BASE.iterdir()
                if d.is_dir() and not is_virtual_ticker(d.name.upper())
            )

    # Deduplicate while preserving order. Virtual '$'-prefixed bookkeeping series are dropped
    # for every source — including explicitly passed `tickers` and resolved watchlists, not only
    # the full-universe fallback above.
    seen = set()
    unique_tickers: List[str] = []
    for t in target_tickers:
        if t and not is_virtual_ticker(t) and t not in seen:
            seen.add(t)
            unique_tickers.append(t)

    t_after_universe = time.perf_counter()

    if not unique_tickers:
        t_tot = round(time.perf_counter() - t_start, 3)
        empty_watchlist = await _sync_watchlist([], req, evaluated_count=0, response_count=0)
        return UniversalScannerResponse(
            status="empty_universe",
            count=0,
            total_evaluated=0,
            records=[],
            matched_tickers=[],
            applied_filters=[],
            performance=PerformanceReport(
                total_seconds=t_tot,
                universe_resolve_seconds=round(t_after_universe - t_start, 3),
                data_loading_seconds=0.0,
                filter_evaluation_seconds=0.0,
                throughput_tickers_per_sec=0.0,
                tickers_evaluated=0,
                tickers_matched=0,
                summary=f"Empty universe in {t_tot:.2f}s",
            ),
            watchlist=ScanWatchlistInfo(**empty_watchlist),
        )

    # 2. Parallel Data Loading across Tickers
    records: List[Dict[str, Any]] = []
    max_workers = min(UNIVERSAL_SCANNER_WORKERS, max(1, len(unique_tickers)))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ticker = {
            executor.submit(load_ticker_record, t, metadata_map.get(t)): t
            for t in unique_tickers
        }
        for future in as_completed(future_to_ticker):
            rec = future.result()
            if rec:
                records.append(rec)

    t_after_loading = time.perf_counter()
    total_evaluated = len(records)
    if not records:
        t_tot = round(time.perf_counter() - t_start, 3)
        empty_watchlist = await _sync_watchlist([], req, evaluated_count=0, response_count=0)
        return UniversalScannerResponse(
            status="no_data",
            count=0,
            total_evaluated=0,
            records=[],
            matched_tickers=[],
            applied_filters=[],
            performance=PerformanceReport(
                total_seconds=t_tot,
                universe_resolve_seconds=round(t_after_universe - t_start, 3),
                data_loading_seconds=round(t_after_loading - t_after_universe, 3),
                filter_evaluation_seconds=0.0,
                throughput_tickers_per_sec=0.0,
                tickers_evaluated=0,
                tickers_matched=0,
                summary=f"No data in {t_tot:.2f}s",
            ),
            watchlist=ScanWatchlistInfo(**empty_watchlist),
        )

    # 3. Dynamic Filtering via DuckDB In-Memory Execution
    where_clause, applied_descriptions = _build_duckdb_where(req.filters, req.expression, valid_fields)

    # Validate sort field
    sort_by_clean = (req.sort_by or "dcr").strip()
    if sort_by_clean not in valid_fields:
        sort_by_clean = "dcr" if "dcr" in valid_fields else "close"

    direction_clean = (req.sort_direction or "desc").strip().lower()
    if direction_clean not in ("asc", "desc"):
        direction_clean = "desc"

    # Effective limit: user specified or default limit, capped at max limit
    eff_limit = req.limit if req.limit is not None else UNIVERSAL_SCANNER_DEFAULT_LIMIT
    eff_limit = max(1, min(UNIVERSAL_SCANNER_MAX_LIMIT, eff_limit))

    df_records = pd.DataFrame(records)
    db = duckdb.connect()
    all_match_tickers: List[str] = []
    try:
        db.register("stocks", df_records)
        base_query = (
            f"FROM stocks WHERE {where_clause} "
            f'ORDER BY "{sort_by_clean}" {direction_clean.upper()} NULLS LAST'
        )
        filtered_df = db.execute(f"SELECT * {base_query} LIMIT {eff_limit}").df()
        # Uncapped match set: the rolling auto-watchlist always holds ALL matches, while the API
        # response stays capped by 'limit' for token efficiency.
        all_match_tickers = [
            str(row[0]).strip().upper()
            for row in db.execute(f"SELECT ticker {base_query}").fetchall()
            if row and row[0]
        ]
    except Exception as e:
        logger.error("DuckDB evaluation failed: %s (Query: %s)", e, where_clause)
        raise HTTPException(status_code=400, detail=f"Filter evaluation error: {e}")
    finally:
        db.close()

    t_after_filter = time.perf_counter()

    result_records = filtered_df.to_dict(orient="records")
    # Clean up NaN to None for clean JSON serialization
    for r in result_records:
        for k, v in list(r.items()):
            if pd.isna(v):
                r[k] = None

    matched_tickers = [str(r["ticker"]) for r in result_records if "ticker" in r]

    t_tot = round(t_after_filter - t_start, 3)
    t_univ = round(t_after_universe - t_start, 3)
    t_load = round(t_after_loading - t_after_universe, 3)
    t_filt = round(t_after_filter - t_after_loading, 3)
    tps = round(total_evaluated / t_tot, 1) if t_tot > 0 else 0.0

    perf = PerformanceReport(
        total_seconds=t_tot,
        universe_resolve_seconds=t_univ,
        data_loading_seconds=t_load,
        filter_evaluation_seconds=t_filt,
        throughput_tickers_per_sec=tps,
        tickers_evaluated=total_evaluated,
        tickers_matched=len(result_records),
        summary=f"Total: {t_tot:.2f}s (Universe: {t_univ:.2f}s, Load: {t_load:.2f}s, Filter: {t_filt:.3f}s) | Throughput: {tps:,.0f} tickers/s",
    )
    logger.info("Universal Scanner Run: %s | Evaluated %d, Matched %d", perf.summary, total_evaluated, len(result_records))

    # 4. Persist the COMPLETE result set into the rolling auto-watchlist. The API response keeps
    #    its 'limit' cap; the watchlist never truncates, so a scan result is always fully available
    #    for chart viewing and follow-up questions.
    watchlist_info = await _sync_watchlist(
        all_match_tickers,
        req,
        evaluated_count=total_evaluated,
        response_count=len(result_records),
    )

    return UniversalScannerResponse(
        status="ok",
        count=len(result_records),
        total_evaluated=total_evaluated,
        records=result_records,
        matched_tickers=matched_tickers,
        applied_filters=applied_descriptions,
        performance=perf,
        watchlist=ScanWatchlistInfo(**watchlist_info),
    )
