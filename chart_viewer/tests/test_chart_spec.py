"""Tests der Chart-Definition-Aufloesung (Pane-Presets -> Overlays).

Deckt das RS-Monitor-Beispiel ab: zwei Serien mit Schwellenfarben, die
30->80-Phase als Zone und die Referenzlinien 30/80.
"""

from __future__ import annotations

from chart_viewer import chart_spec


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _rs_rows(n: int = 8):
    """Zeilen mit den Spalten, die das RS-Monitor-Pane braucht."""
    ibd = [50, 20, 60, 85, 90, 40, 82, 10][:n]
    rs_adr = [40, 25, 55, 78, 88, 45, 84, 15][:n]
    rows = []
    for i in range(n):
        rows.append([1_700_000_000 + i * 86400, 100 + i, 101 + i, 99 + i, 100.5 + i, 1000 + i, ibd[i], rs_adr[i]])
    return rows


COLS = ["timestamp", "open", "high", "low", "close", "volume", "ibd_rs", "rs_adr_neutral"]
COL_IDX = {name: i for i, name in enumerate(COLS)}


def _bars(rows):
    return [
        {
            "t_open": int(r[0]),
            "t_close": int(r[0]) + 86400,
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        }
        for r in rows
    ]


def _series(canonical_id, **kw):
    base = {
        "feature_id": canonical_id,
        "column": canonical_id,
        "canonical_id": canonical_id,
        "display_name": canonical_id,
        "plot_type": "sub_line",
        "style": {},
        "rules": {},
    }
    base.update(kw)
    return base


def _rs_monitor_preset():
    thresholds = {"thresholds": [{"below": 30, "color": "#EF5350"}, {"above": 80, "color": "#26A69A"}]}
    return {
        "id": "rs_monitor",
        "display_name": "RS-Monitor",
        "role": "value",
        "kind": "indicator",
        "default_scale": "linear",
        "series": [
            _series("ibd_rs", style={"color": "#B0BEC5", "width": 2}, rules=thresholds),
            _series("rs_adr_neutral", style={"color": "#42A5F5", "width": 1}, rules=thresholds),
        ],
        "derives": [{"id": "revival", "fn": "cross_window", "series": "ibd_rs", "low": 30, "high": 80, "within": 20}],
        "zones": [{"from": "revival", "style": {"color": "#26A69A", "alpha": 40}, "label": "30->80"}],
        "refs": [
            {"value": 30, "label": "30", "style": {"dash": True}},
            {"value": 80, "label": "80", "style": {"dash": True}},
        ],
    }


def _rs_chart_spec():
    return chart_spec.normalize_chart_spec(
        {
            "id": "rs_monitor_chart",
            "display_name": "RS-Monitor",
            "topbar_metrics": ["ibd_rs"],
            "panes": [
                {"pane_id": "main", "pane_preset_id": "candles", "weight": 7, "scale": "log",
                 "preset": {"id": "candles", "display_name": "Kerzen", "role": "price", "kind": "builtin",
                            "series": [], "refs": [], "derives": [], "zones": []}},
                {"pane_id": "rs", "pane_preset_id": "rs_monitor", "weight": 3, "scale": "linear",
                 "preset": _rs_monitor_preset()},
            ],
        }
    )


def _resolve_column(column):
    return column if column in COL_IDX else None


def _no_calc(symbol, indicator_type, periods, timeframe, limit):
    raise AssertionError("calculate darf hier nicht aufgerufen werden")


# ---------------------------------------------------------------------------
# normalize_chart_spec
# ---------------------------------------------------------------------------


def test_normalize_keeps_price_pane_first_and_adds_volume():
    spec = _rs_chart_spec()
    assert [p["pane_id"] for p in spec["panes"]] == ["main", "rs", "volume"]
    assert spec["panes"][0]["weight"] == 7
    assert spec["panes"][1]["scale"] == "linear"
    assert spec["panes"][2]["preset"]["role"] == "volume"


def test_normalize_moves_price_role_to_main_slot():
    spec = chart_spec.normalize_chart_spec(
        {
            "id": "x",
            "panes": [
                {"pane_id": "rs", "pane_preset_id": "rs_monitor", "preset": _rs_monitor_preset()},
                {"pane_id": "chart", "pane_preset_id": "smas", "preset": {"id": "smas", "role": "price", "series": []}},
            ],
        }
    )
    assert spec["panes"][0]["pane_id"] == "main"
    assert spec["panes"][0]["preset"]["id"] == "smas"
    assert spec["panes"][1]["pane_id"] == "rs"


