#!/usr/bin/env python3
"""Create pca_features schema in Supabase PostgreSQL."""
import subprocess, sys

SQL = """
CREATE TABLE IF NOT EXISTS pca_features (
    canonical_id   TEXT PRIMARY KEY,
    alias          TEXT UNIQUE,
    display_name   TEXT NOT NULL,
    calc_type      TEXT NOT NULL,
    calc_params    JSONB NOT NULL DEFAULT '{}',
    plot_type      TEXT NOT NULL DEFAULT 'overlay_line',
    default_style  JSONB NOT NULL DEFAULT '{}',
    mode           TEXT NOT NULL DEFAULT 'online' CHECK (mode IN ('offline', 'online')),
    depends_on     TEXT REFERENCES pca_features(canonical_id),
    UNIQUE (calc_type, calc_params)
);
CREATE TABLE IF NOT EXISTS pca_feature_sets (
    id             TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    description    TEXT,
    topbar_metrics TEXT[] DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS pca_feature_set_members (
    set_id         TEXT REFERENCES pca_feature_sets(id) ON DELETE CASCADE,
    feature_id     TEXT REFERENCES pca_features(canonical_id) ON DELETE CASCADE,
    sort_order     INT DEFAULT 0,
    style_override JSONB DEFAULT '{}',
    PRIMARY KEY (set_id, feature_id)
);
CREATE INDEX IF NOT EXISTS idx_pca_features_mode ON pca_features(mode);
CREATE INDEX IF NOT EXISTS idx_pca_features_calc_type ON pca_features(calc_type);
"""

r = subprocess.run(
    ["docker", "exec", "-i", "openbrain-db", "psql", "-U", "postgres"],
    input=SQL, capture_output=True, text=True
)
print(r.stdout)
if r.stderr:
    print(r.stderr, file=sys.stderr)
sys.exit(r.returncode)
