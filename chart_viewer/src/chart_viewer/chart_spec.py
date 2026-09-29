"""Chart-Definition (Pane-Presets) in Panes, Overlays und Zeichenregeln aufloesen.

Vertrag: docs/architecture/chart-presets.md

Dieses Modul ist netzwerkfrei: es bekommt die Chart-Daten (Zeilen + Spalten),
einen Spaltenaufloeser und eine Rechenfunktion fuer Indikatoren hereingereicht.
Damit ist die Aufloesung ohne laufenden PCA-Service testbar.

Zeichenprimitive, die hier entstehen:
  line / band / histogram   - Serien; Farbschwellen als color_override je Punkt
  zone                      - Datenraum-Rechteck (t..t2 x value..value2)
  level                     - horizontale Referenzlinie mit Label
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("chart_viewer.chart_spec")

VOLUME_PRESET = "builtin:volume"
CANDLES_PRESET = "builtin:candles"

BUILTIN_PRESETS: Dict[str, dict] = {
    VOLUME_PRESET: {
        "id": VOLUME_PRESET, "display_name": "Volumen", "role": "volume", "kind": "builtin",
        "series": [], "refs": [], "derives": [], "zones": [],
    },
    CANDLES_PRESET: {
        "id": CANDLES_PRESET, "display_name": "Kerzen", "role": "price", "kind": "builtin",
        "series": [], "refs": [], "derives": [], "zones": [],
    },
}

# Zeichenreihenfolge im Pane (Zonen hinten, Linien vorne).
_Z_ORDER = {"zone": 0, "band": 1, "histogram": 2, "level": 3, "line": 4, "marker": 5}


def builtin_preset(preset_id: str) -> Optional[dict]:
    return BUILTIN_PRESETS.get(preset_id)


# Whitelist der Pane-Overrides (Chart-Ebene). Vertrag: docs/architecture/chart-presets.md.
_OVERRIDE_KEYS = frozenset({"title", "scale", "series", "hide_series", "refs", "zones"})
_SERIES_STYLE_KEYS = frozenset({"color", "width", "alpha", "type"})


def _series_key(series: Dict[str, Any]) -> str:
    return str(series.get("canonical_id") or series.get("feature_id") or series.get("column") or "")


def apply_pane_overrides(
    preset: Dict[str, Any],
    overrides: Any,
    pane_id: str = "",
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Pane-Overrides auf ein Preset anwenden (Whitelist, Deep-Merge).

    Erlaubt sind:
      title        - Pane-Titel
      scale        - "linear" | "log" (Default-Skala des Panes)
      series       - {canonical_id: {style: {...}, rules: {...}}}
      hide_series  - [canonical_id]
      refs         - ersetzt die Referenzlinien
      zones        - ersetzt die Zonen

    Unbekannte Schluessel und unbekannte Serien werden als Warnung gemeldet,
    nicht still verworfen. Das Preset selbst bleibt unveraendert (deepcopy).
    """
    warnings: List[Dict[str, Any]] = []
    if not isinstance(overrides, dict) or not overrides:
        return preset, warnings

    merged = copy.deepcopy(preset or {})
    hide: List[str] = []

    for key, value in overrides.items():
        if key not in _OVERRIDE_KEYS:
            _add_warning(warnings, "unknown_override", str(key), pane_id)
            continue
        if key == "title":
            if value:
                merged["display_name"] = str(value)
        elif key == "scale":
            scale = str(value or "").strip().lower()
            if scale in ("linear", "log"):
                merged["default_scale"] = scale
            else:
                _add_warning(warnings, "invalid_override", "scale=" + str(value), pane_id)
        elif key == "refs":
            merged["refs"] = list(value) if isinstance(value, list) else []
        elif key == "zones":
            merged["zones"] = list(value) if isinstance(value, list) else []
        elif key == "hide_series":
            wanted = [str(item) for item in (value or [])]
            known = {_series_key(s) for s in (merged.get("series") or [])}
            hide = wanted
            for missing in [item for item in wanted if item not in known]:
                _add_warning(warnings, "unknown_series_override", missing, pane_id)
        elif key == "series":
            if not isinstance(value, dict):
                _add_warning(warnings, "invalid_override", "series", pane_id)
                continue
            for raw_key, spec in value.items():
                target = next(
                    (s for s in (merged.get("series") or []) if _series_key(s) == str(raw_key)),
                    None,
                )
                if target is None:
                    _add_warning(warnings, "unknown_series_override", str(raw_key), pane_id)
                    continue
                if not isinstance(spec, dict):
                    _add_warning(warnings, "invalid_override", "series." + str(raw_key), pane_id)
                    continue
                for sub_key, sub_value in spec.items():
                    if sub_key == "style":
                        style = target.setdefault("style", {})
                        for style_key, style_value in (sub_value or {}).items():
                            if style_key not in _SERIES_STYLE_KEYS:
                                _add_warning(
                                    warnings, "unknown_override", "style." + str(style_key), pane_id
                                )
                                continue
                            style[style_key] = style_value
                    elif sub_key == "rules":
                        if isinstance(sub_value, dict):
                            target.setdefault("rules", {}).update(sub_value)
                        else:
                            _add_warning(warnings, "invalid_override", "rules", pane_id)
                    else:
                        _add_warning(warnings, "unknown_override", "series." + str(sub_key), pane_id)

    if hide:
        merged["series"] = [s for s in (merged.get("series") or []) if _series_key(s) not in hide]
    return merged, warnings


