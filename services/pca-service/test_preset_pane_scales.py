"""Selbsttest der Preset-API (Pane-Presets + Chart-Presets), ohne DB.

Lauf im Service-Container:
    docker exec qjm-pca-service python test_preset_pane_scales.py

Geprueft wird die Logik, die frueher in chart_data.py lag und jetzt in
presets_api.py liegt: Skalen-Validierung, Slot-Vergabe, die Uebersetzung der
flachen Altform (members mit pane-Strings) in Pane-Presets und das
Pane-Scales-Teilupdate. Die Supabase-Schicht wird durch eine In-Memory-Tabelle
ersetzt.
"""

import asyncio
import re

from fastapi import HTTPException

import presets_api as pa
import chart_data as cd


# ---------------------------------------------------------------------------
# Fake-Supabase
# ---------------------------------------------------------------------------

FEATURES = [
    {"canonical_id": "sma_50", "alias": None, "display_name": "SMA 50", "calc_type": "SMA",
     "calc_params": {"window": 50}, "plot_type": "overlay_line", "default_style": {"color": "#FFF"}, "mode": "offline"},
    {"canonical_id": "ibd_rs", "alias": None, "display_name": "IBD RS Rating", "calc_type": "IBD_RS",
     "calc_params": {}, "plot_type": "topbar_metric", "default_style": {"color": "#000"}, "mode": "offline"},
    {"canonical_id": "rs_adr_neutral", "alias": None, "display_name": "RS neutral (ADR 20/40/80)", "calc_type": "ADR_NEUTRAL",
     "calc_params": {}, "plot_type": "sub_line", "default_style": {"color": "#111"}, "mode": "offline"},
]


class FakeDB:
    def __init__(self):
        self.tables = {
            "pca_features": [dict(f) for f in FEATURES],
            "pca_pane_presets": [],
            "pca_pane_preset_members": [],
            "pca_chart_presets": [],
            "pca_chart_preset_panes": [],
        }
        self.calls = []

    def _match(self, row, params):
        for part in params.split("&"):
            if not part or part.startswith("select=") or part.startswith("order="):
                continue
            key, _, value = part.partition("=")
            if not value.startswith("eq."):
                continue
            expected = value[3:]
            if str(row.get(key)) != expected:
                return False
        return True

    def get(self, table, params=""):
        return [dict(r) for r in self.tables.get(table, []) if self._match(r, params)]

    def post(self, table, payload):
        row = dict(payload)
        self.tables.setdefault(table, []).append(row)
        self.calls.append(("post", table, row))
        return row

    def patch(self, table, params, payload):
        self.calls.append(("patch", table, params, payload))
        for row in self.tables.get(table, []):
            if self._match(row, params):
                row.update(payload)

    def delete(self, table, params):
        self.calls.append(("delete", table, params))
        self.tables[table] = [r for r in self.tables.get(table, []) if not self._match(r, params)]


def _install(db):
    originals = (cd._supabase_get, cd._supabase_post, cd._supabase_patch, cd._supabase_delete,
                 pa._supabase_get, pa._supabase_post, pa._supabase_patch, pa._supabase_delete)
    cd._supabase_get, cd._supabase_post, cd._supabase_patch, cd._supabase_delete = db.get, db.post, db.patch, db.delete
    pa._supabase_get, pa._supabase_post, pa._supabase_patch, pa._supabase_delete = db.get, db.post, db.patch, db.delete
    return originals


def _restore(originals):
    (cd._supabase_get, cd._supabase_post, cd._supabase_patch, cd._supabase_delete,
     pa._supabase_get, pa._supabase_post, pa._supabase_patch, pa._supabase_delete) = originals


def _auto_create_stub(feature_id):
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_normalize_pane_scales():
    assert pa.normalize_pane_scales({"Main": "LOG", "volume": "bogus", "": "log"}) == {
        "main": "log",
        "volume": "linear",
    }
    assert pa.normalize_pane_scales(None) == {}
    assert pa.normalize_pane_scales(["main"]) == {}


def test_resolve_member_pane_legacy_defaults():
    assert pa.resolve_member_pane(None, "overlay_line", "sma_50") == "main"
    assert pa.resolve_member_pane(None, "sub_line", "ibd_rs") == "ibd_rs"
    assert pa.resolve_member_pane("RS", "sub_line", "ibd_rs") == "rs"
    assert pa.resolve_member_pane("none", "topbar_metric", "ibd_rs") == "none"