def test_normalize_makes_pane_ids_unique():
    spec = chart_spec.normalize_chart_spec(
        {
            "id": "x",
            "panes": [
                {"pane_preset_id": "rs_monitor", "preset": _rs_monitor_preset()},
                {"pane_preset_id": "rs_monitor", "preset": _rs_monitor_preset()},
            ],
        }
    )
    ids = [p["pane_id"] for p in spec["panes"]]
    assert ids[0] == "main"  # erstes Pane wird Preispane
    assert len(set(ids)) == len(ids)


def test_normalize_without_price_pane_uses_candles():
    spec = chart_spec.normalize_chart_spec(
        {"id": "x", "panes": [{"pane_id": "rs", "pane_preset_id": "rs_monitor", "preset": _rs_monitor_preset()}]}
    )
    assert spec["panes"][0]["pane_id"] == "main"
    assert spec["panes"][0]["pane_preset_id"] == chart_spec.CANDLES_PRESET


# ---------------------------------------------------------------------------
# Phasen (30 -> 80 in 20 Bars)
# ---------------------------------------------------------------------------


def test_derive_phases_finds_revival():
    points = [(0, 50.0), (1, 20.0), (2, 60.0), (3, 85.0), (4, 90.0), (5, 40.0), (6, 82.0), (7, 10.0)]
    assert chart_spec.derive_phases(points, 30, 80, 20) == [(1, 3)]


def test_derive_phases_respects_window():
    points = [(0, 50.0), (1, 20.0), (2, 60.0), (5, 85.0)]
    assert chart_spec.derive_phases(points, 30, 80, 2) == []
    assert chart_spec.derive_phases(points, 30, 80, 4) == [(1, 5)]


def test_derive_phases_consumes_low_once():
    points = [(0, 20.0), (1, 85.0), (2, 90.0)]
    assert chart_spec.derive_phases(points, 30, 80, 20) == [(0, 1)]


def test_derive_phases_two_cycles():
    points = [(0, 20.0), (1, 85.0), (2, 25.0), (3, 88.0)]
    assert chart_spec.derive_phases(points, 30, 80, 20) == [(0, 1), (2, 3)]


# ---------------------------------------------------------------------------
# Schwellenfarben
# ---------------------------------------------------------------------------


def test_threshold_color():
    rules = {"thresholds": [{"below": 30, "color": "#EF5350"}, {"above": 80, "color": "#26A69A"}]}
    assert chart_spec.threshold_color(20, "#FFF", rules) == "#EF5350"
    assert chart_spec.threshold_color(85, "#FFF", rules) == "#26A69A"
    assert chart_spec.threshold_color(50, "#FFF", rules) == "#FFF"
    assert chart_spec.threshold_color(None, "#FFF", rules) == "#FFF"
    assert chart_spec.threshold_color(50, "#FFF", {}) == "#FFF"


# ---------------------------------------------------------------------------
# build_overlays: das RS-Monitor-Beispiel
# ---------------------------------------------------------------------------


def test_build_overlays_rs_monitor():
    rows = _rs_rows()
    bars = _bars(rows)
    built = chart_spec.build_overlays(
        _rs_chart_spec(), bars, rows, COL_IDX, _resolve_column, _no_calc, "NVDA", "1D", 2000
    )

    by_id = {ov["overlay_id"]: ov for ov in built["overlays"]}

    # Serien
    ibd = by_id["ibd_rs"]
    assert ibd["type"] == "line" and ibd["pane"] == "rs"
    colors = {pt["t"]: pt.get("color_override") for pt in ibd["values"]}
    ts = [int(r[0]) for r in rows]
    assert colors[ts[1]] == "#EF5350"   # 20 -> unter 30
    assert colors[ts[3]] == "#26A69A"   # 85 -> ueber 80
    assert colors[ts[0]] is None        # 50 -> Serienfarbe
    assert ibd["style"] == {"color": "#B0BEC5", "width": 2}
    assert by_id["rs_adr_neutral"]["style"] == {"color": "#42A5F5", "width": 1}

    # Zone aus der Phase (Bar 1 -> Bar 3)
    zones = [ov for ov in built["overlays"] if ov["type"] == "zone"]
    assert len(zones) == 1
    zone = zones[0]
    assert zone["pane"] == "rs"
    assert zone["style"]["alpha"] == 40 and zone["style"]["label"] == "30->80"
    assert [(v["t"], v["t2"]) for v in zone["values"]] == [(ts[1], ts[3])]
    assert zone["values"][0]["value"] is None  # volle Pane-Hoehe

    # Referenzlinien 30/80
    levels = sorted((ov for ov in built["overlays"] if ov["type"] == "level"), key=lambda o: o["values"][0]["value"])
    assert [lv["values"][0]["value"] for lv in levels] == [30.0, 80.0]
    assert [lv["style"]["label"] for lv in levels] == ["30", "80"]
    assert all(lv["style"]["dash"] is True for lv in levels)

    # Zeichenreihenfolge: Zonen vor Linien
    types = [ov["type"] for ov in built["overlays"]]
    assert types.index("zone") < types.index("line")

    # Panes-Metadaten + Volumen
    assert [p["pane_id"] for p in built["panes"]] == ["main", "rs", "volume"]
    assert built["panes"][1] == {"pane_id": "rs", "role": "value", "title": "RS-Monitor",
                                 "weight": 3, "scale": "linear", "preset_id": "rs_monitor"}
    volume = by_id["volume_volume"]
    assert volume["type"] == "histogram" and volume["pane"] == "volume"
    assert len(volume["values"]) == len(rows)
    assert built["pane_scales"] == {"main": "log", "rs": "linear", "volume": "linear"}
    assert built["missing"] == []


