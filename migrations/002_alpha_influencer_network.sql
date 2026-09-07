-- 002_alpha_influencer_network.sql
-- Alpha-Influencer Netzwerk Tabellen für CCO Agent
-- Isoliertes Schema mit Präfix alpha_ zur Vermeidung von Konflikten

-- 1. alpha_scanned_accounts: Verwaltet das globale Register aller per API abgefragten Seeds
CREATE TABLE IF NOT EXISTS alpha_scanned_accounts (
    x_id                TEXT PRIMARY KEY,
    username            TEXT NOT NULL,
    name                TEXT,
    level               INTEGER NOT NULL DEFAULT 1,
    scanned_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    followings_fetched  INTEGER NOT NULL DEFAULT 0,
    api_provider        TEXT NOT NULL DEFAULT 'twitterapi',
    cost_usd            NUMERIC(10, 5) DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_alpha_scanned_username ON alpha_scanned_accounts(username);
CREATE INDEX IF NOT EXISTS idx_alpha_scanned_level ON alpha_scanned_accounts(level);

-- 2. alpha_candidates: Speichert alle entdeckten Profile und deren aktuellen Resonanz-Score
CREATE TABLE IF NOT EXISTS alpha_candidates (
    x_id                TEXT PRIMARY KEY,
    username            TEXT NOT NULL,
    name                TEXT,
    score               INTEGER NOT NULL DEFAULT 1,
    first_level         INTEGER NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alpha_candidates_score ON alpha_candidates(score DESC);
CREATE INDEX IF NOT EXISTS idx_alpha_candidates_username ON alpha_candidates(username);

-- 3. alpha_relations: Graph-Kanten (Wer folgt wem?)
CREATE TABLE IF NOT EXISTS alpha_relations (
    seed_id             TEXT NOT NULL,
    seed_username       TEXT NOT NULL,
    candidate_id        TEXT NOT NULL,
    candidate_username  TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (seed_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_alpha_relations_candidate ON alpha_relations(candidate_id);
CREATE INDEX IF NOT EXISTS idx_alpha_relations_seed ON alpha_relations(seed_id);

-- 4. Row Level Security (RLS) aktivieren
ALTER TABLE alpha_scanned_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE alpha_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE alpha_relations ENABLE ROW LEVEL SECURITY;

-- 5. Policies für Service-Role (Backend-Zugriff)
DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'alpha_scanned_accounts' AND policyname = 'Service role full access on alpha_scanned') THEN
        CREATE POLICY "Service role full access on alpha_scanned" ON alpha_scanned_accounts FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'alpha_candidates' AND policyname = 'Service role full access on alpha_candidates') THEN
        CREATE POLICY "Service role full access on alpha_candidates" ON alpha_candidates FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'alpha_relations' AND policyname = 'Service role full access on alpha_relations') THEN
        CREATE POLICY "Service role full access on alpha_relations" ON alpha_relations FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
    -- Anon Lesezugriff für Dashboard
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'alpha_candidates' AND policyname = 'Anon read access on alpha_candidates') THEN
        CREATE POLICY "Anon read access on alpha_candidates" ON alpha_candidates FOR SELECT TO anon USING (true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'alpha_relations' AND policyname = 'Anon read access on alpha_relations') THEN
        CREATE POLICY "Anon read access on alpha_relations" ON alpha_relations FOR SELECT TO anon USING (true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'alpha_scanned_accounts' AND policyname = 'Anon read access on alpha_scanned') THEN
        CREATE POLICY "Anon read access on alpha_scanned" ON alpha_scanned_accounts FOR SELECT TO anon USING (true);
    END IF;
END $$;

-- 6. Berechtigungen vergeben
GRANT ALL ON TABLE alpha_scanned_accounts TO service_role, anon;
GRANT ALL ON TABLE alpha_candidates TO service_role, anon;
GRANT ALL ON TABLE alpha_relations TO service_role, anon;
