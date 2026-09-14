-- ==============================================================================
-- Migration 004: stock_documents (Dokumenten-Infrastruktur & Destillat-System)
-- ==============================================================================

CREATE TABLE IF NOT EXISTS public.stock_documents (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker                      TEXT NOT NULL REFERENCES public.cda_master_universe(ticker) ON DELETE CASCADE,
    
    -- Dokument-Klassifikation & Metadaten
    doc_type                    TEXT NOT NULL, -- '10-K', '10-Q', '8-K', 'earnings_release', 'presentation', 'transcript', 'other'
    title                       TEXT NOT NULL,
    fiscal_year                 INTEGER,       -- z.B. 2025
    fiscal_quarter              TEXT,          -- 'Q1', 'Q2', 'Q3', 'Q4', 'FY'
    report_date                 DATE,          -- Veröffentlichungsdatum
    
    -- Originaldatei (Relativer Pfad!)
    relative_path               TEXT NOT NULL, -- z.B. "DELL/DELL_2025_Q4_Earnings_Review.pdf"
    file_name                   TEXT NOT NULL, -- z.B. "DELL_2025_Q4_Earnings_Review.pdf"
    file_extension              TEXT NOT NULL, -- 'pdf', 'html', 'txt'
    file_size_bytes             BIGINT,
    sha256_hash                 TEXT NOT NULL, -- Deduplizierung / Integritätsprüfung
    
    -- Destillat (Kompaktes Markdown für LLM & FTS)
    distillate_relative_path    TEXT,          -- z.B. "DELL/DELL_2025_Q4_Earnings_Review.distillate.md"
    distillate_content          TEXT,          -- Vollständiges Markdown in der DB (für direkten Abruf & FTS)
    distillate_generated_at     TIMESTAMPTZ,
    
    -- Flexible Metadaten & Quellen
    source_url                  TEXT,
    sec_accession_number        TEXT,
    tags                        TEXT[],        -- z.B. ['ai_servers', 'guidance_raised']
    metadata                    JSONB DEFAULT '{}'::jsonb,
    
    -- Volltextsuche (FTS) über Titel, Ticker, Typ und den gesamten Destillat-Inhalt
    fts_vector                  TSVECTOR GENERATED ALWAYS AS (
                                    to_tsvector('english', 
                                        coalesce(title, '') || ' ' || 
                                        coalesce(ticker, '') || ' ' || 
                                        coalesce(doc_type, '') || ' ' || 
                                        coalesce(distillate_content, '')
                                    )
                                ) STORED,
    
    created_at                  TIMESTAMPTZ DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ DEFAULT NOW()
);

-- Indizes für maximale Performance
CREATE INDEX IF NOT EXISTS idx_stock_docs_ticker ON public.stock_documents(ticker);
CREATE INDEX IF NOT EXISTS idx_stock_docs_doc_type ON public.stock_documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_stock_docs_period ON public.stock_documents(ticker, fiscal_year, fiscal_quarter);
CREATE INDEX IF NOT EXISTS idx_stock_docs_sha256 ON public.stock_documents(sha256_hash);
CREATE INDEX IF NOT EXISTS idx_stock_docs_fts ON public.stock_documents USING GIN(fts_vector);

-- Trigger für automatisches updated_at
CREATE OR REPLACE FUNCTION public.set_stock_documents_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_stock_documents_updated_at ON public.stock_documents;
CREATE TRIGGER trg_stock_documents_updated_at
    BEFORE UPDATE ON public.stock_documents
    FOR EACH ROW
    EXECUTE FUNCTION public.set_stock_documents_updated_at();

-- RLS und Berechtigungen für Supabase (service_role, anon)
ALTER TABLE public.stock_documents ENABLE ROW LEVEL SECURITY;

DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'stock_documents' AND policyname = 'Service role full access on stock_documents') THEN
        CREATE POLICY "Service role full access on stock_documents" ON public.stock_documents FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'stock_documents' AND policyname = 'Read access on stock_documents') THEN
        CREATE POLICY "Read access on stock_documents" ON public.stock_documents FOR SELECT TO anon USING (true);
    END IF;
END $$;

GRANT ALL ON TABLE public.stock_documents TO service_role, anon;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role, anon;

-- PostgREST Schema-Cache neu laden
NOTIFY pgrst, 'reload schema';
