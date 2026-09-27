-- 037_pca_feature_set_pane_scales.sql
-- Y-Achsen-Skalierung (linear/log) je Pane eines Chart-Presets.
--
-- Der Chart-Viewer zeichnet pro Pane (main, volume, rs, adr, ...) einen eigenen
-- Y-Achsen-Bereich. Mit der neuen Log/Lin-Taste im Pane schaltet der Nutzer die
-- Skalierung um; die Einstellung wird im aktiven Preset gespeichert, damit sie
-- beim naechsten Symbolwechsel / Preset-Apply wieder greift.
--
--   {"main": "log", "volume": "linear", "rs": "linear"}
--
-- Semantik:
--   * fehlender Pane  -> linear (Viewer-Default)
--   * "linear" | "log"
--   * Log wird vom Viewer auf linear zurueckgezogen, wenn der sichtbare
--     Wertebereich <= 0 enthaelt (log ist dafuer undefiniert).
--
-- Additiv und idempotent.

SET search_path = public, extensions;

ALTER TABLE public.pca_feature_sets
    ADD COLUMN IF NOT EXISTS pane_scales JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN public.pca_feature_sets.pane_scales IS
    'Y-Achsen-Skalierung je Pane: {"main": "log", "volume": "linear"}. Werte: linear | log, fehlende Panes = linear.';

GRANT ALL ON TABLE public.pca_feature_sets TO anon, service_role;

-- PostgREST-Schema-Cache neu laden, damit pane_scales sofort per REST nutzbar ist.
NOTIFY pgrst, 'reload schema';
