-- 038_cda_float_history.sql
-- Free Float: aktuelle Kennzahlen am Master Universe + eigene Historientabelle.
--
-- Quellen und Begriffe (bewusst getrennt, die Zahlen sind NICHT deckungsgleich):
--   * Massive GET /stocks/vX/float -> aktueller FREE FLOAT (Aktien + %) je US-Ticker.
--     In allen Massive-Aktienplaenen enthalten, aber ohne Historie: der Endpunkt
--     liefert nur einen Snapshot mit effective_date.
--   * SEC XBRL dei:EntityCommonStockSharesOutstanding -> ausstehende Aktien je Filing
--     (10-K/10-Q, Historie ab ca. 2009). Basis der Aktien-Historie.
--   * SEC XBRL dei:EntityPublicFloat -> PUBLIC FLOAT in USD (Nicht-Affiliate-Anteil
--     von der 10-K-Cover-Page). Systematisch hoeher als der Massive-Free-Float, weil
--     dort nur Insider/Affiliates ausgeschlossen werden.
--   * abgeleitet (is_estimate = true): free_float = free_float_percent (Massive,
--     aktuell) x shares_outstanding (SEC, historisch) - die Prozentquote wird dabei
--     als naeherungsweise konstant angenommen.
--
-- Additiv und idempotent.

SET search_path = public, extensions;

-- ─────────────────────────────────────────────────────────────
-- 1. Aktuelle Kennzahlen am Master Universe
-- ─────────────────────────────────────────────────────────────
ALTER TABLE public.cda_master_universe
    ADD COLUMN IF NOT EXISTS free_float           BIGINT,
    ADD COLUMN IF NOT EXISTS free_float_percent   NUMERIC(6,2),
    ADD COLUMN IF NOT EXISTS float_effective_date DATE,
    ADD COLUMN IF NOT EXISTS float_source         TEXT,
    ADD COLUMN IF NOT EXISTS float_updated_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cik                  TEXT;

COMMENT ON COLUMN public.cda_master_universe.free_float IS
    'Frei handelbare Aktien (Free Float) auf aktueller Split-Basis; Provenienz in float_source.';
COMMENT ON COLUMN public.cda_master_universe.free_float_percent IS
    'Free Float in Prozent der ausstehenden Aktien (Massive /stocks/vX/float).';
COMMENT ON COLUMN public.cda_master_universe.float_effective_date IS
    'Stichtag der Free-Float-Messung (Massive effective_date).';
COMMENT ON COLUMN public.cda_master_universe.float_source IS
    'Herkunft der aktuellen Float-Werte, z. B. massive_float.';
COMMENT ON COLUMN public.cda_master_universe.float_updated_at IS
    'Zeitpunkt des letzten Float-Syncs.';
COMMENT ON COLUMN public.cda_master_universe.cik IS
    'SEC Central Index Key (10-stellig) fuer data.sec.gov/XBRL-Abfragen.';

CREATE INDEX IF NOT EXISTS cda_master_universe_cik_idx
    ON public.cda_master_universe (cik);

-- ─────────────────────────────────────────────────────────────
-- 2. Historientabelle
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.cda_float_history (
    id                 BIGSERIAL PRIMARY KEY,
    ticker             TEXT NOT NULL REFERENCES public.cda_master_universe(ticker) ON DELETE CASCADE,
    as_of              DATE NOT NULL,
    free_float         BIGINT,
    free_float_percent NUMERIC(6,2),
    shares_outstanding BIGINT,
    public_float_usd   NUMERIC(20,2),
    source             TEXT NOT NULL,
    is_estimate        BOOLEAN NOT NULL DEFAULT FALSE,
    meta               JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT cda_float_history_key UNIQUE (ticker, as_of, source)
);

COMMENT ON TABLE public.cda_float_history IS
    'Float-/Aktien-Historie je Ticker. Ein Datensatz je (Ticker, Stichtag, Quelle): '
    'massive_float = gemessener Free Float, sec_xbrl = SEC-Filing-Daten (Aktien und Public Float USD), '
    'derived = Naeherung aus aktueller Float-Quote x historischen Aktien (is_estimate = true).';
COMMENT ON COLUMN public.cda_float_history.as_of IS
    'Stichtag der Messung: Massive effective_date bzw. SEC-Periodenende.';
COMMENT ON COLUMN public.cda_float_history.free_float IS
    'Frei handelbare Aktien (Free Float), aktuelle Split-Basis.';
COMMENT ON COLUMN public.cda_float_history.free_float_percent IS
    'Free Float in Prozent der ausstehenden Aktien.';
COMMENT ON COLUMN public.cda_float_history.shares_outstanding IS
    'Ausstehende Aktien zum Stichtag, auf die aktuelle Split-Basis normiert.';
COMMENT ON COLUMN public.cda_float_history.public_float_usd IS
    'SEC-Public-Float (Nicht-Affiliate) in USD, roh wie im Filing gemeldet.';
COMMENT ON COLUMN public.cda_float_history.source IS
    'massive_float | sec_xbrl | derived.';
COMMENT ON COLUMN public.cda_float_history.meta IS
    'Provenienz: z. B. split_factor, filing_form, abgeleitete Quote.';

CREATE INDEX IF NOT EXISTS cda_float_history_ticker_date_idx
    ON public.cda_float_history (ticker, as_of DESC);
CREATE INDEX IF NOT EXISTS cda_float_history_date_idx
    ON public.cda_float_history (as_of DESC);

CREATE OR REPLACE FUNCTION cda_float_history_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cda_float_history_updated_at ON public.cda_float_history;
CREATE TRIGGER trg_cda_float_history_updated_at
    BEFORE UPDATE ON public.cda_float_history
    FOR EACH ROW EXECUTE FUNCTION cda_float_history_set_updated_at();

-- ─────────────────────────────────────────────────────────────
-- 3. Konsum-View: eine Zeile je Ticker+Stichtag mit Float-Wert,
--    egal ob gemessen (massive_float) oder abgeleitet (derived).
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW public.cda_float_series AS
    SELECT DISTINCT ON (ticker, as_of)
           ticker, as_of, free_float, free_float_percent, shares_outstanding,
           source, is_estimate, public_float_usd
      FROM public.cda_float_history
     WHERE free_float IS NOT NULL
     ORDER BY ticker, as_of, is_estimate ASC, source ASC;

COMMENT ON VIEW public.cda_float_series IS
    'Free-Float-Zeitreihe je Ticker: gemessene Massive-Werte haben Vorrang vor abgeleiteten Naeherungen.';

-- ─────────────────────────────────────────────────────────────
-- 4. GRANTS
-- ─────────────────────────────────────────────────────────────
GRANT ALL ON TABLE public.cda_float_history TO anon, service_role;
GRANT ALL ON SEQUENCE public.cda_float_history_id_seq TO anon, service_role;
GRANT EXECUTE ON FUNCTION cda_float_history_set_updated_at TO anon, service_role;
GRANT SELECT ON public.cda_float_series TO anon, service_role;

-- PostgREST-Schema-Cache neu laden, damit Tabelle/View sofort per REST erreichbar sind.
NOTIFY pgrst, 'reload schema';
