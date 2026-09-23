#!/usr/bin/env python3
"""Phase 12 — Lab-RS-Features in pca_features registrieren + Preset-Mitglieder ergaenzen."""
from __future__ import annotations

import httpx
from lab_common import SUPABASE_KEY, SUPABASE_URL, supabase_get

# Spalte, Anzeigename, Farbe
VARIANTS = [
    ("lab_rs",      "Lab RS (ROC 63/126/252)", "#00E5FF"),
    ("lab_rs_112",  "Lab RS 1-1-2",            "#FF6D00"),
    ("lab_rs_123",  "Lab RS 1-2-3",            "#76FF03"),
    ("lab_rs_1111", "Lab RS 4-Periode",        "#D500F9"),
    ("lab_rs_212",  "Lab RS 2-1-2",            "#FFD600"),
    ("lab_rs_20_40_80",   "Lab RS 20/40/80",   "#FF5252"),
    ("lab_rs_30_60_120",  "Lab RS 30/60/120",  "#FF4081"),
    ("lab_rs_40_80_160",  "Lab RS 40/80/160",  "#FF1744"),
    ("lab_rs_50_100_200", "Lab RS 50/100/200", "#FFAB40"),
    ("lab_rs_n",             "Lab RS neutral",             "#00B0FF"),
    ("lab_rs_n_20_40_80",    "Lab RS neutral 20/40/80",    "#82B1FF"),
    ("lab_rs_n_30_60_120",   "Lab RS neutral 30/60/120",   "#448AFF"),
    ("lab_rs_n_50_100_200",  "Lab RS neutral 50/100/200",  "#2962FF"),
]
HEAD = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY}


def post(table, rows, on_conflict=None, prefer="return=minimal"):
    params = {"on_conflict": on_conflict} if on_conflict else None
    prefer_full = ("resolution=merge-duplicates," if on_conflict else "") + prefer
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{table}", params=params, json=rows,
                   headers={**HEAD, "Prefer": prefer_full}, timeout=120)
    r.raise_for_status()
    return r


def main() -> None:
    feats = [{
        "canonical_id": col, "alias": None, "display_name": name,
        "calc_type": "IBD_RS", "calc_params": {"variant": col, "kind": "lab_rs"}, "plot_type": "sub_line",
        "default_style": {"color": color, "width": 2}, "mode": "online", "depends_on": None,
    } for col, name, color in VARIANTS]
    post("pca_features", feats, on_conflict="canonical_id")
    print(f"pca_features upsert: {len(feats)}")

    # alte Lab-Mitglieder entfernen (idempotent)
    httpx.delete(f"{SUPABASE_URL}/rest/v1/pca_feature_set_members",
                 params={"feature_id": "like.lab_%"}, headers={**HEAD, "Prefer": "return=minimal"},
                 timeout=60).raise_for_status()

    sets = supabase_get("pca_feature_sets", {"select": "id,display_name"})
    rows = []
    for s in sets:
        for i, (col, _, _) in enumerate(VARIANTS):
            pane = "lab_rs_n" if col.startswith("lab_rs_n") else "lab_rs"
            rows.append({"set_id": s["id"], "feature_id": col, "sort_order": 100 + i,
                         "style_override": None, "pane": pane})
    post("pca_feature_set_members", rows)
    print(f"pca_feature_set_members: {len(rows)} Zeilen in {len(sets)} Presets")
    print("Presets:", [s["id"] for s in sets])


if __name__ == "__main__":
    main()