def test_build_overlays_reports_missing_series():
    preset = _rs_monitor_preset()
    preset["series"] = [_series("voellig_unbekannt")]
    preset["derives"] = []
    preset["zones"] = []
    spec = chart_spec.normalize_chart_spec(
        {"id": "x", "panes": [{"pane_id": "rs", "pane_preset_id": "p", "preset": preset}]}
    )
    built = chart_spec.build_overlays(spec, _bars(_rs_rows()), _rs_rows(), COL_IDX, _resolve_column, _no_calc, "NVDA", "1D", 2000)
    assert "voellig_unbekannt" in built["missing"]


def test_build_overlays_calculates_missing_columns():
    preset = {
        "id": "sma_pane", "display_name": "SMA", "role": "value", "series": [_series("sma_7", style={"color": "#FFF"})],
        "refs": [], "derives": [], "zones": [],
    }
    spec = chart_spec.normalize_chart_spec({"id": "x", "panes": [{"pane_id": "rs", "pane_preset_id": "p", "preset": preset}]})
    rows = _rs_rows()
    ts = [int(r[0]) for r in rows]
    calls = []

    def fake_calc(symbol, indicator_type, periods, timeframe, limit):
        calls.append((indicator_type, tuple(periods)))
        return {"timestamps": ts, "series": {"sma_7": [None, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]}}

    built = chart_spec.build_overlays(spec, _bars(rows), rows, COL_IDX, _resolve_column, fake_calc, "NVDA", "1D", 2000)
    assert calls == [("SMA", (7,))]
    line = [ov for ov in built["overlays"] if ov["overlay_id"] == "sma_7"][0]
    assert len(line["values"]) == 7  # der None-Wert fehlt
    assert built["missing"] == []


def test_build_overlays_band_from_bollinger():
    preset = {
        "id": "bb", "display_name": "BB", "role": "value",
        "series": [_series("bb_20_upper", column="bb_20_upper", plot_type="overlay_band", style={"color": "#26A69A", "alpha": 25})],
        "refs": [], "derives": [], "zones": [],
    }
    spec = chart_spec.normalize_chart_spec({"id": "x", "panes": [{"pane_id": "bb", "pane_preset_id": "p", "preset": preset}]})
    rows = _rs_rows()
    cols = COLS + ["bb_20_upper", "bb_20_lower"]
    col_idx = {name: i for i, name in enumerate(cols)}
    rows = [r + [r[4] * 1.02, r[4] * 0.98] for r in rows]
    built = chart_spec.build_overlays(spec, _bars(rows), rows, col_idx, lambda c: c if c in col_idx else None,
                                      _no_calc, "NVDA", "1D", 2000)
    band = [ov for ov in built["overlays"] if ov["type"] == "band"][0]
    assert band["overlay_id"] == "bb_20_upper"
    assert band["values"][0]["value"] > band["values"][0]["value2"]


# ---------------------------------------------------------------------------
# Warnkanal (Plan Phase 4)
# ---------------------------------------------------------------------------


def _single_pane_spec(preset):
    return chart_spec.normalize_chart_spec(
        {"id": "x", "panes": [{"pane_id": "rs", "pane_preset_id": "p", "preset": preset}]}
    )


def test_build_overlays_reports_unknown_column_as_warning():
    preset = _rs_monitor_preset()
    preset["series"] = [_series("voellig_unbekannt")]
    preset["derives"] = []
    preset["zones"] = []
    built = chart_spec.build_overlays(
        _single_pane_spec(preset), _bars(_rs_rows()), _rs_rows(), COL_IDX, _resolve_column, _no_calc,
        "NVDA", "1D", 2000,
    )
    assert {"code": "unknown_column", "pane_id": "rs", "detail": "voellig_unbekannt"} in built["warnings"]


