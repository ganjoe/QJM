#!/usr/bin/env python3
"""Phase 1: Import features.json into Supabase pca_features + seed presets.

Reads the existing features.json, maps each feature to a canonical_id,
and inserts into the pca_features table. Also creates preset feature sets.

Usage:
    python3 import_features_to_supabase.py
"""

import json
import os
import subprocess
import sys

DB_CONTAINER = "openbrain-db"
DB_USER = "postgres"


def psql(sql: str) -> str:
    """Execute SQL via docker exec psql with stdin pipe."""
    result = subprocess.run(
        ["docker", "exec", "-i", DB_CONTAINER, "psql", "-U", DB_USER, "-t", "-A"],
        input=sql, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"SQL ERROR: {result.stderr}", file=sys.stderr)
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


# ── Feature Mapping: features.json entry → (canonical_id, calc_type, calc_params, plot_type) ──

FEATURE_MAP = {
    # Moving Averages
    "ma_sma_10":  ("sma_10",  "SMA", {"window": 10, "source": "close"}, "overlay_line", "SMA 10",  {"color": "#2962FF", "width": 2}),
    "ma_sma_20":  ("sma_20",  "SMA", {"window": 20, "source": "close"}, "overlay_line", "SMA 20",  {"color": "#2962FF", "width": 2}),
    "ma_sma_50":  ("sma_50",  "SMA", {"window": 50, "source": "close"}, "overlay_line", "SMA 50",  {"color": "#2962FF", "width": 2}),
    "ma_sma_100": ("sma_100", "SMA", {"window": 100, "source": "close"}, "overlay_line", "SMA 100", {"color": "#2962FF", "width": 2}),
    "ma_sma_150": ("sma_150", "SMA", {"window": 150, "source": "close"}, "overlay_line", "SMA 150", {"color": "#2962FF", "width": 2}),
    "ma_sma_200": ("sma_200", "SMA", {"window": 200, "source": "close"}, "overlay_line", "SMA 200", {"color": "#2962FF", "width": 2}),

    # Bollinger Bands
    "bb_20": ("bb_20", "BOLLINGER", {"window": 20, "source": "close", "std_dev": 2}, "overlay_band", "Bollinger 20", {"color": "#26A69A", "alpha": 30}),

    # Stochastic
    "stock_10_1": ("stoch_10_1", "STOCHASTIC", {"k_period": 10, "slowing": 1, "source": "close"}, "subchart", "Stochastic 10/1", {"color": "#FF5252", "width": 1}),

    # IBD RS Rating
    "ibd_rs": ("ibd_rs", "IBD_RS", {"source": "close"}, "topbar_metric", "IBD RS Rating", {}),

    # Minervini
    "minervini_score": ("minervini", "MINERVINI_TREND", {}, "topbar_metric", "Minervini Score", {}),
    # minervini_trend_template is a derived column from minervini, not a separate feature
    
    # Breadth
    "breadth_minervini": ("breadth_min", "BREADTH_MINERVINI", {"aggregation": "all"}, "topbar_metric", "Breadth Minervini", {}),
    # breadth_minervini_pct is a derived column from breadth_min

    # ADR
    "adr_20": ("adr_20", "ADR", {"window": 20}, "topbar_metric", "ADR 20", {}),

    # Dollar Volume
    "dollar_volume": ("dollar_volume", "DERIVED", {"formula": "close * volume"}, "topbar_metric", "Dollar Volume", {}),

    # SMA 50 Dollar Volume
    "ma_sma_50_dollar_volume": ("sma_50_dvol", "SMA", {"window": 50, "source": "dollar_volume"}, "topbar_metric", "SMA 50 Dollar Volume", {}),

    # Daily Range
    "daily_range": ("daily_range", "DERIVED", {"formula": "high - low"}, "topbar_metric", "Daily Range", {}),
}

# Features that depend on other features
DEPENDENCIES = {
    "sma_50_dvol": "dollar_volume",  # SMA 50 of dollar_volume
    "minervini": "ibd_rs",           # Minervini uses RS Rating for condition 8
}


def insert_features():
    """Insert all features from the mapping into pca_features."""
    count = 0
    for old_id, (canonical_id, calc_type, calc_params, plot_type, display_name, default_style) in FEATURE_MAP.items():
        depends_on = DEPENDENCIES.get(canonical_id)
        depends_sql = f"'{depends_on}'" if depends_on else "NULL"
        
        sql = f"""
        INSERT INTO pca_features (canonical_id, alias, display_name, calc_type, calc_params, plot_type, default_style, mode, depends_on)
        VALUES (
            '{canonical_id}',
            '{old_id}',
            '{display_name}',
            '{calc_type}',
            '{json.dumps(calc_params)}'::jsonb,
            '{plot_type}',
            '{json.dumps(default_style)}'::jsonb,
            'offline',
            {depends_sql}
        )
        ON CONFLICT (canonical_id) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            calc_type = EXCLUDED.calc_type,
            calc_params = EXCLUDED.calc_params,
            plot_type = EXCLUDED.plot_type,
            default_style = EXCLUDED.default_style,
            depends_on = EXCLUDED.depends_on;
        """
        psql(sql)
        count += 1
        print(f"  ✓ {old_id:30s} → {canonical_id}")
    
    return count