def test_assign_pane_ids_avoids_collisions():
    ids = pa._assign_pane_ids([None, None, "Main"], ["rs_monitor", "rs_monitor", "qmaggi__main"])
    assert ids == ["rs_monitor", "rs_monitor_2", "main"]


def test_legacy_members_become_pane_presets():
    db = FakeDB()
    originals = _install(db)
    original_auto = pa._auto_create_feature_if_missing
    pa._auto_create_feature_if_missing = _auto_create_stub
    try:
        preset = pa.ChartPresetIn(
            id="demo", display_name="Demo",
            members=[
                pa.PaneMember(feature_id="sma_50", pane="main", sort_order=0),
                pa.PaneMember(feature_id="ibd_rs", pane="rs", sort_order=1),
                pa.PaneMember(feature_id="rs_adr_neutral", pane="rs", sort_order=2),
            ],
        )
        asyncio.run(pa.legacy_create_preset(preset))
    finally:
        pa._auto_create_feature_if_missing = original_auto
        _restore(originals)

    panes = [p for p in db.tables["pca_chart_preset_panes"] if p["chart_preset_id"] == "demo"]
    by_id = {p["pane_id"]: p for p in panes}
    assert set(by_id) == {"main", "rs", "volume"}, by_id.keys()
    assert by_id["main"]["pane_preset_id"] == "demo__main"
    assert by_id["main"]["sort_order"] == 0 and by_id["main"]["weight"] == 7
    assert by_id["volume"]["pane_preset_id"] == pa.VOLUME_PANE_PRESET
    members = {m["pane_preset_id"]: m["feature_id"] for m in db.tables["pca_pane_preset_members"]}
    assert members["demo__rs"] in ("ibd_rs", "rs_adr_neutral")


def test_chart_without_price_pane_gets_candles():
    db = FakeDB()
    originals = _install(db)
    try:
        db.post("pca_pane_presets", {"id": "rs_only", "display_name": "RS", "role": "value", "default_scale": "linear"})
        chart = pa.ChartPresetIn(id="only_rs", display_name="Nur RS", panes=[pa.ChartPaneIn(pane_id="rs", pane_preset_id="rs_only")])
        asyncio.run(pa.create_chart(chart))
    finally:
        _restore(originals)
    panes = [p for p in db.tables["pca_chart_preset_panes"] if p["chart_preset_id"] == "only_rs"]
    assert panes[0]["pane_id"] == "main"
    assert panes[0]["pane_preset_id"] == pa.CANDLES_PANE_PRESET
    assert [p["pane_id"] for p in panes] == ["main", "rs", "volume"]


def test_chart_full_resolves_series_and_zones():
    db = FakeDB()
    originals = _install(db)
    try:
        db.post("pca_pane_presets", {
            "id": "rs_monitor", "display_name": "RS-Monitor", "role": "value", "kind": "indicator",
            "default_scale": "linear", "refs": [{"value": 30}], "derives": [{"id": "revival"}], "zones": [{"from": "revival"}],
            "archived": False,
        })
        db.post("pca_pane_preset_members", {"pane_preset_id": "rs_monitor", "feature_id": "ibd_rs", "sort_order": 0,
                                            "style_override": {"width": 2}, "rules": {"thresholds": [{"above": 80}]}})
        db.post("pca_pane_presets", {"id": "demo__main", "display_name": "Chart", "role": "price", "kind": "indicator",
                                     "default_scale": "linear", "archived": False})
        db.post("pca_chart_presets", {"id": "demo", "display_name": "Demo", "description": "", "topbar_metrics": ["ibd_rs"]})
        db.post("pca_chart_preset_panes", {"chart_preset_id": "demo", "pane_id": "main", "pane_preset_id": "demo__main",
                                           "sort_order": 0, "weight": 7, "scale": "log", "overrides": {}})
        db.post("pca_chart_preset_panes", {"chart_preset_id": "demo", "pane_id": "rs", "pane_preset_id": "rs_monitor",
                                           "sort_order": 1, "weight": 3, "scale": "linear", "overrides": {}})
        chart = asyncio.run(pa.get_chart("demo"))
    finally:
        _restore(originals)

    assert [p["pane_id"] for p in chart["panes"]] == ["main", "rs"]
    assert chart["panes"][1]["preset"]["zones"] == [{"from": "revival"}]
    series = chart["panes"][1]["preset"]["series"][0]
    assert series["canonical_id"] == "ibd_rs"
    assert series["style"]["width"] == 2 and series["style"]["color"] == "#000"
    assert series["rules"] == {"thresholds": [{"above": 80}]}
    assert chart["topbar_metrics"] == ["ibd_rs"]


