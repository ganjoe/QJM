"""Orchestrator: Fetches data from PCA-Service, builds overlays, and pushes to the viewer.

This module is the single source of truth for turning a DISPLAY_STOCK request
(symbol + indicators/preset) into a full snapshot (bars + overlays + topbar).
"""

from __future__ import annotations
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

from chart_viewer import chart_spec
from chart_viewer.formatting import compact_de as _compact_de
from chart_viewer.formatting import de_num as _de_num
from chart_viewer.fundamentals import fundamental_topbar_parts
from chart_viewer.models.validation import sanitize_pane_scales

logger = logging.getLogger("chart_viewer.orchestrator")

# Kurze Topbar-Beschriftungen. Sie ueberschreiben den display_name der
# Feature-Registry NUR in der Info-Zeile - Scanner-Tabellen und Watchlist-Spalten
# behalten die ausgeschriebenen Registry-Namen.
TOPBAR_LABEL_OVERRIDES: Dict[str, str] = {
    "ibd_rs": "IBD-RS",
    "IBD RS Rating": "IBD-RS",
}


def _short_topbar_label(metric_col: str, resolved_col: str, registry_label: str) -> str:
    """Label der Registry, ersetzt durch die Kurzform, wenn eine hinterlegt ist."""
    for key in (metric_col, resolved_col, registry_label):
        short = TOPBAR_LABEL_OVERRIDES.get(key or "")
        if short:
            return short
    return registry_label

# PCA-Service base URL – configurable via environment variable
PCA_SERVICE_URL = os.environ.get("PCA_SERVICE_URL", "http://127.0.0.1:8794")

# Default candle limit for chart display (configurable via CV_CHART_LIMIT)
DEFAULT_CHART_LIMIT = int(os.environ.get("CV_CHART_LIMIT", "2000"))

# Timeout fuer die PCA-Service-Aufrufe des Renderpfads. Ohne Timeout konnte ein
# haengender Service (blockierter Event-Loop, laufender Scan) den kompletten
# Symbolwechsel blockieren - der Viewer wartete dann ewig auf seinen Snapshot.
PCA_HTTP_TIMEOUT_SEC = float(os.environ.get("CV_PCA_HTTP_TIMEOUT_SEC", "30"))


def _line_style(style: dict, default_color: str) -> dict:
    """Build an overlay line style from a preset style.

    A width is only carried through when the preset explicitly sets one, so the
    viewer's 1px default applies otherwise.
    """
    result = {"color": style.get("color", default_color)}
    if style.get("width") is not None:
        result["width"] = style["width"]
    return result


def _pca_get(path: str) -> Dict[str, Any]:
    """GET request to PCA-Service, returns parsed JSON."""
    url = f"{PCA_SERVICE_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=PCA_HTTP_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError) as e:
        logger.error("PCA-Service GET %s failed: %s", url, e)
        raise RuntimeError(f"PCA-Service unreachable at {url}: {e}") from e


def _pca_post(path: str, payload: dict) -> Dict[str, Any]:
    """POST request to PCA-Service, returns parsed JSON."""
    url = f"{PCA_SERVICE_URL}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=PCA_HTTP_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError) as e:
        logger.error("PCA-Service POST %s failed: %s", url, e)
        raise RuntimeError(f"PCA-Service unreachable at {url}: {e}") from e


def resolve_preset(preset_name: str) -> Dict[str, Any]:
    """Fetch a named preset from PCA-Service."""
    return _pca_get(f"/api/presets/{preset_name}")


def _pca_get_soft(path: str) -> Optional[Dict[str, Any]]:
    """GET auf den PCA-Service, das bei 404/Netzfehler None liefert.

    Damit bleibt der Altpfad (flache Presets) benutzbar, wenn die neue
    Chart-Ebene einen Namen nicht kennt oder der Service noch alt ist.
    """
    url = f"{PCA_SERVICE_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 - Fallback ist gewollt
        logger.info("PCA GET %s nicht verfuegbar (%s) - Fallback.", url, exc)
        return None


def load_chart_spec(chart_id: str) -> Optional[Dict[str, Any]]:
    """Chart-Definition (Panes + Pane-Presets) vom PCA-Service holen."""
    payload = _pca_get_soft(f"/api/charts/{urllib.parse.quote(str(chart_id), safe='')}")
    if not payload or "panes" not in payload:
        return None
    return chart_spec.normalize_chart_spec(payload)


