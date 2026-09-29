"""Pane-Presets, Chart-Presets und die Alt-API /api/presets.

Zwei-Ebenen-Modell (Vertrag: docs/architecture/chart-presets.md):

    Pane-Preset  = Inhalt EINES Panes (Serien, Regeln, Zonen, Referenzlinien)
    Chart-Preset = Fenster-Inhalt: geordnete Panes + Topbar

Source of Truth sind ausschliesslich die Tabellen pca_pane_presets,
pca_pane_preset_members, pca_chart_presets und pca_chart_preset_panes. Die
Alt-Endpunkte /api/presets lesen und schreiben ueber dieselben Tabellen, damit
aeltere Viewer-/Agent-Versionen unveraendert weiterlaufen.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from chart_data import (
    _auto_create_feature_if_missing,
    _supabase_delete,
    _supabase_get,
    _supabase_patch,
    _supabase_post,
)

logger = logging.getLogger("pca.presets")
router = APIRouter()

PANE_SCALE_VALUES = ("linear", "log")
PANE_ROLES = ("price", "value", "volume", "any")
PANE_KINDS = ("indicator", "custom", "builtin")
VOLUME_PANE_PRESET = "builtin:volume"
CANDLES_PANE_PRESET = "builtin:candles"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_pane_scales(raw: Any) -> Dict[str, str]:
    """Y-Achsen-Skalierung je Pane validieren (Altverhalten, unveraendert).

    Eingabe: {"main": "log", "volume": "linear"}. Unbekannte Werte werden auf
    'linear' gezogen, unbrauchbare Eintraege verworfen - ein Preset darf nie an
    einem Tippfehler scheitern.
    """
    if not isinstance(raw, dict):
        return {}
    clean: Dict[str, str] = {}
    for key, value in raw.items():
        pane_id = str(key or "").strip().lower()
        if not pane_id:
            continue
        scale = str(value or "").strip().lower()
        clean[pane_id] = scale if scale in PANE_SCALE_VALUES else "linear"
    return clean


def _clean_id(raw: Any) -> str:
    return str(raw or "").strip()


def _clean_role(raw: Any) -> str:
    role = str(raw or "").strip().lower()
    return role if role in PANE_ROLES else "any"


def _clean_kind(raw: Any) -> str:
    kind = str(raw or "").strip().lower()
    return kind if kind in PANE_KINDS else "indicator"


def _clean_scale(raw: Any) -> str:
    scale = str(raw or "").strip().lower()
    return scale if scale in PANE_SCALE_VALUES else "linear"


def resolve_member_pane(
    explicit_pane: Optional[str],
    plot_type: Optional[str],
    canonical_id: Optional[str],
) -> str:
    """Ziel-Pane eines Legacy-Members bestimmen (unveraendertes Altverhalten)."""
    if explicit_pane is not None and str(explicit_pane).strip():
        return str(explicit_pane).strip().lower()
    if plot_type in ("overlay_line", "overlay_band"):
        return "main"
    if plot_type in ("sub_line", "subchart"):
        return (canonical_id or "main").strip().lower()
    return "main"


def _feature_label_map() -> Dict[str, Dict[str, Any]]:
    """canonical_id -> Feature-Zeile (display_name, calc_type, plot_type, ...)."""
    rows = _supabase_get(
        "pca_features",
        "select=canonical_id,alias,display_name,calc_type,calc_params,plot_type,default_style,mode",
    )
    return {r.get("canonical_id", ""): r for r in rows if r.get("canonical_id")}


def _merge_style(feature: Dict[str, Any], override: Dict[str, Any]) -> dict:
    return {**(feature.get("default_style") or {}), **(override or {})}


def _series_entry(feature: Dict[str, Any], member: Dict[str, Any]) -> Dict[str, Any]:
    """Feature + Member zu einer Zeichen-Serie aufloesen (wie die Alt-API)."""
    canonical_id = feature.get("canonical_id", member.get("feature_id", ""))
    if feature.get("calc_type") == "BOLLINGER":
        window = (feature.get("calc_params") or {}).get("window", 20)
        column = f"bb_{window}_upper"
    else:
        column = canonical_id
    return {
        "feature_id": member.get("feature_id"),
        "column": column,
        "canonical_id": canonical_id,
        "alias": feature.get("alias"),
        "display_name": feature.get("display_name") or canonical_id,
        "calc_type": feature.get("calc_type"),
        "mode": feature.get("mode"),
        "plot_type": feature.get("plot_type"),
        "style": _merge_style(feature, member.get("style_override")),
        "rules": member.get("rules") or {},
    }


def _pane_summary(labels: List[str]) -> str:
    """Kompakte Ein-Zeile fuer Dropdowns (Text statt Thumbnail)."""
    labels = [label for label in labels if label]
    if not labels:
        return "leer"
    if len(labels) <= 3:
        return " · ".join(labels)
    return " · ".join(labels[:3]) + f" +{len(labels) - 3} weitere"


# ---------------------------------------------------------------------------
# Pane-Presets
# ---------------------------------------------------------------------------


class PaneMember(BaseModel):
    feature_id: str
    sort_order: int = 0
    style_override: dict = {}
    rules: dict = {}
    # Nur in der Altform (ChartPresetIn.members) benutzt: Ziel-Pane als String.
    # In einem Pane-Preset ist das Pane implizit - das Feld wird dort ignoriert.
    pane: Optional[str] = None


class PanePresetIn(BaseModel):
    id: str
    display_name: str = ""
    description: str = ""
    role: str = "any"
    kind: str = "indicator"
    renderer: Optional[str] = None
    params: dict = {}
    default_scale: str = "linear"
    refs: list = []
    derives: list = []
    zones: list = []
    members: List[PaneMember] = []


def _pane_payload(pane: PanePresetIn, pane_id: str) -> dict:
    return {
        "id": pane_id,
        "display_name": pane.display_name or pane_id,
        "description": pane.description or "",
        "role": _clean_role(pane.role),
        "kind": _clean_kind(pane.kind),
        "renderer": pane.renderer,
        "params": pane.params or {},
        "default_scale": _clean_scale(pane.default_scale),
        "refs": pane.refs or [],
        "derives": pane.derives or [],
        "zones": pane.zones or [],
    }


def _store_pane_members(pane_id: str, members: List[PaneMember], replace: bool = True) -> None:
    if replace:
        _supabase_delete("pca_pane_preset_members", f"pane_preset_id=eq.{pane_id}")
    for idx, member in enumerate(members):
        _auto_create_feature_if_missing(member.feature_id)
        _supabase_post(
            "pca_pane_preset_members",
            {
                "pane_preset_id": pane_id,
                "feature_id": member.feature_id,
                "sort_order": member.sort_order if member.sort_order else idx,
                "style_override": member.style_override or {},
                "rules": member.rules or {},
            },
        )


def _load_pane_full(pane_id: str) -> Dict[str, Any]:
    """Ein Pane-Preset mit aufgeloesten Serien."""
    rows = _supabase_get("pca_pane_presets", f"id=eq.{pane_id}")
    if not rows:
        raise HTTPException(status_code=404, detail=f"Pane-Preset '{pane_id}' nicht gefunden.")
    pane = rows[0]
    members = _supabase_get(
        "pca_pane_preset_members",
        f"pane_preset_id=eq.{pane_id}&select=feature_id,sort_order,style_override,rules&order=sort_order",
    )
    features = _feature_label_map()
    series = []
    for member in members:
        feature = features.get(member.get("feature_id", ""), {})
        series.append(_series_entry(feature, member))
    pane["series"] = series
    pane["summary"] = _pane_summary([s.get("display_name") or s.get("feature_id") for s in series])
    return pane


@router.get("/panes")
async def list_panes(include_archived: bool = Query(default=False)):
    """Katalog aller Pane-Presets (Dropdown-Quelle)."""
    flt = "order=id" if include_archived else "archived=eq.false&order=id"
    panes = _supabase_get("pca_pane_presets", flt)
    members = _supabase_get(
        "pca_pane_preset_members", "select=pane_preset_id,feature_id,sort_order&order=sort_order"
    )
    features = _feature_label_map()
    by_pane: Dict[str, List[dict]] = {}
    for member in members:
        by_pane.setdefault(member.get("pane_preset_id", ""), []).append(member)

    out = []
    for pane in panes:
        rows = by_pane.get(pane.get("id", ""), [])
        labels = [
            (features.get(r.get("feature_id", ""), {}).get("display_name") or r.get("feature_id"))
            for r in rows
        ]
        out.append(
            {
                "id": pane.get("id"),
                "display_name": pane.get("display_name") or pane.get("id"),
                "description": pane.get("description") or "",
                "role": pane.get("role") or "any",
                "kind": pane.get("kind") or "indicator",
                "default_scale": pane.get("default_scale") or "linear",
                "member_count": len(rows),
                "zone_count": len(pane.get("zones") or []),
                "ref_count": len(pane.get("refs") or []),
                "summary": _pane_summary(labels),
            }
        )
    return {"panes": out, "count": len(out)}


@router.get("/panes/{pane_id}")
async def get_pane(pane_id: str):
    return _load_pane_full(pane_id)


@router.post("/panes")
async def create_pane(pane: PanePresetIn):
    pane_id = _clean_id(pane.id)
    if not pane_id:
        raise HTTPException(status_code=400, detail="id ist Pflicht.")
    if _supabase_get("pca_pane_presets", f"id=eq.{pane_id}&select=id"):
        raise HTTPException(status_code=409, detail=f"Pane-Preset '{pane_id}' existiert bereits.")
    _supabase_post("pca_pane_presets", _pane_payload(pane, pane_id))
    _store_pane_members(pane_id, pane.members)
    return {"status": "success", "id": pane_id}


@router.put("/panes/{pane_id}")
async def update_pane(pane_id: str, pane: PanePresetIn):
    if not _supabase_get("pca_pane_presets", f"id=eq.{pane_id}&select=id"):
        raise HTTPException(status_code=404, detail=f"Pane-Preset '{pane_id}' nicht gefunden.")
    payload = _pane_payload(pane, pane_id)
    payload["updated_at"] = "now()"
    _supabase_patch("pca_pane_presets", f"id=eq.{pane_id}", payload)
    _store_pane_members(pane_id, pane.members)
    return {"status": "success", "id": pane_id}


@router.delete("/panes/{pane_id}")
async def delete_pane(pane_id: str, hard: bool = Query(default=False)):
    """Standard ist Soft-Delete (archived): Charts, die das Pane referenzieren,
    bleiben lesbar. hard=true loescht endgueltig (nur ohne Referenzen)."""
    if hard:
        used = _supabase_get(
            "pca_chart_preset_panes", f"pane_preset_id=eq.{pane_id}&select=chart_preset_id"
        )
        if used:
            names = ", ".join(sorted({u.get("chart_preset_id", "") for u in used}))
            raise HTTPException(
                status_code=409,
                detail=f"Pane-Preset '{pane_id}' wird von Charts benutzt ({names}) - erst dort entfernen.",
            )
        _supabase_delete("pca_pane_presets", f"id=eq.{pane_id}")
    else:
        _supabase_patch("pca_pane_presets", f"id=eq.{pane_id}", {"archived": True})
    return {"status": "success", "id": pane_id, "hard": bool(hard)}


# ---------------------------------------------------------------------------
# Chart-Presets
# ---------------------------------------------------------------------------


class ChartPaneIn(BaseModel):
    pane_id: Optional[str] = None
    pane_preset_id: str
    sort_order: int = 0
    weight: int = 2
    scale: str = "linear"
    overrides: dict = {}


class ChartPresetIn(BaseModel):
    id: str
    display_name: str = ""
    description: str = ""
    topbar_metrics: List[str] = []
    x_axis_pane: Optional[str] = None
    panes: Optional[List[ChartPaneIn]] = None
    # Altform: flache Mitgliederliste mit pane-Strings (wird uebersetzt).
    members: Optional[List[PaneMember]] = None
    pane_scales: Optional[Dict[str, str]] = None


def _chart_panes(chart_id: str) -> List[dict]:
    rows = _supabase_get(
        "pca_chart_preset_panes",
        f"chart_preset_id=eq.{chart_id}&order=sort_order&select=pane_id,pane_preset_id,sort_order,weight,scale,overrides",
    )
    return sorted(rows, key=lambda r: (0 if r.get("pane_id") == "main" else 1, r.get("sort_order") or 0, r.get("pane_id") or ""))


def _assign_pane_ids(pane_ids: List[Optional[str]], preset_ids: List[str]) -> List[str]:
    """Eindeutige Slot-Ids vergeben: explizit > Pane-Preset-Id, Kollision => _2."""
    out: List[str] = []
    used: set = set()
    for idx, (explicit, preset_id) in enumerate(zip(pane_ids, preset_ids)):
        base = _clean_id(explicit) or _clean_id(preset_id) or f"pane{idx + 1}"
        base = base.lower()
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        out.append(candidate)
    return out


def _store_chart_panes(chart_id: str, panes: List[Dict[str, Any]]) -> None:
    """Panestruktur eines Charts schreiben.

    Das Preispane (role=price) liegt immer auf dem Slot 'main' und zuerst - das
    ist die einzige Invariante (chart-presets.md Abschnitt 1). Sonst wird genau
    die uebergebene Liste persistiert: kein implizites Volumen-Pane, sonst waere
    "Volumen entfernen" nicht speicherbar. Die Altform (flache Mitglieder) setzt
    ihr Volumen-Pane in _write_chart, weil sie seine Abwesenheit nicht ausdruecken
    kann.
    """
    _supabase_delete("pca_chart_preset_panes", f"chart_preset_id=eq.{chart_id}")
    if not panes:
        panes = [{"pane_id": "main", "pane_preset_id": CANDLES_PANE_PRESET, "scale": "linear", "weight": 7}]

    roles = {}
    for preset_id in {p.get("pane_preset_id") for p in panes}:
        rows = _supabase_get("pca_pane_presets", f"id=eq.{preset_id}&select=id,role,default_scale")
        if rows:
            roles[preset_id] = rows[0]

    ordered: List[Dict[str, Any]] = []
    price_pane = None
    for pane in panes:
        preset_id = pane.get("pane_preset_id")
        role = (roles.get(preset_id) or {}).get("role") or "any"
        if price_pane is None and pane.get("pane_id") == "main":
            price_pane = pane
        elif role == "price" and price_pane is None:
            price_pane = pane
        else:
            ordered.append(pane)

    if price_pane is None:
        price_pane = {"pane_id": "main", "pane_preset_id": CANDLES_PANE_PRESET, "scale": "linear", "weight": 7}
    price_pane = {**price_pane, "pane_id": "main"}

    final = [price_pane] + ordered
    preset_ids = [p.get("pane_preset_id") for p in final]
    pane_ids = _assign_pane_ids([p.get("pane_id") for p in final], preset_ids)

    for idx, (pane, pane_id) in enumerate(zip(final, pane_ids)):
        payload = {
            "chart_preset_id": chart_id,
            "pane_id": pane_id,
            "pane_preset_id": pane.get("pane_preset_id"),
            "sort_order": idx,
            "weight": max(1, int(pane.get("weight") or 2)) if pane_id != "main" else max(1, int(pane.get("weight") or 7)),
            "scale": _clean_scale(pane.get("scale")),
            "overrides": pane.get("overrides") or {},
        }
        try:
            _supabase_post("pca_chart_preset_panes", payload)
        except Exception as exc:  # unbekanntes Pane-Preset o. Ae.
            raise HTTPException(status_code=400, detail=f"Pane '{payload['pane_id']}' konnte nicht gespeichert werden: {exc}")


def _panes_from_legacy_members(chart_id: str, members: List[PaneMember]) -> List[Dict[str, Any]]:
    """Altform (flache Mitglieder mit pane-Strings) in Pane-Presets uebersetzen."""
    features = _feature_label_map()
    groups: Dict[str, List[PaneMember]] = {}
    for idx, member in enumerate(members):
        if not member.sort_order:
            member.sort_order = idx
        feature = features.get(member.feature_id, {})
        pane_id = resolve_member_pane(getattr(member, "pane", None), feature.get("plot_type"), member.feature_id)
        if pane_id == "none":
            continue
        groups.setdefault(pane_id, []).append(member)

    panes: List[Dict[str, Any]] = []
    for sort_index, (pane_id, pane_members) in enumerate(groups.items()):
        preset_id = f"{chart_id}__{pane_id}"
        payload = {
            "id": preset_id,
            "display_name": _pane_summary(
                [features.get(m.feature_id, {}).get("display_name") or m.feature_id for m in pane_members]
            ),
            "description": f"Pane '{pane_id}' von Chart '{chart_id}'.",
            "role": "price" if pane_id == "main" else "value",
            "kind": "indicator",
            "renderer": None,
            "params": {},
            "default_scale": "linear",
            "refs": [],
            "derives": [],
            "zones": [],
        }
        if _supabase_get("pca_pane_presets", f"id=eq.{preset_id}&select=id"):
            _supabase_patch("pca_pane_presets", f"id=eq.{preset_id}", payload)
        else:
            _supabase_post("pca_pane_presets", payload)
        _store_pane_members(preset_id, pane_members)
        panes.append({"pane_id": pane_id, "pane_preset_id": preset_id, "sort_order": sort_index})
    return panes


def _write_chart(chart_id: str, chart: ChartPresetIn, create: bool) -> None:
    payload = {
        "id": chart_id,
        "display_name": chart.display_name or chart_id,
        "description": chart.description or "",
        "topbar_metrics": chart.topbar_metrics or [],
        "x_axis_pane": (chart.x_axis_pane or None),
    }
    exists = bool(_supabase_get("pca_chart_presets", f"id=eq.{chart_id}&select=id"))
    if create:
        if exists:
            raise HTTPException(status_code=409, detail=f"Chart '{chart_id}' existiert bereits.")
        _supabase_post("pca_chart_presets", payload)
    else:
        if not exists:
            raise HTTPException(status_code=404, detail=f"Chart '{chart_id}' nicht gefunden.")
        payload["updated_at"] = "now()"
        _supabase_patch("pca_chart_presets", f"id=eq.{chart_id}", payload)

    panes: List[Dict[str, Any]] = []
    if chart.panes:
        panes = [
            {
                "pane_id": p.pane_id,
                "pane_preset_id": p.pane_preset_id,
                "weight": p.weight,
                "scale": p.scale,
                "overrides": p.overrides,
            }
            for p in chart.panes
        ]
    elif chart.members:
        panes = _panes_from_legacy_members(chart_id, chart.members)
        # Die flache Altform kennt keinen Zustand "ohne Volumen": der Viewer
        # zeichnete es fuer jedes Fenster. Bei der Uebersetzung wird es deshalb -
        # wie in Migration 040 - explizit in die Definition geschrieben. Danach ist
        # es eine normale, entfernbare Pane.
        if not any(p.get("pane_preset_id") == VOLUME_PANE_PRESET for p in panes):
            panes.append(
                {"pane_id": "volume", "pane_preset_id": VOLUME_PANE_PRESET, "weight": 2, "scale": "linear"}
            )
    else:
        existing = _chart_panes(chart_id)
        panes = [
            {"pane_id": p["pane_id"], "pane_preset_id": p["pane_preset_id"], "weight": p.get("weight"), "scale": p.get("scale")}
            for p in existing
        ]

    if chart.pane_scales is not None:
        scales = normalize_pane_scales(chart.pane_scales)
        for pane in panes:
            if pane.get("pane_id") in scales:
                pane["scale"] = scales[pane["pane_id"]]

    _store_chart_panes(chart_id, panes)


@router.get("/charts")
async def list_charts():
    charts = _supabase_get("pca_chart_presets", "order=id")
    panes = _supabase_get(
        "pca_chart_preset_panes", "select=chart_preset_id,pane_id,pane_preset_id&order=sort_order"
    )
    presets = _supabase_get("pca_pane_presets", "select=id,display_name,role,archived")
    names = {p.get("id"): (p.get("display_name") or p.get("id")) for p in presets}
    by_chart: Dict[str, List[dict]] = {}
    for pane in panes:
        by_chart.setdefault(pane.get("chart_preset_id", ""), []).append(pane)

    out = []
    for chart in charts:
        rows = sorted(by_chart.get(chart.get("id", ""), []), key=lambda r: r.get("pane_id") or "")
        out.append(
            {
                "id": chart.get("id"),
                "display_name": chart.get("display_name") or chart.get("id"),
                "description": chart.get("description") or "",
                "pane_count": len(rows),
                "summary": " · ".join(f"{r.get('pane_id')}={names.get(r.get('pane_preset_id'), r.get('pane_preset_id'))}" for r in rows),
            }
        )
    return {"charts": out, "count": len(out)}


def _load_chart_full(chart_id: str) -> Dict[str, Any]:
    rows = _supabase_get("pca_chart_presets", f"id=eq.{chart_id}")
    if not rows:
        raise HTTPException(status_code=404, detail=f"Chart '{chart_id}' nicht gefunden.")
    chart = rows[0]
    panes = []
    for row in _chart_panes(chart_id):
        preset_id = row.get("pane_preset_id", "")
        if preset_id.startswith("builtin:"):
            preset = {"id": preset_id, "display_name": "Volumen" if preset_id == VOLUME_PANE_PRESET else "Kerzen",
                      "role": "volume" if preset_id == VOLUME_PANE_PRESET else "price",
                      "kind": "builtin", "series": [], "refs": [], "derives": [], "zones": [],
                      "summary": "eingebaut"}
        else:
            try:
                preset = _load_pane_full(preset_id)
            except HTTPException:
                preset = {"id": preset_id, "display_name": preset_id, "role": "any", "kind": "missing",
                          "series": [], "refs": [], "derives": [], "zones": [],
                          "summary": "Pane-Preset fehlt"}
        panes.append(
            {
                "pane_id": row.get("pane_id"),
                "sort_order": row.get("sort_order") or 0,
                "weight": row.get("weight") or 2,
                "scale": row.get("scale") or "linear",
                "overrides": row.get("overrides") or {},
                "preset": preset,
            }
        )
    return {
        "id": chart.get("id"),
        "display_name": chart.get("display_name") or chart.get("id"),
        "description": chart.get("description") or "",
        "topbar_metrics": chart.get("topbar_metrics") or [],
        "x_axis_pane": chart.get("x_axis_pane"),
        "panes": panes,
    }


@router.get("/charts/{chart_id}")
async def get_chart(chart_id: str):
    return _load_chart_full(chart_id)


@router.post("/charts")
async def create_chart(chart: ChartPresetIn):
    chart_id = _clean_id(chart.id)
    if not chart_id:
        raise HTTPException(status_code=400, detail="id ist Pflicht.")
    _write_chart(chart_id, chart, create=True)
    return {"status": "success", "id": chart_id}


@router.put("/charts/{chart_id}")
async def update_chart(chart_id: str, chart: ChartPresetIn):
    _write_chart(chart_id, chart, create=False)
    return {"status": "success", "id": chart_id}


@router.patch("/charts/{chart_id}/pane_scales")
async def update_chart_pane_scales(chart_id: str, update: Dict[str, Any]):
    """Nur die Y-Skalen einzelner Panes setzen (Merge, Panes bleiben unberuehrt)."""
    scales = normalize_pane_scales(update.get("pane_scales"))
    if not _supabase_get("pca_chart_presets", f"id=eq.{chart_id}&select=id"):
        raise HTTPException(status_code=404, detail=f"Chart '{chart_id}' nicht gefunden.")
    for pane_id, scale in scales.items():
        _supabase_patch(
            "pca_chart_preset_panes",
            f"chart_preset_id=eq.{chart_id}&pane_id=eq.{pane_id}",
            {"scale": scale},
        )
    return {"status": "success", "id": chart_id, "pane_scales": scales}


@router.delete("/charts/{chart_id}")
async def delete_chart(chart_id: str):
    _supabase_delete("pca_chart_presets", f"id=eq.{chart_id}")
    return {"status": "success"}


# ---------------------------------------------------------------------------
# Alt-API /api/presets (liest/schreibt ueber die neuen Tabellen)
# ---------------------------------------------------------------------------


@router.get("/presets")
async def legacy_list_presets():
    charts = _supabase_get("pca_chart_presets", "order=id")
    panes = _supabase_get("pca_chart_preset_panes", "select=chart_preset_id,pane_preset_id")
    members = _supabase_get("pca_pane_preset_members", "select=pane_preset_id")
    per_pane: Dict[str, int] = {}
    for member in members:
        key = member.get("pane_preset_id", "")
        per_pane[key] = per_pane.get(key, 0) + 1
    per_chart: Dict[str, int] = {}
    for pane in panes:
        per_chart[pane.get("chart_preset_id", "")] = per_chart.get(pane.get("chart_preset_id", ""), 0) + per_pane.get(
            pane.get("pane_preset_id", ""), 0
        )
    return {
        "presets": {
            c["id"]: {
                "display_name": c.get("display_name", c["id"]),
                "description": c.get("description", ""),
                "indicator_count": per_chart.get(c["id"], 0),
            }
            for c in charts
        },
        "count": len(charts),
    }


@router.get("/presets/{preset_name}")
async def legacy_get_preset(preset_name: str):
    """Flache Altform: eine Indikatorliste mit pane-Strings + pane_scales."""
    chart = _load_chart_full(preset_name)
    indicators = []
    pane_scales: Dict[str, str] = {}
    for pane in chart["panes"]:
        pane_scales[pane["pane_id"]] = pane["scale"]
        for series in pane["preset"].get("series", []):
            indicators.append({**series, "pane": pane["pane_id"]})
    return {
        "name": chart["id"],
        "display_name": chart["display_name"],
        "description": chart["description"],
        "indicators": indicators,
        "topbar_metrics": chart["topbar_metrics"],
        "pane_scales": pane_scales,
        "x_axis_pane": chart["x_axis_pane"],
        "panes": [
            {
                "pane_id": p["pane_id"],
                "pane_preset_id": p["preset"].get("id"),
                "display_name": p["preset"].get("display_name"),
                "role": p["preset"].get("role"),
                "scale": p["scale"],
                "weight": p["weight"],
            }
            for p in chart["panes"]
        ],
    }


@router.post("/presets")
async def legacy_create_preset(preset: ChartPresetIn):
    chart_id = _clean_id(preset.id)
    if preset.members:
        for member in preset.members:
            _auto_create_feature_if_missing(member.feature_id)
    _write_chart(chart_id, preset, create=True)
    return {"status": "success", "id": chart_id}


@router.put("/presets/{preset_name}")
async def legacy_update_preset(preset_name: str, preset: ChartPresetIn):
    if preset.members:
        for member in preset.members:
            _auto_create_feature_if_missing(member.feature_id)
    preset.id = preset_name
    _write_chart(preset_name, preset, create=False)
    return {"status": "success", "id": preset_name}


@router.patch("/presets/{preset_name}/pane_scales")
async def legacy_update_pane_scales(preset_name: str, update: Dict[str, Any]):
    return await update_chart_pane_scales(preset_name, update)


@router.delete("/presets/{preset_name}")
async def legacy_delete_preset(preset_name: str):
    _supabase_delete("pca_chart_presets", f"id=eq.{preset_name}")
    return {"status": "success"}