def insert_presets():
    """Insert preset feature sets."""
    presets = [
        {
            "id": "trend_template",
            "display_name": "Minervini Trend Template",
            "description": "6 SMAs for Stage 2 analysis",
            "topbar_metrics": ["ibd_rs", "minervini", "adr_20"],
            "members": [
                ("sma_10",  0, {"color": "#00BCD4", "width": 2}),   # Türkis
                ("sma_20",  1, {"color": "#FFEB3B", "width": 2}),   # Gelb
                ("sma_50",  2, {"color": "#B39DDB", "width": 2}),   # Fliederblau
                ("sma_100", 3, {"color": "#4CAF50", "width": 2}),   # Grün
                ("sma_150", 4, {"color": "#FF9800", "width": 2}),   # Orange
                ("sma_200", 5, {"color": "#FFFFFF", "width": 2}),   # Weiß
            ],
        },
        {
            "id": "default",
            "display_name": "Standard",
            "description": "SMA 50, SMA 200, Bollinger Bands 20",
            "topbar_metrics": [],
            "members": [
                ("sma_50",  0, {"color": "#2962FF", "width": 2}),
                ("sma_200", 1, {"color": "#FF9800", "width": 2}),
                ("bb_20",   2, {"color": "#26A69A", "alpha": 30}),
            ],
        },
        {
            "id": "clean",
            "display_name": "Clean Chart",
            "description": "Candles only, no overlays",
            "topbar_metrics": [],
            "members": [],
        },
        {
            "id": "momentum",
            "display_name": "Momentum",
            "description": "EMA 8, EMA 21",
            "topbar_metrics": ["ibd_rs"],
            "members": [
                ("ema_8",  0, {"color": "#00E676", "width": 2}),
                ("ema_21", 1, {"color": "#FF5252", "width": 2}),
            ],
        },
    ]

    # First, ensure momentum preset's online-only EMAs exist
    online_features = [
        ("ema_8",  "EMA", {"window": 8, "source": "close"},  "overlay_line", "EMA 8",  {"color": "#00E676", "width": 2}),
        ("ema_21", "EMA", {"window": 21, "source": "close"}, "overlay_line", "EMA 21", {"color": "#FF5252", "width": 2}),
    ]
    for canonical_id, calc_type, calc_params, plot_type, display_name, default_style in online_features:
        sql = f"""
        INSERT INTO pca_features (canonical_id, display_name, calc_type, calc_params, plot_type, default_style, mode)
        VALUES (
            '{canonical_id}', '{display_name}', '{calc_type}',
            '{json.dumps(calc_params)}'::jsonb, '{plot_type}',
            '{json.dumps(default_style)}'::jsonb, 'online'
        ) ON CONFLICT (canonical_id) DO NOTHING;
        """
        psql(sql)
        print(f"  ✓ Online feature: {canonical_id}")

    for preset in presets:
        # Insert set
        topbar_arr = "{" + ",".join(preset["topbar_metrics"]) + "}"
        sql = f"""
        INSERT INTO pca_feature_sets (id, display_name, description, topbar_metrics)
        VALUES ('{preset["id"]}', '{preset["display_name"]}', '{preset["description"]}', '{topbar_arr}')
        ON CONFLICT (id) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            description = EXCLUDED.description,
            topbar_metrics = EXCLUDED.topbar_metrics;
        """
        psql(sql)
        
        # Delete old members and re-insert
        psql(f"DELETE FROM pca_feature_set_members WHERE set_id = '{preset['id']}';")
        
        for feature_id, sort_order, style_override in preset["members"]:
            sql = f"""
            INSERT INTO pca_feature_set_members (set_id, feature_id, sort_order, style_override)
            VALUES ('{preset["id"]}', '{feature_id}', {sort_order}, '{json.dumps(style_override)}'::jsonb);
            """
            psql(sql)
        
        print(f"  ✓ Preset: {preset['id']} ({len(preset['members'])} members)")

    return len(presets)


def verify():
    """Verify the import."""
    features_count = psql("SELECT count(*) FROM pca_features;")
    offline_count = psql("SELECT count(*) FROM pca_features WHERE mode = 'offline';")
    online_count = psql("SELECT count(*) FROM pca_features WHERE mode = 'online';")
    sets_count = psql("SELECT count(*) FROM pca_feature_sets;")
    members_count = psql("SELECT count(*) FROM pca_feature_set_members;")
    
    print(f"\n{'='*50}")
    print(f"  Features:  {features_count} total ({offline_count} offline, {online_count} online)")
    print(f"  Sets:      {sets_count}")
    print(f"  Members:   {members_count}")
    print(f"{'='*50}")

    # Show all features
    rows = psql("SELECT canonical_id, alias, calc_type, mode, depends_on FROM pca_features ORDER BY calc_type, canonical_id;")
    print(f"\n{'canonical_id':20s} {'alias':30s} {'calc_type':15s} {'mode':8s} {'depends_on'}")
    print("-" * 90)
    for line in rows.split("\n"):
        if line.strip():
            parts = line.split("|")
            if len(parts) >= 5:
                print(f"{parts[0]:20s} {parts[1]:30s} {parts[2]:15s} {parts[3]:8s} {parts[4]}")


if __name__ == "__main__":
    print("=" * 50)
    print("  Phase 1: Import features.json → Supabase")
    print("=" * 50)
    
    print("\n[1/3] Inserting features...")
    n_features = insert_features()
    
    print(f"\n[2/3] Inserting presets...")
    n_presets = insert_presets()
    
    print(f"\n[3/3] Verifying...")
    verify()
    
    print(f"\n✅ Import complete: {n_features} features, {n_presets} presets")
