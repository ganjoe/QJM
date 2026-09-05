"""Orchestrator: Fetches data from PCA-Service, builds overlays, and pushes to the viewer.

This module is the single source of truth for turning a DISPLAY_STOCK request
(symbol + indicators/preset) into a full snapshot (bars + overlays + topbar).
"""

from __future__ import annotations
import json
import logging
import os
import re
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger("chart_viewer.orchestrator")

# PCA-Service base URL – configurable via environment variable
PCA_SERVICE_URL = os.environ.get("PCA_SERVICE_URL", "http://127.0.0.1:8794")

# Default candle limit for chart display
DEFAULT_CHART_LIMIT = int(os.environ.get("CV_CHART_LIMIT", "300"))


def _pca_get(path: str) -> Dict[str, Any]:
    """GET request to PCA-Service, returns parsed JSON."""
    url = f"{PCA_SERVICE_URL}{path}"
    try:
        with urllib.request.urlopen(url) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        logger.error("PCA-Service GET %s failed: %s", url, e)
        raise RuntimeError(f"PCA-Service unreachable at {url}: {e}") from e


def _pca_post(path: str, payload: dict) -> Dict[str, Any]:
    """POST request to PCA-Service, returns parsed JSON."""
    url = f"{PCA_SERVICE_URL}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        logger.error("PCA-Service POST %s failed: %s", url, e)
        raise RuntimeError(f"PCA-Service unreachable at {url}: {e}") from e


def resolve_preset(preset_name: str) -> Dict[str, Any]:
    """Fetch a named preset from PCA-Service."""
    return _pca_get(f"/api/presets/{preset_name}")


def fetch_chart_data(symbol: str, timeframe: str = "1D", limit: int = DEFAULT_CHART_LIMIT) -> Dict[str, Any]:
    """Fetch OHLCV + precalculated features from PCA-Service."""
    return _pca_get(f"/api/chartdata?symbol={symbol}&timeframe={timeframe}&limit={limit}&features=true")


def calculate_indicators_on_the_fly(
    symbol: str,
    indicator_type: str,
    periods: List[int],
    timeframe: str = "1D",
    limit: int = DEFAULT_CHART_LIMIT,
) -> Dict[str, Any]:
    """Request on-the-fly indicator calculation from PCA-Service."""
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "limit": limit,
        "indicator_type": indicator_type,
        "periods": periods,
    }
    return _pca_post("/api/indicators/calculate", payload)


def _column_to_indicator_spec(column: str) -> Optional[Dict[str, Any]]:
    """Parse a column name like 'ma_sma_50' or 'sma_50' into an on-the-fly calculation spec.

    Returns: {"indicator_type": "SMA", "period": 50} or None if not parseable.
    """
    sma_match = re.match(r"^(?:ma_)?sma_(\d+)$", column)
    if sma_match:
        return {"indicator_type": "SMA", "period": int(sma_match.group(1)), "result_col": f"sma_{sma_match.group(1)}"}

    ema_match = re.match(r"^(?:ma_)?ema_(\d+)$", column)
    if ema_match:
        return {"indicator_type": "EMA", "period": int(ema_match.group(1)), "result_col": f"ema_{ema_match.group(1)}"}

    adr_pct_match = re.match(r"^adr_(\d+)_pct$", column)
    if adr_pct_match:
        return {"indicator_type": "ADR_PCT", "period": int(adr_pct_match.group(1)), "result_col": f"adr_{adr_pct_match.group(1)}_pct"}

    adr_sma_match = re.match(r"^adr_(\d+)_sma$", column)
    if adr_sma_match:
        return {"indicator_type": "ADR_PCT", "period": int(adr_sma_match.group(1)), "result_col": f"adr_{adr_sma_match.group(1)}_pct"}

    bb_match = re.match(r"^bb_(\d+)(?:_upper)?$", column)
    if bb_match:
        return {"indicator_type": "BOLLINGER", "period": int(bb_match.group(1)),
                "result_col_upper": f"bb_{bb_match.group(1)}_upper",
                "result_col_lower": f"bb_{bb_match.group(1)}_lower"}

    return None