def test_build_overlays_warns_when_a_series_has_no_data():
    preset = {
        "id": "leer", "display_name": "Leer", "role": "value",
        "series": [_series("ibd_rs")], "refs": [], "derives": [], "zones": [],
    }
    rows = [[r[0], r[1], r[2], r[3], r[4], r[5], None, None] for r in _rs_rows()]
    built = chart_spec.build_overlays(
        _single_pane_spec(preset), _bars(rows), rows, COL_IDX, _resolve_column, _no_calc,
        "NVDA", "1D", 2000,
    )
    assert {"code": "series_without_data", "pane_id": "rs", "detail": "ibd_rs"} in built["warnings"]


def test_build_overlays_warns_about_a_pane_without_content():
    preset = {"id": "nackt", "display_name": "Nackt", "role": "value",
              "series": [], "refs": [], "derives": [], "zones": []}
    built = chart_spec.build_overlays(
        _single_pane_spec(preset), _bars(_rs_rows()), _rs_rows(), COL_IDX, _resolve_column, _no_calc,
        "NVDA", "1D", 2000,
    )
    assert {"code": "pane_without_series", "pane_id": "rs", "detail": "nackt"} in built["warnings"]


def test_builtin_panes_do_not_warn():
    built = chart_spec.build_overlays(
        _rs_chart_spec(), _bars(_rs_rows()), _rs_rows(), COL_IDX, _resolve_column, _no_calc,
        "NVDA", "1D", 2000,
    )
    assert built["warnings"] == []
    assert built["missing"] == []


# ---------------------------------------------------------------------------
# Pane-Overrides (Chart-Ebene, Plan Phase 3)
# ---------------------------------------------------------------------------


def test_normalize_keeps_pane_overrides_and_applies_them():
    spec = chart_spec.normalize_chart_spec(
        {
            "id": "x",
            "panes": [
                {"pane_id": "rs", "pane_preset_id": "rs_monitor", "preset": _rs_monitor_preset(),
                 "overrides": {"title": "RS neu", "scale": "log"}},
            ],
        }
    )
    rs = [p for p in spec["panes"] if p["pane_id"] == "rs"][0]
    assert rs["overrides"] == {"title": "RS neu", "scale": "log"}
    assert rs["preset"]["display_name"] == "RS neu"
    assert rs["scale"] == "log"
    assert spec["override_warnings"] == []


def test_apply_pane_overrides_merges_series_style_and_rules():
    preset = _rs_monitor_preset()
    merged, warnings = chart_spec.apply_pane_overrides(
        preset,
        {"series": {"ibd_rs": {"style": {"color": "#123456", "width": 3}, "rules": {"thresholds": []}}}},
        "rs",
    )
    assert warnings == []
    ibd = [s for s in merged["series"] if s["canonical_id"] == "ibd_rs"][0]
    assert ibd["style"]["color"] == "#123456" and ibd["style"]["width"] == 3
    assert ibd["rules"]["thresholds"] == []
    # Das Preset bleibt unangetastet (deepcopy).
    original = [s for s in preset["series"] if s["canonical_id"] == "ibd_rs"][0]
    assert original["style"]["color"] == "#B0BEC5"
    assert original["rules"]["thresholds"] == [{"below": 30, "color": "#EF5350"}, {"above": 80, "color": "#26A69A"}]


def test_apply_pane_overrides_hides_series_and_replaces_refs():
    merged, warnings = chart_spec.apply_pane_overrides(
        _rs_monitor_preset(),
        {"hide_series": ["rs_adr_neutral"], "refs": [{"value": 50, "label": "50"}]},
        "rs",
    )
    assert warnings == []
    assert [s["canonical_id"] for s in merged["series"]] == ["ibd_rs"]
    assert [r["value"] for r in merged["refs"]] == [50]


def test_apply_pane_overrides_reports_unknown_keys_and_series():
    _, warnings = chart_spec.apply_pane_overrides(
        _rs_monitor_preset(),
        {
            "farbe": "rot",
            "series_bogus": 1,
            "series": {"gibt_es_nicht": {"style": {"color": "#fff"}}, "ibd_rs": {"stil": 1}},
            "hide_series": ["auch_nicht"],
        },
        "rs",
    )
    pairs = {(w["code"], w["detail"]) for w in warnings}
    assert ("unknown_override", "farbe") in pairs
    assert ("unknown_override", "series_bogus") in pairs
    assert ("unknown_series_override", "gibt_es_nicht") in pairs
    assert ("unknown_series_override", "auch_nicht") in pairs
    assert ("unknown_override", "series.stil") in pairs


