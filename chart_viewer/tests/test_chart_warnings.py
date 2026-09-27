"""Tests fuer den Warnkanal im Renderpfad (Plan Phase 4).

Kein Renderpfad darf "success" melden, waehrend Panes fehlen oder leer bleiben:
- unbekanntes Pane-Preset  -> skipped
- unbekannte Spalte/Serie  -> warnings
"""

from __future__ import annotations

from chart_viewer import orchestrator

COLS = ["timestamp", "open", "high", "low", "close", "volume", "ibd_rs"]
ROWS = [
    [1_700_000_000 + i * 86400, 100 + i, 101 + i, 99 + i, 100.5 + i, 1000 + i, 50 + i]
    for i in range(6)
]


def _chart_data():
    return {"status": "ok", "columns": COLS, "data": ROWS, "features_stale": False}


def _pane_preset(pane_id, series, role="value"):
    return {
        "id": pane_id, "display_name": pane_id, "role": role,
        "series": series, "refs": [], "derives": [], "zones": [],
    }


def _series(column):
    return {"canonical_id": column, "column": column, "plot_type": "line", "style": {}, "rules": {}}


def _patch_backend(monkeypatch):
    monkeypatch.setattr(orchestrator, "fetch_chart_data", lambda symbol, timeframe, limit: _chart_data())
    monkeypatch.setattr(orchestrator, "_pca_get", lambda path: {"features": []})
    monkeypatch.setattr(orchestrator, "fundamental_topbar_parts", lambda symbol, price: [])

    presets = {
        "chart_main": _pane_preset("chart_main", [_series("close")], role="price"),
        "kaputt_pane": _pane_preset("kaputt_pane", [_series("gibt_es_nicht")]),
    }

    def fake_soft(path):
        prefix = "/api/panes/"
        if path.startswith(prefix):
            return presets.get(path[len(prefix):])
        return None

    monkeypatch.setattr(orchestrator, "_pca_get_soft", fake_soft)


def test_build_inline_chart_spec_reports_unknown_presets(monkeypatch):
    _patch_backend(monkeypatch)
    skipped = []
    spec = orchestrator.build_inline_chart_spec(
        [
            {"pane_id": "vol", "pane_preset_id": "builtin:volume"},
            {"pane_id": "ghost", "pane_preset_id": "gibt_es_nicht"},
        ],
        skipped=skipped,
    )
    assert skipped == [{"pane_preset_id": "gibt_es_nicht", "reason": "unknown_preset"}]
    assert [p["pane_preset_id"] for p in spec["panes"]] == ["builtin:candles", "builtin:volume"]


def test_build_display_stock_surfaces_warnings_and_skipped(monkeypatch):
    _patch_backend(monkeypatch)
    result = orchestrator.build_display_stock(
        symbol="NVDA",
        panes=[
            {"pane_id": "main", "pane_preset_id": "chart_main"},
            {"pane_id": "kaputt", "pane_preset_id": "kaputt_pane"},
            {"pane_id": "ghost", "pane_preset_id": "gibt_es_nicht"},
        ],
        chart_meta={"draft": True},
    )

    assert result["skipped"] == [{"pane_preset_id": "gibt_es_nicht", "reason": "unknown_preset"}]
    assert {"code": "unknown_column", "pane_id": "kaputt", "detail": "gibt_es_nicht"} in result["warnings"]
    assert result["panes"], "das Fenster rendert trotz uebersprungenem Pane"


def test_build_display_stock_applies_pane_overrides(monkeypatch):
    _patch_backend(monkeypatch)
    result = orchestrator.build_display_stock(
        symbol="NVDA",
        panes=[{
            "pane_id": "main", "pane_preset_id": "chart_main",
            "overrides": {"series": {"close": {"style": {"color": "#ABCDEF"}}}},
        }],
        chart_meta={"draft": True},
    )
    line = [ov for ov in result["overlays"] if ov["overlay_id"] == "close"][0]
    assert line["style"]["color"] == "#ABCDEF"
    assert result["warnings"] == []


def test_build_display_stock_without_problems_reports_empty_lists(monkeypatch):
    _patch_backend(monkeypatch)
    result = orchestrator.build_display_stock(
        symbol="NVDA",
        panes=[{"pane_id": "main", "pane_preset_id": "chart_main"}],
        chart_meta={"draft": True},
    )
    assert result["warnings"] == [] and result["skipped"] == []