def build_display_stock(
    symbol: str,
    indicators: Optional[List[Dict[str, Any]]] = None,
    preset: Optional[str] = None,
    timeframe: str = "1D",
    limit: int = DEFAULT_CHART_LIMIT,
    position: Optional[Dict[str, int]] = None,
    size: Optional[Dict[str, int]] = None,
    topbar_metrics: Optional[List[str]] = None,
    window_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a complete OPEN_WINDOW + snapshot payload for a stock.

    This is the core orchestration function that:
    1. Resolves preset -> indicator list (if preset given)
    2. Fetches chart data with pre-calculated features from Parquet
    3. Matches requested indicators against available Parquet columns
    4. Falls back to on-the-fly calculation for missing indicators
    5. Builds overlay objects with user-specified styles
    6. Builds topbar content from metric columns

    Returns a dict ready to be used as an OPEN_WINDOW command payload.
    """
    symbol = symbol.upper()

    # 1. Resolve preset if given
    if preset and not indicators:
        preset_data = resolve_preset(preset)
        indicators = preset_data.get("indicators", [])
        if topbar_metrics is None:
            topbar_metrics = preset_data.get("topbar_metrics", [])

    if indicators is None:
        indicators = []
    if topbar_metrics is None:
        topbar_metrics = []

    # 2. Fetch chart data with features
    chart_data = fetch_chart_data(symbol, timeframe, limit)
    if chart_data.get("status") != "ok" or not chart_data.get("data"):
        raise RuntimeError(f"No chart data available for {symbol}: {chart_data.get('notice', 'unknown error')}")

    columns = chart_data.get("columns", [])
    rows = chart_data.get("data", [])
    col_idx = {name: i for i, name in enumerate(columns)}

    # Build alias lookup: canonical_id → old Parquet column name (for transition period)
    alias_map = {}
    try:
        registry = _pca_get("/api/features/registry")
        for f in registry.get("features", []):
            cid = f.get("canonical_id", "")
            alias = f.get("alias", "")
            if cid and alias and cid != alias:
                alias_map[cid] = alias
    except Exception:
        pass  # Registry unavailable, proceed without alias resolution

    def resolve_column(col: str) -> Optional[str]:
        """Resolve a column name against available Parquet columns, trying alias as fallback."""
        if col in col_idx:
            return col
        alias = alias_map.get(col)
        if alias and alias in col_idx:
            return alias
        return None

    # 3. Build bars
    bars = []
    for r in rows:
        t = int(r[col_idx["timestamp"]])
        bars.append({
            "t_open": t,
            "t_close": t + 86400,
            "open": float(r[col_idx["open"]]),
            "high": float(r[col_idx["high"]]),
            "low": float(r[col_idx["low"]]),
            "close": float(r[col_idx["close"]]),
            "volume": float(r[col_idx["volume"]] or 0),
        })

    # 4. Build volume overlay
    vol_values = []
    for r in rows:
        vol = r[col_idx["volume"]]
        if vol is not None:
            vol_values.append({
                "t": int(r[col_idx["timestamp"]]),
                "value": float(vol),
            })
    overlays = [{
        "overlay_id": "volume",
        "type": "histogram",
        "style": {"color": "#546E7A", "alpha": 60},
        "values": vol_values,
        "pane": "volume",
        "origin": "bottom",
    }]

    # 5. Build overlays from indicators

    # Group on-the-fly requests by indicator_type for batch calculation
    otf_sma_periods = []
    otf_ema_periods = []
    otf_bb_periods = []
    otf_adr_pct_periods = []
    otf_sma_styles = {}
    otf_ema_styles = {}
    otf_bb_styles = {}
    otf_adr_pct_styles = {}

    for ind in indicators:
        col = ind.get("column", "")
        style = ind.get("style", {})

        # Resolve column name (canonical_id → alias fallback for old Parquet names)
        resolved = resolve_column(col)

        if resolved:
            actual_col = resolved
            # Check if it's a band (bb_*_upper with a paired lower)
            bb_upper_match = re.match(r"^bb_(\d+)_upper$", col)
            if bb_upper_match:
                lower_col_name = f"bb_{bb_upper_match.group(1)}_lower"
                lower_resolved = resolve_column(lower_col_name) or lower_col_name
                if lower_resolved in col_idx:
                    band_values = []
                    for r in rows:
                        upper_val = r[col_idx[actual_col]]
                        lower_val = r[col_idx[lower_resolved]]
                        if upper_val is not None and lower_val is not None:
                            band_values.append({
                                "t": int(r[col_idx["timestamp"]]),
                                "value": float(upper_val),
                                "value2": float(lower_val),
                            })
                    overlays.append({
                        "overlay_id": col.replace("_upper", ""),
                        "type": "band",
                        "style": {"color": style.get("color", "#26A69A"), "alpha": style.get("alpha", 30)},
                        "values": band_values,
                        "pane": ind.get("pane", "main"),
                    })
                continue

            # Skip lower band columns (handled by upper)
            if re.match(r"^bb_\d+_lower$", col):
                continue

            # Regular line overlay (SMA, EMA, or any single-value column)
            line_values = []
            for r in rows:
                val = r[col_idx[actual_col]]
                if val is not None:
                    line_values.append({
                        "t": int(r[col_idx["timestamp"]]),
                        "value": float(val),
                    })
            overlays.append({
                "overlay_id": col,
                "type": style.get("type", "line"),
                "style": style,
                "values": line_values,
                "pane": ind.get("pane", "main"),
                "origin": ind.get("origin", "bottom"),
            })
        else:
            # Column not in Parquet -> queue for on-the-fly calculation
            spec = _column_to_indicator_spec(col)
            if spec:
                if spec["indicator_type"] == "SMA":
                    otf_sma_periods.append(spec["period"])
                    otf_sma_styles[spec["result_col"]] = style
                elif spec["indicator_type"] == "EMA":
                    otf_ema_periods.append(spec["period"])
                    otf_ema_styles[spec["result_col"]] = style
                elif spec["indicator_type"] == "BOLLINGER":
                    otf_bb_periods.append(spec["period"])
                    otf_bb_styles[spec["period"]] = style
                elif spec["indicator_type"] == "ADR_PCT":
                    otf_adr_pct_periods.append(spec["period"])
                    otf_adr_pct_styles[spec["result_col"]] = style
            else:
                logger.warning("Indicator column '%s' not in Parquet and not calculable on-the-fly, skipping.", col)

    # 5. Execute batched on-the-fly calculations
    if otf_adr_pct_periods:
        try:
            result = calculate_indicators_on_the_fly(symbol, "ADR_PCT", otf_adr_pct_periods, timeframe, limit)
            timestamps = result.get("timestamps", [])
            for col_name, values in result.get("series", {}).items():
                style = otf_adr_pct_styles.get(col_name, {"color": "#B39DDB"})
                line_values = [{"t": ts, "value": v} for ts, v in zip(timestamps, values) if v is not None]
                overlays.append({
                    "overlay_id": col_name,
                    "type": style.get("type", "line"),
                    "style": style,
                    "values": line_values,
                    "pane": "adr",
                    "origin": "bottom",
                })
        except Exception as e:
            logger.error("On-the-fly ADR_PCT calculation failed: %s", e)
    if otf_sma_periods:
        try:
            result = calculate_indicators_on_the_fly(symbol, "SMA", otf_sma_periods, timeframe, limit)
            timestamps = result.get("timestamps", [])
            for col_name, values in result.get("series", {}).items():
                style = otf_sma_styles.get(col_name, {"color": "#2962FF", "width": 2})
                line_values = [{"t": ts, "value": v} for ts, v in zip(timestamps, values) if v is not None]
                overlays.append({
                    "overlay_id": f"ma_{col_name}",
                    "type": "line",
                    "style": {"color": style.get("color", "#2962FF"), "width": style.get("width", 2)},
                    "values": line_values,
                })
        except Exception as e:
            logger.error("On-the-fly SMA calculation failed: %s", e)

    if otf_ema_periods:
        try:
            result = calculate_indicators_on_the_fly(symbol, "EMA", otf_ema_periods, timeframe, limit)
            timestamps = result.get("timestamps", [])
            for col_name, values in result.get("series", {}).items():
                style = otf_ema_styles.get(col_name, {"color": "#FF6D00", "width": 2})
                line_values = [{"t": ts, "value": v} for ts, v in zip(timestamps, values) if v is not None]
                overlays.append({
                    "overlay_id": f"ma_{col_name}",
                    "type": "line",
                    "style": {"color": style.get("color", "#FF6D00"), "width": style.get("width", 2)},
                    "values": line_values,
                })
        except Exception as e:
            logger.error("On-the-fly EMA calculation failed: %s", e)

    if otf_bb_periods:
        try:
            result = calculate_indicators_on_the_fly(symbol, "BOLLINGER", otf_bb_periods, timeframe, limit)
            timestamps = result.get("timestamps", [])
            series = result.get("series", {})
            for period in otf_bb_periods:
                upper_key = f"bb_{period}_upper"
                lower_key = f"bb_{period}_lower"
                if upper_key in series and lower_key in series:
                    style = otf_bb_styles.get(period, {"color": "#26A69A", "alpha": 30})
                    band_values = [
                        {"t": ts, "value": u, "value2": l}
                        for ts, u, l in zip(timestamps, series[upper_key], series[lower_key])
                        if u is not None and l is not None
                    ]
                    overlays.append({
                        "overlay_id": f"bb_{period}",
                        "type": "band",
                        "style": {"color": style.get("color", "#26A69A"), "alpha": style.get("alpha", 30)},
                        "values": band_values,
                    })
        except Exception as e:
            logger.error("On-the-fly Bollinger calculation failed: %s", e)

    # 6. Build topbar content from metric columns
    topbar_parts = []
    last_row = rows[-1] if rows else []
    for metric_col in topbar_metrics:
        resolved_metric = resolve_column(metric_col)
        if resolved_metric:
            val = last_row[col_idx[resolved_metric]]
            if val is not None:
                display_name = metric_col.replace("_", " ").title()
                if isinstance(val, float):
                    topbar_parts.append(f"{display_name}: {val:.2f}")
                else:
                    topbar_parts.append(f"{display_name}: {val}")

    last_close = bars[-1]["close"] if bars else 0
    topbar_content = f"{symbol} | Last: ${last_close:.2f}"
    if topbar_parts:
        topbar_content += " | " + " | ".join(topbar_parts)
    topbar_content += f" | Bars: {len(bars)} | Overlays: {len(overlays)}"

    # Add staleness notice if applicable
    if chart_data.get("features_stale") and chart_data.get("notice"):
        topbar_content += f" | {chart_data['notice']}"

    # 7. Assemble final command payload
    win_id = window_id or f"win_{symbol.lower()}_1d"
    result = {
        "action": "OPEN_WINDOW",
        "window_id": win_id,
        "symbol": symbol,
        "timeframe": {"unit": "D", "multiplier": 1},
        "sync_group_id": "stocks",
        "position": position or {"x": 100, "y": 100},
        "size": size or {"width": 1100, "height": 750},
        "bars": bars,
        "overlays": overlays,
        "annotations": [],
        "topbar": {
            "block_id": "info_block",
            "content": topbar_content,
        },
    }

    logger.info(
        "Built DISPLAY_STOCK for %s: %d bars, %d overlays (%d from Parquet, %d on-the-fly)",
        symbol, len(bars), len(overlays),
        len(overlays) - len(otf_sma_periods) - len(otf_ema_periods) - len(otf_bb_periods),
        len(otf_sma_periods) + len(otf_ema_periods) + len(otf_bb_periods),
    )
    return result
