-- 001_pca_window_setups.sql
-- Window Setup Management: Speichert Fenster-Layouts (Geometrien, Typ, Flag, Monitor)
-- Composite-Unique-Key auf (setup_name, monitor_count) erlaubt Varianten pro Monitor-Anzahl.

CREATE TABLE IF NOT EXISTS pca_window_setups (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    setup_name     TEXT NOT NULL,
    monitor_count  INTEGER NOT NULL DEFAULT 1,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW(),
    windows        JSONB NOT NULL DEFAULT '[]',

    -- Ein Setup-Name kann mehrfach existieren, aber nur einmal pro Monitor-Anzahl
    UNIQUE(setup_name, monitor_count)
);

-- Index für schnelle Lookups nach setup_name
CREATE INDEX IF NOT EXISTS idx_pca_window_setups_name ON pca_window_setups(setup_name);

-- RLS: Service-Role hat vollen Zugriff (wird von agent-pca und chart_viewer genutzt)
ALTER TABLE pca_window_setups ENABLE ROW LEVEL SECURITY;

-- Allow full access for service_role (used by all backend services)
CREATE POLICY "Service role full access"
    ON pca_window_setups
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Explicitly grant permissions to API roles
GRANT ALL ON TABLE pca_window_setups TO service_role, anon;