#!/usr/bin/env python3
"""Teardown des RS-Labs.

Entfernt: lab_*-Watchlists, lab_*-Features aus pca_features und die lab_*-
Preset-Mitglieder. Die 1D_lab_rs.parquet-Dateien und der chart_data.py-Merge
werden von teardown.sh bzw. manuell entfernt.
"""
from __future__ import annotations

import httpx
from lab_common import SUPABASE_KEY, SUPABASE_URL

H = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY, "Prefer": "return=minimal"}


def main() -> None:
    for table, flt, val in (
        ("pca_watchlists", "list_name", "like.lab_%"),
        ("pca_feature_set_members", "feature_id", "like.lab_%"),
        ("pca_features", "canonical_id", "like.lab_%"),
        ("pca_feature_sets", "id", "eq.qmaggi_lab"),
        ("pca_features", "canonical_id", "eq.ma_sma_50_dollar_volume"),
    ):
        r = httpx.delete(f"{SUPABASE_URL}/rest/v1/{table}", params={flt: val}, headers=H, timeout=60)
        r.raise_for_status()
        print(f"{table}: lab_* geloescht (HTTP {r.status_code})")
    print("Hinweis: 1D_lab_rs.parquet entfernt teardown.sh; chart_data.py-Merge manuell entfernen.")


if __name__ == "__main__":
    main()
