"""Selbsttest der pane_scales-Logik des Preset-APIs (ohne DB, ohne pytest).

Lauf im Service-Container:
    docker exec qjm-pca-service python test_preset_pane_scales.py

Geprueft wird die Validierung (normalize_pane_scales) und das Merge-Verhalten
des Teil-Updates PATCH /api/presets/{id}/pane_scales: ein Klick im Chart-Viewer
darf nur die Y-Skala des betroffenen Panes aendern, niemals die Preset-Member.
"""

import asyncio

from fastapi import HTTPException

import chart_data as cd


def test_normalize_pane_scales():
    assert cd.normalize_pane_scales({"Main": "LOG", "volume": "bogus", "": "log"}) == {
        "main": "log",
        "volume": "linear",
    }
    assert cd.normalize_pane_scales(None) == {}
    assert cd.normalize_pane_scales(["main"]) == {}


def _with_stubs(rows, patch_calls):
    def fake_get(table, params=""):
        return rows

    def fake_patch(table, params, payload):
        patch_calls.append((table, params, payload))

    original = (cd._supabase_get, cd._supabase_patch)
    cd._supabase_get, cd._supabase_patch = fake_get, fake_patch
    return original


def test_patch_merges_and_keeps_other_panes():
    patch_calls = []
    original = _with_stubs([{"id": "demo", "pane_scales": {"main": "log", "volume": "linear"}}], patch_calls)
    try:
        result = asyncio.run(
            cd.update_preset_pane_scales("demo", cd.PaneScalesUpdate(pane_scales={"main": "linear"}))
        )
    finally:
        cd._supabase_get, cd._supabase_patch = original

    assert patch_calls == [
        ("pca_feature_sets", "id=eq.demo", {"pane_scales": {"main": "linear", "volume": "linear"}})
    ]
    assert result["pane_scales"] == {"main": "linear", "volume": "linear"}


def test_patch_unknown_preset_is_404():
    original = _with_stubs([], [])
    try:
        try:
            asyncio.run(cd.update_preset_pane_scales("nope", cd.PaneScalesUpdate(pane_scales={"main": "log"})))
        except HTTPException as e:
            assert e.status_code == 404
        else:
            raise AssertionError("expected HTTPException 404")
    finally:
        cd._supabase_get, cd._supabase_patch = original


def test_preset_models_accept_pane_scales():
    preset = cd.PresetCreate(id="x", display_name="X", pane_scales={"main": "log"})
    assert preset.pane_scales == {"main": "log"}
    # Ohne Angabe bleibt das Feld unangetastet (PUT darf Skalen nicht loeschen).
    assert cd.PresetCreate(id="x", display_name="X").pane_scales is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} Tests bestanden")