def build_inline_chart_spec(
    panes: List[Dict[str, Any]],
    topbar_metrics: Optional[List[str]] = None,
    chart_meta: Optional[Dict[str, Any]] = None,
    skipped: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Ad-hoc-Zusammenstellung (Builder-Draft) in eine Chart-Definition uebersetzen.

    Unbekannte Pane-Presets werden uebersprungen und - wenn eine Liste
    hereingereicht wird - als 'skipped' gemeldet, statt nur im Log zu landen.
    """
    resolved = []
    for entry in panes or []:
        preset_id = str((entry or {}).get("pane_preset_id") or "").strip()
        if not preset_id:
            continue
        preset = chart_spec.builtin_preset(preset_id) or _pca_get_soft(
            f"/api/panes/{urllib.parse.quote(preset_id, safe='')}"
        )
        if not preset:
            logger.warning("Pane-Preset '%s' unbekannt - uebersprungen.", preset_id)
            if skipped is not None:
                skipped.append({"pane_preset_id": preset_id, "reason": "unknown_preset"})
            continue
        resolved.append(
            {
                "pane_id": (entry or {}).get("pane_id"),
                "pane_preset_id": preset_id,
                "scale": (entry or {}).get("scale") or preset.get("default_scale") or "linear",
                "weight": (entry or {}).get("weight"),
                "overrides": (entry or {}).get("overrides") or {},
                "preset": preset,
            }
        )
    meta = dict(chart_meta or {})
    meta["panes"] = resolved
    meta.setdefault("id", None)
    meta.setdefault("display_name", "Draft")
    if topbar_metrics is not None:
        meta["topbar_metrics"] = topbar_metrics
    return chart_spec.normalize_chart_spec(meta, draft=bool(meta.get("draft", True)))


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


def _format_metric_value(col_name: str, calc_type: Optional[str], value: Any) -> str:
    """Topbar-Wert typabhaengig formatieren.

    Die Registry kennt kein Einheiten-Feld, daher wird aus calc_type und
    Spaltenname abgeleitet: Volumen als kompakte Waehrung, ADR/_pct als Prozent,
    Ratings/Scores/Zaehler ganzzahlig, alles andere mit zwei Nachkommastellen.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)

    name = (col_name or "").lower()
    ctype = (calc_type or "").upper()

    if "dollar_volume" in name or "volume" in name:
        # Stueckzahl und Umsatz in derselben k/M/G/T-Notation wie die Achsen.
        return _compact_de(num, decimals=3)
    if ctype.startswith("ADR") or name.endswith("_pct"):
        return f"{_de_num(num)} %"
    # Ratings, Scores und Stueckzahlen (z. B. breadth_minervini ist eine Anzahl)
    if ctype in ("IBD_RS", "MINERVINI_TREND", "BREADTH_MINERVINI", "DAYS_BACK") \
            or name in ("ibd_rs", "minervini"):
        return str(int(round(num)))
    return _de_num(num)


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
    chart: Optional[str] = None,
    panes: Optional[List[Dict[str, Any]]] = None,
    chart_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a complete OPEN_WINDOW + snapshot payload for a stock.

    This is the core orchestration function that:
    1. Resolves preset -> indicator list (if preset given)
    2. Fetches chart data with pre-calculated features from Parquet
    3. Matches requested indicators against available Parquet columns
    4. Falls back to on-the-fly calculation for missing indicators
    5. Builds overlay objects with user-specified styles
    6. Builds topbar content from metric columns + fundamental universe metadata

    Returns a dict ready to be used as an OPEN_WINDOW command payload.
    """
    symbol = symbol.upper()

    # 1. Chart-Definition (Pane-Ebene) aufloesen. Ist sie vorhanden, kommen
    #    Panes, Skalen und Topbar ausschliesslich aus ihr; der flache Altpfad
    #    (Preset -> Indikatorliste) wird stillgelegt.
    pane_scales: Dict[str, str] = {}
    spec: Optional[Dict[str, Any]] = None
    skipped: List[Dict[str, Any]] = []
    if panes is not None:
        spec = build_inline_chart_spec(
            panes,
            topbar_metrics=topbar_metrics,
            chart_meta=chart_meta,
            skipped=skipped,
        )
        topbar_metrics = spec.get("topbar_metrics") or []
    elif chart:
        spec = load_chart_spec(chart)
    elif preset and not indicators:
        # Explizit mitgeschickte Indikatoren haben weiter Vorrang vor einem Preset.
        spec = load_chart_spec(preset)

    if spec is not None:
        indicators = []
        if topbar_metrics is None:
            topbar_metrics = spec.get("topbar_metrics") or []
    elif preset and not indicators:
        preset_data = resolve_preset(preset)
        indicators = preset_data.get("indicators", [])
        # Y-Skalierung je Pane (linear/log) aus dem Preset uebernehmen
        pane_scales = sanitize_pane_scales(preset_data.get("pane_scales"))
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
    label_map: Dict[str, tuple] = {}   # canonical_id/alias -> (display_name, calc_type)
    try:
        registry = _pca_get("/api/features/registry")
        for f in registry.get("features", []):
            cid = f.get("canonical_id", "")
            alias = f.get("alias", "")
            if cid and alias and cid != alias:
                alias_map[cid] = alias
            # Label + calc_type je Feature, adressierbar ueber canonical_id UND alias
            if cid:
                label_map[cid] = (f.get("display_name") or cid, f.get("calc_type"))
            if alias:
                label_map[alias] = (f.get("display_name") or alias, f.get("calc_type"))
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
            is_up = float(r[col_idx["close"]]) >= float(r[col_idx["open"]])
            color = "#3877FF" if is_up else "#E040FB"  # TC2000 default
            vol_values.append({
                "t": int(r[col_idx["timestamp"]]),
                "value": float(vol),
                "color_override": color,
            })
    overlays = [{
        "overlay_id": "volume",
        "type": "histogram",
        "style": {"color": "#546E7A", "alpha": 60},  # Base color fallback
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
    otf_panes = {}   # result_col -> pane (nur wenn vom Preset vorgegeben)

    for ind in indicators:
        col = ind.get("column", "")
        style = ind.get("style", {})

        # Ziel-Pane: 'main' = Chartfenster-Overlay, sonst eigene Subpane.
        # 'none' = reine Topbar-Metrik -> kein Overlay zeichnen.
        pane_target = ind.get("pane") or "main"
        if str(pane_target).strip().lower() == "none":
            continue
        pane_target = str(pane_target).strip().lower()

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
                        "pane": pane_target,
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
                "pane": pane_target,
                "origin": ind.get("origin", "bottom"),
            })
        else:
            # Column not in Parquet -> queue for on-the-fly calculation.
            # Achtung: eigener Name, 'spec' gehoert der Chart-Ebene.
            otf_spec = _column_to_indicator_spec(col)
            if otf_spec:
                if otf_spec["indicator_type"] == "SMA":
                    otf_sma_periods.append(otf_spec["period"])
                    otf_sma_styles[otf_spec["result_col"]] = style
                    otf_panes[otf_spec["result_col"]] = pane_target
                elif otf_spec["indicator_type"] == "EMA":
                    otf_ema_periods.append(otf_spec["period"])
                    otf_ema_styles[otf_spec["result_col"]] = style
                    otf_panes[otf_spec["result_col"]] = pane_target
                elif otf_spec["indicator_type"] == "BOLLINGER":
                    otf_bb_periods.append(otf_spec["period"])
                    otf_bb_styles[otf_spec["period"]] = style
                elif otf_spec["indicator_type"] == "ADR_PCT":
                    otf_adr_pct_periods.append(otf_spec["period"])
                    otf_adr_pct_styles[otf_spec["result_col"]] = style
                    otf_panes[otf_spec["result_col"]] = pane_target
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
                    "pane": otf_panes.get(col_name, "adr"),
                    "origin": "bottom",
                })
        except Exception as e:
            logger.error("On-the-fly ADR_PCT calculation failed: %s", e)
    if otf_sma_periods:
        try:
            result = calculate_indicators_on_the_fly(symbol, "SMA", otf_sma_periods, timeframe, limit)
            timestamps = result.get("timestamps", [])
            for col_name, values in result.get("series", {}).items():
                style = otf_sma_styles.get(col_name, {"color": "#2962FF"})
                line_values = [{"t": ts, "value": v} for ts, v in zip(timestamps, values) if v is not None]
                overlays.append({
                    "overlay_id": f"ma_{col_name}",
                    "type": "line",
                    "style": _line_style(style, "#2962FF"),
                    "values": line_values,
                })
        except Exception as e:
            logger.error("On-the-fly SMA calculation failed: %s", e)

    if otf_ema_periods:
        try:
            result = calculate_indicators_on_the_fly(symbol, "EMA", otf_ema_periods, timeframe, limit)
            timestamps = result.get("timestamps", [])
            for col_name, values in result.get("series", {}).items():
                style = otf_ema_styles.get(col_name, {"color": "#FF6D00"})
                line_values = [{"t": ts, "value": v} for ts, v in zip(timestamps, values) if v is not None]
                overlays.append({
                    "overlay_id": f"ma_{col_name}",
                    "type": "line",
                    "style": _line_style(style, "#FF6D00"),
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

    # 5b. Pane-Ebene: Overlays/Gewichte/Skalen aus der Chart-Definition
    chart_panes: List[Dict[str, Any]] = []
    chart_info: Dict[str, Any] = {}
    x_axis_pane: Optional[str] = None
    pane_notice = ""
    warnings: List[Dict[str, Any]] = []
    if spec is not None:
        built = chart_spec.build_overlays(
            spec,
            bars,
            rows,
            col_idx,
            resolve_column,
            calculate_indicators_on_the_fly,
            symbol,
            timeframe,
            limit,
        )
        overlays = built["overlays"]
        pane_scales = built["pane_scales"]
        chart_panes = built["panes"]
        x_axis_pane = spec.get("x_axis_pane")
        chart_info = {
            "id": spec.get("id"),
            "display_name": spec.get("display_name"),
            "draft": bool(spec.get("draft")),
        }
        warnings = built.get("warnings") or []
        if built.get("missing"):
            pane_notice = " | ⚠ fehlt: " + ", ".join(dict.fromkeys(built["missing"]))

    # 6. Build topbar content from metric columns
    topbar_parts = []
    last_row = rows[-1] if rows else []
    for metric_col in topbar_metrics:
        resolved_metric = resolve_column(metric_col)
        if resolved_metric:
            val = last_row[col_idx[resolved_metric]]
            if val is not None:
                # Label aus der Feature-Registry (display_name), sonst Fallback
                label, ctype = label_map.get(
                    metric_col,
                    label_map.get(resolved_metric, (metric_col.replace("_", " ").title(), None)),
                )
                label = _short_topbar_label(metric_col, resolved_metric, label)
                topbar_parts.append(
                    f"{label}: {_format_metric_value(resolved_metric, ctype, val)}"
                )
        elif metric_col == "days_back":
            try:
                source_col = "breadth_40_pct" if "breadth_40_pct" in col_idx else "close"
                db_res = _pca_post("/api/indicators/calculate", {
                    "symbol": symbol,
                    "indicator_type": "DAYS_BACK",
                    "source": source_col,
                    "limit": 1,
                })
                latest_db = db_res.get("latest_values", {}).get("days_back")
                if latest_db is not None:
                    sign = "+" if latest_db > 0 else ""
                    label = label_map.get("days_back", ("Days Back", None))[0]
                    topbar_parts.append(f"{label}: {sign}{int(latest_db)}")
            except Exception as e:
                logger.warning("Could not calculate live days_back for topbar: %s", e)

    last_close = bars[-1]["close"] if bars else 0
    fundamental_parts = fundamental_topbar_parts(symbol, last_close)

    topbar_content = f"{symbol} | Last: ${last_close:.2f}"
    if topbar_parts:
        topbar_content += " | " + " | ".join(topbar_parts)
    if fundamental_parts:
        topbar_content += " | " + " | ".join(fundamental_parts)
    # "Bars" schliesst die Zeile ab; die Overlay-Anzahl stand hier frueher auch,
    # ist fuer die Chartarbeit aber ohne Aussage und hat die Zeile nur verlaengert.
    topbar_content += f" | Bars: {len(bars)}"
    if pane_notice:
        topbar_content += pane_notice

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
        # Geometrie nur durchreichen, wenn sie ausdruecklich angefordert wurde.
        # Der Renderer erfindet keine Position/Groesse: ein Re-Render (Chart
        # anwenden, Symbolwechsel, Compose) darf kein Fenster verschieben - sie
        # gehoert dem Client. Startwerte setzt der Agent nur beim ANLEGEN.
        "position": position,
        "size": size,
        "bars": bars,
        "overlays": overlays,
        "annotations": [],
        # Linear/Log je Pane; der Viewer wendet das beim Snapshot an und
        # meldet Aenderungen (LOG/LIN-Taste) ins Preset zurueck.
        "pane_scales": pane_scales,
        # Pane-Ebene (Vertrag docs/architecture/chart-presets.md): Rolle, Titel,
        # Gewicht und Skala je Pane; chart = Name/Draft-Status des Fensterinhalts.
        "panes": chart_panes,
        "chart": chart_info,
        "x_axis_pane": x_axis_pane,
        # Aufgeloeste Topbar-Metriken (der Server legt sie im Ledger ab, damit ein
        # Symbolwechsel die Chart-Definition ohne Preset-Abruf neu rendern kann).
        "topbar_metrics": list(topbar_metrics or []),
        "topbar": {
            "block_id": "info_block",
            "content": topbar_content,
        },
        # Warnkanal (Plan Phase 4): sichtbar machen, was fehlt oder uebersprungen wurde.
        "warnings": warnings,
        "skipped": skipped,
    }

    logger.info(
        "Built DISPLAY_STOCK for %s: %d bars, %d overlays (%d from Parquet, %d on-the-fly)",
        symbol, len(bars), len(overlays),
        len(overlays) - len(otf_sma_periods) - len(otf_ema_periods) - len(otf_bb_periods),
        len(otf_sma_periods) + len(otf_ema_periods) + len(otf_bb_periods),
    )
    return result