def normalize_chart_spec(chart: Dict[str, Any], draft: bool = False) -> Dict[str, Any]:
    """Panes eines Charts vereinheitlichen.

    Regeln (Vertrag, chart-presets.md Abschnitt 1): genau ein Preispane auf Slot
    main und zuerst; fehlt es, wird das erste Pane mit role=price genommen, sonst
    ein Kerzen-Pane ergaenzt. Slot-Ids sind eindeutig (Kollision => Suffix _2).

    Die Pane-Liste der Definition ist VOLLSTAENDIG: es wird kein Volumen-Pane
    ergaenzt. Ein Volumen-Pane ist eine normale Pane mit role=volume; wer sie
    entfernt, bekommt sie beim naechsten Render nicht zurueck.
    """
    panes: List[Dict[str, Any]] = []
    override_warnings: List[Dict[str, Any]] = []
    for entry in chart.get("panes") or []:
        preset = entry.get("preset")
        if not preset:
            continue
        overrides = entry.get("overrides") if isinstance(entry.get("overrides"), dict) else {}
        pane_key = str(entry.get("pane_id") or preset.get("id") or "")
        merged_preset, pane_warnings = apply_pane_overrides(preset, overrides, pane_key)
        override_warnings.extend(pane_warnings)
        panes.append(
            {
                "pane_id": entry.get("pane_id"),
                "pane_preset_id": entry.get("pane_preset_id") or preset.get("id"),
                "scale": str(entry.get("scale") or merged_preset.get("default_scale") or "linear"),
                "weight": int(entry.get("weight") or 0) or None,
                # Rohdaten bleiben erhalten: sie sind der Vertrag der Chart-Ebene und
                # gehen so durch Speichern/Laden (presets_api persistiert sie).
                "overrides": overrides,
                "preset": merged_preset,
            }
        )

    price: Optional[Dict[str, Any]] = None
    rest: List[Dict[str, Any]] = []
    for pane in panes:
        role = (pane["preset"].get("role") or "any")
        if price is None and (pane.get("pane_id") == "main" or role == "price"):
            price = pane
        else:
            rest.append(pane)

    if price is None:
        price = {"pane_id": "main", "pane_preset_id": CANDLES_PRESET, "scale": "linear",
                 "weight": None, "overrides": {}, "preset": builtin_preset(CANDLES_PRESET)}
    price["pane_id"] = "main"
    price["weight"] = price["weight"] or 7

    final = [price] + rest
    used: set = set()
    for idx, pane in enumerate(final):
        base = str(pane.get("pane_id") or pane.get("pane_preset_id") or "pane").strip().lower()
        candidate, suffix = base, 2
        while candidate in used:
            candidate = base + "_" + str(suffix)
            suffix += 1
        used.add(candidate)
        pane["pane_id"] = candidate
        pane["weight"] = pane["weight"] or 2

    return {
        "id": chart.get("id"),
        "display_name": chart.get("display_name") or chart.get("id") or "Chart",
        "description": chart.get("description") or "",
        "draft": bool(draft),
        "topbar_metrics": chart.get("topbar_metrics") or [],
        "x_axis_pane": chart.get("x_axis_pane"),
        "panes": final,
        # Warnungen aus der Override-Aufloesung; build_overlays mischt sie in seinen
        # Warnkanal, damit der Agent sie zusammen mit den Render-Warnungen sieht.
        "override_warnings": override_warnings,
    }