def test_legacy_get_preset_is_flat_with_panes():
    db = FakeDB()
    originals = _install(db)
    try:
        db.post("pca_pane_presets", {"id": "demo__rs", "display_name": "Rs", "role": "value", "kind": "indicator",
                                     "default_scale": "linear", "archived": False})
        db.post("pca_pane_preset_members", {"pane_preset_id": "demo__rs", "feature_id": "rs_adr_neutral",
                                            "sort_order": 0, "style_override": {}, "rules": {}})
        db.post("pca_chart_presets", {"id": "demo", "display_name": "Demo", "description": "d", "topbar_metrics": []})
        db.post("pca_chart_preset_panes", {"chart_preset_id": "demo", "pane_id": "rs", "pane_preset_id": "demo__rs",
                                           "sort_order": 0, "weight": 3, "scale": "log", "overrides": {}})
        flat = asyncio.run(pa.legacy_get_preset("demo"))
    finally:
        _restore(originals)
    assert flat["name"] == "demo"
    assert flat["pane_scales"] == {"rs": "log"}
    assert flat["indicators"][0]["pane"] == "rs"
    assert flat["indicators"][0]["column"] == "rs_adr_neutral"


def test_patch_sets_scale_per_pane_row():
    db = FakeDB()
    originals = _install(db)
    try:
        db.post("pca_chart_presets", {"id": "demo", "display_name": "Demo", "topbar_metrics": []})
        db.post("pca_chart_preset_panes", {"chart_preset_id": "demo", "pane_id": "main", "pane_preset_id": "x",
                                           "sort_order": 0, "weight": 7, "scale": "linear", "overrides": {}})
        db.post("pca_chart_preset_panes", {"chart_preset_id": "demo", "pane_id": "rs", "pane_preset_id": "y",
                                           "sort_order": 1, "weight": 2, "scale": "linear", "overrides": {}})
        result = asyncio.run(pa.update_chart_pane_scales("demo", {"pane_scales": {"main": "log", "rs": "bogus"}}))
    finally:
        _restore(originals)
    rows = {p["pane_id"]: p for p in db.tables["pca_chart_preset_panes"]}
    assert rows["main"]["scale"] == "log" and rows["rs"]["scale"] == "linear"
    assert result["pane_scales"] == {"main": "log", "rs": "linear"}


def test_patch_unknown_chart_is_404():
    db = FakeDB()
    originals = _install(db)
    try:
        try:
            asyncio.run(pa.update_chart_pane_scales("nope", {"pane_scales": {"main": "log"}}))
        except HTTPException as e:
            assert e.status_code == 404
        else:
            raise AssertionError("expected HTTPException 404")
    finally:
        _restore(originals)


def test_hard_delete_refuses_when_chart_uses_pane():
    db = FakeDB()
    originals = _install(db)
    try:
        db.post("pca_pane_presets", {"id": "rs_monitor", "display_name": "RS", "role": "value", "archived": False})
        db.post("pca_chart_preset_panes", {"chart_preset_id": "demo", "pane_id": "rs", "pane_preset_id": "rs_monitor"})
        try:
            asyncio.run(pa.delete_pane("rs_monitor", hard=True))
        except HTTPException as e:
            assert e.status_code == 409
        else:
            raise AssertionError("expected HTTPException 409")
        asyncio.run(pa.delete_pane("rs_monitor", hard=False))
    finally:
        _restore(originals)
    assert db.tables["pca_pane_presets"][0]["archived"] is True


def test_chart_models_accept_pane_scales():
    chart = pa.ChartPresetIn(id="x", display_name="X", pane_scales={"main": "log"})
    assert chart.pane_scales == {"main": "log"}
    assert pa.ChartPresetIn(id="x", display_name="X").pane_scales is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} Tests bestanden")
