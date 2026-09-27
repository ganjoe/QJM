-- 040_pane_chart_presets.sql
-- Zwei-Ebenen-Presets fuer den Chart-Viewer.
--
--   pca_pane_presets        = Inhalt EINES Panes (Serien, Regeln, Zonen, Refs)
--   pca_chart_presets       = Fenster-Inhalt: geordnete Panes + Topbar
--   pca_chart_preset_panes  = Slot -> Pane-Preset (Reihenfolge, Gewicht, Skala)
--
-- Vertrag: docs/architecture/chart-presets.md
-- Bestehende Daten (pca_feature_sets + members) werden einmalig migriert:
-- je Feature-Set ein Chart-Preset (gleiche id), je (Set, pane) ein Pane-Preset.
-- Identische Pane-Inhalte ueber Sets hinweg werden geteilt (Content-Hash).
-- Additiv und idempotent.

SET search_path = public, extensions;

-- ---------------------------------------------------------------------------
-- 1. Tabellen
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.pca_pane_presets (
    id             TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    description    TEXT DEFAULT '',
    role           TEXT NOT NULL DEFAULT 'any',      -- price | value | volume | any
    kind           TEXT NOT NULL DEFAULT 'indicator',-- indicator | custom | builtin
    renderer       TEXT,
    params         JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_scale  TEXT NOT NULL DEFAULT 'linear',
    refs           JSONB NOT NULL DEFAULT '[]'::jsonb,
    derives        JSONB NOT NULL DEFAULT '[]'::jsonb,
    zones          JSONB NOT NULL DEFAULT '[]'::jsonb,
    archived       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.pca_pane_preset_members (
    pane_preset_id TEXT NOT NULL REFERENCES public.pca_pane_presets(id) ON DELETE CASCADE,
    feature_id     TEXT NOT NULL REFERENCES public.pca_features(canonical_id) ON DELETE CASCADE,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    style_override JSONB NOT NULL DEFAULT '{}'::jsonb,
    rules          JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (pane_preset_id, feature_id)
);

CREATE TABLE IF NOT EXISTS public.pca_chart_presets (
    id             TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    description    TEXT DEFAULT '',
    topbar_metrics TEXT[] NOT NULL DEFAULT '{}'::text[],
    x_axis_pane    TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.pca_chart_preset_panes (
    chart_preset_id TEXT NOT NULL REFERENCES public.pca_chart_presets(id) ON DELETE CASCADE,
    pane_id         TEXT NOT NULL,
    pane_preset_id  TEXT NOT NULL REFERENCES public.pca_pane_presets(id) ON DELETE RESTRICT,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    weight          INTEGER NOT NULL DEFAULT 2,
    scale           TEXT NOT NULL DEFAULT 'linear',
    overrides       JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (chart_preset_id, pane_id)
);

CREATE INDEX IF NOT EXISTS idx_pca_pane_preset_members_preset
    ON public.pca_pane_preset_members(pane_preset_id);
CREATE INDEX IF NOT EXISTS idx_pca_chart_preset_panes_preset
    ON public.pca_chart_preset_panes(pane_preset_id);

ALTER TABLE public.pca_pane_presets        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pca_pane_preset_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pca_chart_presets       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pca_chart_preset_panes  ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['pca_pane_presets','pca_pane_preset_members','pca_chart_presets','pca_chart_preset_panes']
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS "Service role full access" ON public.%I', t);
        EXECUTE format('CREATE POLICY "Service role full access" ON public.%I FOR ALL TO service_role USING (true) WITH CHECK (true)', t);
        EXECUTE format('GRANT ALL ON TABLE public.%I TO anon, service_role', t);
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Eingebautes Volumen-Pane (virtuell: der Orchestrator fuellt es)
-- ---------------------------------------------------------------------------

INSERT INTO public.pca_pane_presets (id, display_name, description, role, kind)
VALUES ('builtin:volume', 'Volumen', 'Volumen-Histogramm des Fensters (eingebaut).', 'volume', 'builtin')
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.pca_pane_presets (id, display_name, description, role, kind)
VALUES ('builtin:candles', 'Kerzen', 'Nur Kerzen, keine Indikatoren (eingebaut).', 'price', 'builtin')
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. Backfill aus pca_feature_sets / pca_feature_set_members
-- ---------------------------------------------------------------------------

-- 3a. Feature-Mitglieder mit aufgeloester Pane-Id (NULL = aus plot_type ableiten).
DROP TABLE IF EXISTS _cv_mig_members;
CREATE TEMP TABLE _cv_mig_members AS
SELECT s.id AS set_id,
       COALESCE(
           NULLIF(LOWER(BTRIM(m.pane)), ''),
           CASE WHEN f.plot_type IN ('overlay_line', 'overlay_band') THEN 'main'
                WHEN f.plot_type IN ('sub_line', 'subchart')     THEN LOWER(f.canonical_id)
                ELSE 'main' END
       ) AS pane_id,
       m.feature_id,
       COALESCE(m.sort_order, 0) AS sort_order,
       COALESCE(m.style_override, '{}'::jsonb) AS style_override
FROM public.pca_feature_sets s
JOIN public.pca_feature_set_members m ON m.set_id = s.id
LEFT JOIN public.pca_features f ON f.canonical_id = m.feature_id
WHERE COALESCE(NULLIF(LOWER(BTRIM(m.pane)), ''), 'x') <> 'none';

-- 3b. Inhalt je (Set, Pane) hashen, identische Inhalte teilen ein Pane-Preset.
DROP TABLE IF EXISTS _cv_mig_panes;
CREATE TEMP TABLE _cv_mig_panes AS
WITH grp AS (
    SELECT set_id, pane_id,
           md5(string_agg(feature_id || ':' || sort_order || ':' || style_override::text,
                          '|' ORDER BY feature_id)) AS content_hash
    FROM _cv_mig_members
    GROUP BY set_id, pane_id
),
owner AS (
    SELECT DISTINCT ON (content_hash) content_hash, set_id, pane_id
    FROM grp ORDER BY content_hash, set_id, pane_id
)
SELECT g.set_id, g.pane_id, o.set_id || '__' || o.pane_id AS pane_preset_id
FROM grp g JOIN owner o USING (content_hash);

INSERT INTO public.pca_pane_presets (id, display_name, description, role, kind)
SELECT p.pane_preset_id,
       (CASE WHEN p.pane_id = 'main' THEN 'Chart' ELSE initcap(p.pane_id) END)
         || ' · ' || COALESCE(lab.names, 'leer') || COALESCE(lab.more, ''),
       'Zusammengefuehrt aus bestehenden Presets, Pane ' || p.pane_id || '.',
       CASE WHEN p.pane_id = 'main' THEN 'price' ELSE 'value' END,
       'indicator'
FROM (SELECT DISTINCT pane_preset_id, pane_id, set_id FROM _cv_mig_panes) p
LEFT JOIN LATERAL (
    SELECT left(array_to_string((array_agg(COALESCE(f.display_name, m.feature_id) ORDER BY m.sort_order, m.feature_id))[1:3], ' · '), 60) AS names,
           CASE WHEN count(*) > 3 THEN ' +' || (count(*) - 3) || ' weitere' ELSE '' END AS more
    FROM _cv_mig_members m
    LEFT JOIN public.pca_features f ON f.canonical_id = m.feature_id
    WHERE m.set_id = p.set_id AND m.pane_id = p.pane_id
) lab ON TRUE
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.pca_pane_preset_members (pane_preset_id, feature_id, sort_order, style_override, rules)
SELECT DISTINCT p.pane_preset_id, m.feature_id, m.sort_order, m.style_override, '{}'::jsonb
FROM _cv_mig_panes p
JOIN _cv_mig_members m ON m.set_id = p.set_id AND m.pane_id = p.pane_id
ON CONFLICT (pane_preset_id, feature_id) DO NOTHING;

-- 3c. Chart-Presets (gleiche ids wie die Feature-Sets).
INSERT INTO public.pca_chart_presets (id, display_name, description, topbar_metrics)
SELECT s.id, s.display_name, COALESCE(s.description, ''), COALESCE(s.topbar_metrics, '{}'::text[])
FROM public.pca_feature_sets s
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.pca_chart_preset_panes (chart_preset_id, pane_id, pane_preset_id, sort_order, weight, scale)
SELECT p.set_id, p.pane_id, p.pane_preset_id,
       (row_number() OVER (PARTITION BY p.set_id ORDER BY (p.pane_id = 'main') DESC, p.pane_id) - 1)::int,
       CASE WHEN p.pane_id = 'main' THEN 7 ELSE 2 END,
       COALESCE(s.pane_scales ->> p.pane_id, 'linear')
FROM _cv_mig_panes p
JOIN public.pca_feature_sets s ON s.id = p.set_id
ON CONFLICT (chart_preset_id, pane_id) DO NOTHING;

-- 3c2. Jedes Chart braucht ein Preispane (Kerzen). Sets, deren Mitglieder alle
--      in Sub-Panes liegen (z. B. 'clean'), bekommen ein leeres Kerzen-Pane.
INSERT INTO public.pca_chart_preset_panes (chart_preset_id, pane_id, pane_preset_id, sort_order, weight, scale)
SELECT c.id, 'main', 'builtin:candles', 0, 7, 'linear'
FROM public.pca_chart_presets c
WHERE EXISTS (SELECT 1 FROM public.pca_feature_sets s WHERE s.id = c.id)
  AND NOT EXISTS (SELECT 1 FROM public.pca_chart_preset_panes p
                  WHERE p.chart_preset_id = c.id AND p.pane_id = 'main')
ON CONFLICT (chart_preset_id, pane_id) DO NOTHING;

-- 3d. Volumen-Pane ergaenzen (heute zeichnet der Orchestrator es ohnehin in jedes
--     Fenster; ab jetzt steht es explizit in der Definition und ist entfernbar).
INSERT INTO public.pca_chart_preset_panes (chart_preset_id, pane_id, pane_preset_id, sort_order, weight, scale)
SELECT c.id, 'volume', 'builtin:volume',
       COALESCE((SELECT MAX(sort_order) + 1 FROM public.pca_chart_preset_panes WHERE chart_preset_id = c.id), 1),
       2,
       COALESCE(c_old.pane_scales ->> 'volume', 'linear')
FROM public.pca_chart_presets c
LEFT JOIN public.pca_feature_sets c_old ON c_old.id = c.id
WHERE EXISTS (SELECT 1 FROM public.pca_feature_sets s WHERE s.id = c.id)
ON CONFLICT (chart_preset_id, pane_id) DO NOTHING;

DROP TABLE IF EXISTS _cv_mig_members;
DROP TABLE IF EXISTS _cv_mig_panes;

-- ---------------------------------------------------------------------------
-- 4. Beispiel: RS-Monitor (Pane-Preset) + Chart, das ihn benutzt
-- ---------------------------------------------------------------------------

INSERT INTO public.pca_pane_presets
    (id, display_name, description, role, kind, default_scale, refs, derives, zones)
VALUES (
    'rs_monitor',
    'RS-Monitor',
    'IBD-RS und RS-ADR-neutral als Linien mit Schwellenfarben (unter 30 rot, ueber 80 gruen). '
    'Jede 30->80-Phase innerhalb von 20 Bars wird als halbtransparenter Block markiert.',
    'value', 'indicator', 'linear',
    '[{"value": 30, "label": "30", "style": {"color": "#546E7A", "dash": true}},
      {"value": 80, "label": "80", "style": {"color": "#546E7A", "dash": true}}]'::jsonb,
    '[{"id": "revival", "fn": "cross_window", "series": "ibd_rs", "low": 30, "high": 80, "within": 20}]'::jsonb,
    '[{"from": "revival", "style": {"color": "#26A69A", "alpha": 40}, "label": "30->80"}]'::jsonb
)
ON CONFLICT (id) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    description  = EXCLUDED.description,
    role         = EXCLUDED.role,
    kind         = EXCLUDED.kind,
    default_scale= EXCLUDED.default_scale,
    refs         = EXCLUDED.refs,
    derives      = EXCLUDED.derives,
    zones        = EXCLUDED.zones,
    updated_at   = NOW();

INSERT INTO public.pca_pane_preset_members (pane_preset_id, feature_id, sort_order, style_override, rules)
VALUES
    ('rs_monitor', 'ibd_rs', 0,
     '{"color": "#B0BEC5", "width": 2}'::jsonb,
     '{"thresholds": [{"below": 30, "color": "#EF5350"}, {"above": 80, "color": "#26A69A"}]}'::jsonb),
    ('rs_monitor', 'rs_adr_neutral', 1,
     '{"color": "#42A5F5", "width": 1}'::jsonb,
     '{"thresholds": [{"below": 30, "color": "#EF5350"}, {"above": 80, "color": "#26A69A"}]}'::jsonb)
ON CONFLICT (pane_preset_id, feature_id) DO UPDATE SET
    sort_order = EXCLUDED.sort_order,
    style_override = EXCLUDED.style_override,
    rules = EXCLUDED.rules;

-- Chart "RS-Monitor": Preispane aus qmaggi (SMA 10-200) + RS-Pane + Volumen.
INSERT INTO public.pca_chart_presets (id, display_name, description, topbar_metrics)
VALUES ('rs_monitor_chart', 'RS-Monitor', 'SMA 10-200 im Preispane, darunter der RS-Monitor (RS + RS-ADR) und Volumen.',
        ARRAY['ibd_rs', 'adr_20'])
ON CONFLICT (id) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    description = EXCLUDED.description,
    topbar_metrics = EXCLUDED.topbar_metrics,
    updated_at = NOW();

INSERT INTO public.pca_chart_preset_panes (chart_preset_id, pane_id, pane_preset_id, sort_order, weight, scale)
SELECT 'rs_monitor_chart', 'main', p.pane_preset_id, 0, 7, 'log'
FROM public.pca_chart_preset_panes p
WHERE p.chart_preset_id = 'qmaggi' AND p.pane_id = 'main'
ON CONFLICT (chart_preset_id, pane_id) DO NOTHING;

INSERT INTO public.pca_chart_preset_panes (chart_preset_id, pane_id, pane_preset_id, sort_order, weight, scale)
VALUES ('rs_monitor_chart', 'rs', 'rs_monitor', 1, 3, 'linear')
ON CONFLICT (chart_preset_id, pane_id) DO UPDATE SET
    pane_preset_id = EXCLUDED.pane_preset_id, sort_order = EXCLUDED.sort_order,
    weight = EXCLUDED.weight, scale = EXCLUDED.scale;

INSERT INTO public.pca_chart_preset_panes (chart_preset_id, pane_id, pane_preset_id, sort_order, weight, scale)
VALUES ('rs_monitor_chart', 'volume', 'builtin:volume', 2, 2, 'linear')
ON CONFLICT (chart_preset_id, pane_id) DO NOTHING;

NOTIFY pgrst, 'reload schema';