def test_apply_pane_overrides_rejects_a_bad_scale():
    _, warnings = chart_spec.apply_pane_overrides(_rs_monitor_preset(), {"scale": "wurzel"}, "rs")
    assert warnings == [{"code": "invalid_override", "pane_id": "rs", "detail": "scale=wurzel"}]


def test_diff_chart_specs_reports_added_removed_changed_and_order():
    window = [
        {"pane_id": "main", "pane_preset_id": "chart_main", "scale": "linear", "weight": 7},
        {"pane_id": "rs", "pane_preset_id": "rs_monitor", "scale": "linear", "weight": 3},
    ]
    chart = [
        {"pane_id": "main", "pane_preset_id": "chart_main", "scale": "log", "weight": 5},
        {"pane_id": "adr", "pane_preset_id": "adr_pane", "scale": "linear", "weight": 2},
        {"pane_id": "rs", "pane_preset_id": "rs_monitor", "scale": "linear", "weight": 3},
    ]
    diff = chart_spec.diff_chart_specs(
        window, chart,
        {"topbar_metrics": ["close"], "x_axis_pane": None},
        {"topbar_metrics": ["ibd_rs"], "x_axis_pane": "main"},
    )
    assert diff["same"] is False
    assert [p["pane_id"] for p in diff["added"]] == ["adr"]
    assert diff["removed"] == []
    main = [c for c in diff["changed"] if c["pane_id"] == "main"][0]
    assert main["fields"]["scale"] == {"window": "linear", "chart": "log"}
    assert main["fields"]["weight"] == {"window": 7, "chart": 5}
    assert diff["reordered"] == [{"pane_id": "rs", "from": 1, "to": 2}]
    assert diff["topbar_metrics"] == {"window": ["close"], "chart": ["ibd_rs"]}
    assert diff["x_axis_pane"] == {"window": None, "chart": "main"}


def test_diff_chart_specs_understands_the_api_shape():
    """Die PCA-API liefert die Preset-Id verschachtelt unter preset.id."""
    window = [{"pane_id": "main", "pane_preset_id": "default__main", "scale": "linear", "weight": 7}]
    chart = [{"pane_id": "main", "scale": "linear", "weight": 7,
              "preset": {"id": "default__main", "role": "price"}}]
    diff = chart_spec.diff_chart_specs(window, chart)
    assert diff["same"] is True
    assert diff["changed"] == []


def test_diff_chart_specs_identical_is_same():
    panes = [{"pane_id": "main", "pane_preset_id": "candles", "scale": "linear", "weight": 7}]
    diff = chart_spec.diff_chart_specs(
        panes, [dict(p) for p in panes],
        {"topbar_metrics": ["close"]}, {"topbar_metrics": ["close"]},
    )
    assert diff["same"] is True
    assert diff["added"] == [] and diff["removed"] == [] and diff["changed"] == []


def test_diff_chart_specs_detects_removed_and_override_changes():
    window = [
        {"pane_id": "main", "pane_preset_id": "candles", "overrides": {}},
        {"pane_id": "vol", "pane_preset_id": "builtin:volume"},
    ]
    chart = [
        {"pane_id": "main", "pane_preset_id": "candles", "overrides": {"title": "Neu"}},
    ]
    diff = chart_spec.diff_chart_specs(window, chart)
    assert [p["pane_id"] for p in diff["removed"]] == ["vol"]
    main = diff["changed"][0]
    assert main["pane_id"] == "main"
    assert main["fields"]["overrides"] == {"window": {}, "chart": {"title": "Neu"}}


def test_build_overlays_uses_the_overrides():
    spec = chart_spec.normalize_chart_spec(
        {
            "id": "x",
            "panes": [
                {"pane_id": "rs", "pane_preset_id": "rs_monitor", "preset": _rs_monitor_preset(),
                 "overrides": {
                     "series": {"ibd_rs": {"style": {"color": "#123456"}}},
                     "hide_series": ["rs_adr_neutral"],
                 }},
            ],
        }
    )
    built = chart_spec.build_overlays(
        spec, _bars(_rs_rows()), _rs_rows(), COL_IDX, _resolve_column, _no_calc, "NVDA", "1D", 2000,
    )
    ids = {ov["overlay_id"] for ov in built["overlays"]}
    assert "rs_adr_neutral" not in ids
    ibd = [ov for ov in built["overlays"] if ov["overlay_id"] == "ibd_rs"][0]
    assert ibd["style"]["color"] == "#123456"
    assert built["warnings"] == []
