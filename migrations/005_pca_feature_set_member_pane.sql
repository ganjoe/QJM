-- 005_pca_feature_set_member_pane.sql
-- Pane-Auswahl pro Preset-Member.
--
-- Erlaubt es, einen in der Parquet vorberechneten Indikator (z. B. ibd_rs)
-- wahlweise im Chartfenster oder in einer eigenen Subpane zu plotten:
--
--   'main'  -> Overlay im Chartfenster (Standard fuer overlay_line/overlay_band)
--   'rs'    -> eigene Subpane mit eigenem, auto-gefittetem Y-Achsenbereich
--   'none'  -> kein Overlay, nur Topbar-Metrik
--   NULL    -> PCA-Service leitet die Pane aus pca_features.plot_type ab
--              (overlay_* -> 'main', sub_* -> canonical_id, topbar_metric -> 'none')
--
-- Der Chart-Viewer-Client unterstuetzt beliebige Panes bereits dynamisch
-- (canvas._rebuild_panes baut Panes aus den vorkommenden pane-Werten).

ALTER TABLE pca_feature_set_members
    ADD COLUMN IF NOT EXISTS pane TEXT;

COMMENT ON COLUMN pca_feature_set_members.pane IS
    'Ziel-Pane des Overlays: ''main'' = Chartfenster, beliebiger Name = eigene Subpane, ''none'' = nur Topbar-Metrik, NULL = aus pca_features.plot_type ableiten';