def threshold_color(value: Optional[float], base_color: str, rules: Dict[str, Any]) -> str:
    """Farbe eines Punktes aus den Schwellenregeln der Serie."""
    if value is None:
        return base_color
    for rule in (rules or {}).get("thresholds") or []:
        try:
            if "below" in rule and value < float(rule["below"]):
                return rule.get("color") or base_color
            if "above" in rule and value > float(rule["above"]):
                return rule.get("color") or base_color
        except (TypeError, ValueError):
            continue
    return base_color


def derive_phases(points: List[Tuple[int, float]], low: float, high: float, within: int) -> List[Tuple[int, int]]:
    """Phasen 'Wert war <= low und steigt innerhalb von within Bars auf >= high'.

    Eingabe: nach Bar-Index sortierte (index, value)-Punkte. Ausgabe:
    Liste von (start_index, end_index) in Bar-Indizes, ueberlappende Phasen
    zusammengefasst. Ein Tief wird nur einmal verbraucht: nach einer
    abgeschlossenen Phase beginnt die naechste mit einem neuen Tief.
    """
    phases: List[Tuple[int, int]] = []
    last_low: Optional[int] = None
    for idx, value in points:
        if value <= low:
            last_low = idx
            continue
        if value >= high and last_low is not None and 0 < (idx - last_low) <= within:
            phases.append((last_low, idx))
            last_low = None

    merged: List[Tuple[int, int]] = []
    for start, end in phases:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _series_overlay(
    pane_id: str,
    series: Dict[str, Any],
    values: List[Tuple[int, float]],
    bars: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Eine Serie in ein Overlay uebersetzen (Linie/Band/Histogramm)."""
    if not values:
        return None
    style = dict(series.get("style") or {})
    rules = series.get("rules") or {}
    base_color = style.get("color") or "#26A69A"
    canonical_id = series.get("canonical_id") or series.get("feature_id") or "series"
    overlay_type = str(style.get("type") or "").strip().lower()
    plot_type = str(series.get("plot_type") or "").strip().lower()

    column = str(series.get("column") or "")
    is_band = plot_type == "overlay_band" or column.endswith("_upper")

    if is_band and series.get("column2"):
        lower_by_idx = {idx: value for idx, value in (series.get("values2") or [])}
        band_values = []
        for idx, upper_val in values:
            lower_val = lower_by_idx.get(idx)
            if upper_val is None or lower_val is None:
                continue
            band_values.append({"t": bars[idx]["t_open"], "value": upper_val, "value2": lower_val})
        if not band_values:
            return None
        return {
            "overlay_id": canonical_id,
            "type": "band",
            "style": {"color": base_color, "alpha": style.get("alpha", 30)},
            "values": band_values,
            "pane": pane_id,
        }

    if overlay_type == "histogram" or plot_type == "sub_hist":
        hist_values = []
        for idx, value in values:
            if value is None:
                continue
            point: Dict[str, Any] = {"t": bars[idx]["t_open"], "value": value}
            override = threshold_color(value, "", rules)
            if override:
                point["color_override"] = override
            hist_values.append(point)
        if not hist_values:
            return None
        return {
            "overlay_id": canonical_id,
            "type": "histogram",
            "style": {"color": base_color, "alpha": style.get("alpha", 70)},
            "values": hist_values,
            "pane": pane_id,
            "origin": "center" if plot_type == "sub_hist_center" else "bottom",
        }

    line_values = []
    for idx, value in values:
        if value is None:
            continue
        point = {"t": bars[idx]["t_open"], "value": value}
        override = threshold_color(value, "", rules)
        if override:
            point["color_override"] = override
        line_values.append(point)
    if not line_values:
        return None
    line_style: Dict[str, Any] = {"color": base_color}
    if style.get("width") is not None:
        line_style["width"] = style["width"]
    return {
        "overlay_id": canonical_id,
        "type": "line",
        "style": line_style,
        "values": line_values,
        "pane": pane_id,
    }


def build_overlays(
    spec: Dict[str, Any],
    bars: List[Dict[str, Any]],
    rows: List[List[Any]],
    col_idx: Dict[str, int],
    resolve_column: Callable[[str], Optional[str]],
    calculate: Callable[..., Dict[str, Any]],
    symbol: str,
    timeframe: str,
    limit: int,
) -> Dict[str, Any]:
    """Chart-Definition + Datenzeilen -> Overlays, Pane-Metadaten, Skalen.

    Fehlende Spalten werden gebuendelt on-the-fly berechnet (SMA/EMA/Bollinger/
    ADR). Was auch dann fehlt, landet in missing und wird im Topbar ausgewiesen -
    eine Pane bleibt nie stillschweigend leer.
    """
    ts_of_idx = [int(r[col_idx["timestamp"]]) for r in rows]
    overlays: List[Dict[str, Any]] = []
    panes_meta: List[Dict[str, Any]] = []
    pane_scales: Dict[str, str] = {}
    missing: List[str] = []
    warnings: List[Dict[str, Any]] = list(spec.get("override_warnings") or [])

    plan: List[Dict[str, Any]] = []
    pending: Dict[str, set] = {}
    for pane in spec["panes"]:
        preset = pane["preset"]
        pane_id = pane["pane_id"]
        role = preset.get("role") or "any"
        panes_meta.append(
            {
                "pane_id": pane_id,
                "role": role,
                "title": preset.get("display_name") or preset.get("id") or pane_id,
                "weight": pane["weight"],
                "scale": pane["scale"],
                "preset_id": preset.get("id"),
            }
        )
        pane_scales[pane_id] = pane["scale"]

        if role == "volume":
            continue

        is_builtin = str(preset.get("kind") or "") == "builtin"
        if not is_builtin and not (preset.get("series") or preset.get("refs") or preset.get("zones")):
            # Pane ohne Inhalt: kein Fehler, aber sichtbar machen statt leer rendern.
            _add_warning(warnings, "pane_without_series", str(preset.get("id") or ""), pane_id)

        for series in preset.get("series") or []:
            column = series.get("column") or series.get("canonical_id") or ""
            resolved = resolve_column(column)
            entry = {"pane_id": pane_id, "series": series, "column": column, "resolved": resolved}
            if resolved is None:
                spec_calc = _calc_spec(column)
                if spec_calc:
                    entry["calc"] = spec_calc
                    pending.setdefault(spec_calc["indicator_type"], set()).add(spec_calc["period"])
                else:
                    missing.append(column)
                    _add_warning(warnings, "unknown_column", column, pane_id)
            plan.append(entry)

    calculated: Dict[str, List[Optional[float]]] = {}
    calc_ts: List[int] = []
    for indicator_type, periods in pending.items():
        try:
            result = calculate(symbol, indicator_type, sorted(periods), timeframe, limit)
            calc_ts = result.get("timestamps") or calc_ts
            calculated.update(result.get("series") or {})
        except Exception as exc:  # pragma: no cover - Netzwerkfehler
            logger.error("On-the-fly %s fehlgeschlagen: %s", indicator_type, exc)

    for entry in plan:
        values = _column_values(entry, rows, col_idx, calculated, calc_ts)
        # Werte ausserhalb der geladenen Bars (z. B. aus einer Berechnung mit
        # anderem Zeitfenster) wuerden beim Zeichnen ins Leere greifen.
        if values:
            values = [pt for pt in values if 0 <= pt[0] < len(bars)]
        if not values:
            if entry["column"] not in missing:
                missing.append(entry["column"])
            _add_warning(warnings, "series_without_data", entry["column"], entry["pane_id"])
            continue
        overlay = _series_overlay(entry["pane_id"], entry["series"], values, bars)
        if overlay:
            overlays.append(overlay)

    for pane in spec["panes"]:
        if (pane["preset"].get("role") or "") != "volume":
            continue
        vol_values = []
        for r in rows:
            vol = r[col_idx["volume"]] if "volume" in col_idx else None
            if vol is None:
                continue
            is_up = float(r[col_idx["close"]]) >= float(r[col_idx["open"]])
            vol_values.append(
                {
                    "t": int(r[col_idx["timestamp"]]),
                    "value": float(vol),
                    "color_override": "#3877FF" if is_up else "#E040FB",
                }
            )
        if vol_values:
            overlays.append(
                {
                    "overlay_id": "volume_" + pane["pane_id"],
                    "type": "histogram",
                    "style": {"color": "#546E7A", "alpha": 60},
                    "values": vol_values,
                    "pane": pane["pane_id"],
                    "origin": "bottom",
                }
            )

    for pane in spec["panes"]:
        preset = pane["preset"]
        pane_id = pane["pane_id"]
        for derive in preset.get("derives") or []:
            if str(derive.get("fn") or "") != "cross_window":
                continue
            source = str(derive.get("series") or "")
            points = _points_for(pane_id, source, overlays, rows, col_idx)
            if not points:
                marker = source + " (Phase)"
                if marker not in missing:
                    missing.append(marker)
                continue
            try:
                phases = derive_phases(
                    points,
                    float(derive.get("low")),
                    float(derive.get("high")),
                    int(derive.get("within") or 20),
                )
            except (TypeError, ValueError):
                continue
            if not phases:
                continue
            for zone_idx, zone in enumerate(preset.get("zones") or []):
                if str(zone.get("from") or "") != str(derive.get("id") or ""):
                    continue
                style = dict(zone.get("style") or {})
                style.setdefault("color", "#26A69A")
                style.setdefault("alpha", 40)
                if zone.get("label"):
                    style["label"] = zone["label"]
                overlays.append(
                    {
                        "overlay_id": "zone_" + pane_id + "_" + str(zone_idx),
                        "type": "zone",
                        "style": style,
                        "values": [
                            {"t": ts_of_idx[start], "t2": ts_of_idx[end], "value": None, "value2": None}
                            for start, end in phases
                            if 0 <= start < len(ts_of_idx) and 0 <= end < len(ts_of_idx)
                        ],
                        "pane": pane_id,
                    }
                )

    for pane in spec["panes"]:
        preset = pane["preset"]
        for ref_idx, ref in enumerate(preset.get("refs") or []):
            if ref.get("value") is None:
                continue
            style = {"color": "#546E7A", "width": 1}
            style.update(ref.get("style") or {})
            if ref.get("label"):
                style["label"] = str(ref["label"])
            overlays.append(
                {
                    "overlay_id": "level_" + pane["pane_id"] + "_" + str(ref_idx),
                    "type": "level",
                    "style": style,
                    "values": [{"t": ts_of_idx[0] if ts_of_idx else 0, "value": float(ref["value"])}],
                    "pane": pane["pane_id"],
                }
            )

    overlays.sort(key=lambda ov: _Z_ORDER.get(ov.get("type", "line"), 9))
    return {
        "overlays": overlays,
        "panes": panes_meta,
        "pane_scales": pane_scales,
        "missing": missing,
        # Strukturierter Warnkanal (Plan Phase 4): der Agent soll sehen, wenn ein
        # Pane leer bleibt oder eine Spalte fehlt - nicht stillschweigend "success".
        "warnings": warnings,
    }


def _add_warning(warnings: List[Dict[str, Any]], code: str, detail: str, pane_id: str) -> None:
    """Warnung eindeutig anhaengen (gleiche Pane+Code+Detail nur einmal)."""
    entry = {"code": code, "pane_id": pane_id, "detail": detail}
    if entry not in warnings:
        warnings.append(entry)


# ── Strukturvergleich (Preview/Diff, Plan Phase 2) ─────────────────────────


def _pane_snapshot(pane: Dict[str, Any]) -> Dict[str, Any]:
    pane = pane if isinstance(pane, dict) else {}
    preset = pane.get("preset") if isinstance(pane.get("preset"), dict) else {}
    return {
        "pane_id": str(pane.get("pane_id") or ""),
        # Chart-Definitionen aus der PCA-API liefern die Preset-Id verschachtelt
        # unter "preset.id" (pca_chart_preset_panes liefert "pane_preset_id").
        "pane_preset_id": str(
            pane.get("pane_preset_id") or pane.get("preset_id") or preset.get("id") or ""
        ),
        "scale": str(pane.get("scale") or "linear"),
        "weight": int(pane.get("weight") or 0) or 2,
        "overrides": pane.get("overrides") or {},
    }


def diff_chart_specs(
    window_panes: Any,
    chart_panes: Any,
    window_meta: Optional[Dict[str, Any]] = None,
    chart_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fensterinhalt vs. Chart-Definition vergleichen - ohne zu rendern.

    Beide Seiten haben dieselbe Form (Pane-Slots), deshalb ist der Vergleich ein
    Mengen-, Reihenfolge- und Feldervergleich. Ergebnis ist der Vertrag fuer
    DIFF_CHART (Plan Phase 2 / 7).
    """
    window = [_pane_snapshot(p) for p in (window_panes or [])]
    chart = [_pane_snapshot(p) for p in (chart_panes or [])]
    window_by = {p["pane_id"]: p for p in window}
    chart_by = {p["pane_id"]: p for p in chart}

    added = [p for p in chart if p["pane_id"] not in window_by]
    removed = [p for p in window if p["pane_id"] not in chart_by]

    changed = []
    for pane_id, current in window_by.items():
        target = chart_by.get(pane_id)
        if target is None:
            continue
        fields = {}
        for field in ("pane_preset_id", "scale", "weight", "overrides"):
            if current[field] != target[field]:
                fields[field] = {"window": current[field], "chart": target[field]}
        if fields:
            changed.append({"pane_id": pane_id, "fields": fields})

    positions = {p["pane_id"]: idx for idx, p in enumerate(window)}
    reordered = []
    for target_index, pane in enumerate(chart):
        if pane["pane_id"] not in window_by:
            continue
        current_index = positions.get(pane["pane_id"])
        if current_index is not None and current_index != target_index:
            reordered.append({"pane_id": pane["pane_id"], "from": current_index, "to": target_index})

    result: Dict[str, Any] = {
        "same": False,
        "added": added,
        "removed": removed,
        "reordered": reordered,
        "changed": changed,
    }
    wm = window_meta or {}
    cm = chart_meta or {}
    for key in ("topbar_metrics", "x_axis_pane"):
        if (wm.get(key) or None) != (cm.get(key) or None):
            result[key] = {"window": wm.get(key), "chart": cm.get(key)}
    result["same"] = not (added or removed or reordered or changed) and not any(
        key in result for key in ("topbar_metrics", "x_axis_pane")
    )
    return result


def _calc_spec(column: str) -> Optional[Dict[str, Any]]:
    for pattern, indicator_type in (
        (r"^(?:ma_)?sma_(\d+)$", "SMA"),
        (r"^(?:ma_)?ema_(\d+)$", "EMA"),
        (r"^adr_(\d+)_pct$", "ADR_PCT"),
        (r"^adr_(\d+)_sma$", "ADR_PCT"),
    ):
        match = re.match(pattern, column)
        if match:
            return {"indicator_type": indicator_type, "period": int(match.group(1))}
    match = re.match(r"^bb_(\d+)(?:_upper)?$", column)
    if match:
        return {"indicator_type": "BOLLINGER", "period": int(match.group(1))}
    return None


def _column_values(
    entry: Dict[str, Any],
    rows: List[List[Any]],
    col_idx: Dict[str, int],
    calculated: Dict[str, List[Optional[float]]],
    calc_ts: List[int],
) -> Optional[List[Tuple[int, float]]]:
    """Werte einer Serie als (bar_index, value) - aus Parquet oder Berechnung."""
    series = entry["series"]
    resolved = entry.get("resolved")
    column = str(series.get("column") or entry["column"])
    calc = entry.get("calc")

    if resolved is not None and resolved in col_idx:
        index = col_idx[resolved]
        out = [(i, float(r[index])) for i, r in enumerate(rows) if r[index] is not None]
        if column.endswith("_upper"):
            lower = column.replace("_upper", "_lower")
            if lower in col_idx:
                series["column2"] = lower
                series["values2"] = [
                    (i, float(r[col_idx[lower]])) for i, r in enumerate(rows) if r[col_idx[lower]] is not None
                ]
        return out

    if not calc:
        return None
    values = calculated.get(_calc_key(calc))
    if values is None:
        return None
    ts_index = {int(t): i for i, t in enumerate(calc_ts)}
    out = []
    for pos, value in enumerate(values):
        if value is None or pos >= len(calc_ts):
            continue
        bar_index = ts_index.get(int(calc_ts[pos]))
        if bar_index is not None:
            out.append((bar_index, float(value)))
    if column.endswith("_upper"):
        lower_key = "bb_" + str(calc["period"]) + "_lower"
        lower_values = calculated.get(lower_key)
        if lower_values is not None:
            series["column2"] = lower_key
            series["values2"] = [
                (ts_index[int(t)], float(v))
                for t, v in zip(calc_ts, lower_values)
                if v is not None and int(t) in ts_index
            ]
    return out


def _calc_key(calc: Dict[str, Any]) -> str:
    if calc["indicator_type"] == "BOLLINGER":
        return "bb_" + str(calc["period"]) + "_upper"
    if calc["indicator_type"] == "ADR_PCT":
        return "adr_" + str(calc["period"]) + "_pct"
    if calc["indicator_type"] == "SMA":
        return "sma_" + str(calc["period"])
    return "ema_" + str(calc["period"])


def _points_for(
    pane_id: str,
    canonical_id: str,
    overlays: List[Dict[str, Any]],
    rows: List[List[Any]],
    col_idx: Dict[str, int],
) -> List[Tuple[int, float]]:
    """Werte einer bereits gebauten Serie (fuer abgeleitete Phasen)."""
    ts_index = {int(r[col_idx["timestamp"]]): i for i, r in enumerate(rows)}
    for overlay in overlays:
        if overlay.get("pane") != pane_id or overlay.get("type") not in ("line", "histogram"):
            continue
        if overlay.get("overlay_id") != canonical_id:
            continue
        points = []
        for point in overlay.get("values") or []:
            index = ts_index.get(int(point.get("t")))
            if index is not None and point.get("value") is not None:
                points.append((index, float(point["value"])))
        return sorted(points)
    return []
