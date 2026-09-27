-- 036_earnings_calendar.sql
-- Earnings-Kalender: Historie vergangener Termine als eigene Tabelle + vorwärtsgerichtetes
-- Feld `next_earnings` am Master Universe. Additiv und idempotent.
--
-- Hintergrund: cda_master_universe.earnings war bisher ambivalent (letzter ODER nächster
-- Termin, je nach Datenquelle). Verbindliche Semantik ab dieser Migration:
--   * cda_master_universe.earnings      = LETZTER gemeldeter Termin (Rückblick)
--   * cda_master_universe.next_earnings = NÄCHSTER geplanter Termin (vorwärtsgerichtet)
--   * cda_earnings_history              = alle VERGANGENEN Termine je Ticker (1 Zeile je Tag)

SET search_path = public, extensions;

-- ─────────────────────────────────────────────────────────────
-- 1. Vorwärtsgerichtetes Feld am Master Universe
-- ─────────────────────────────────────────────────────────────
ALTER TABLE public.cda_master_universe
    ADD COLUMN IF NOT EXISTS next_earnings        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS next_earnings_source TEXT;

COMMENT ON COLUMN public.cda_master_universe.next_earnings IS
    'Nächster geplanter Earnings-Termin (UTC, vorwärtsgerichtet). Herkunft in next_earnings_source.';
COMMENT ON COLUMN public.cda_master_universe.next_earnings_source IS
    'Herkunft von next_earnings: manual | nasdaq_zacks_estimate | yfinance | migrated_from_earnings.';
COMMENT ON COLUMN public.cda_master_universe.earnings IS
    'Letzter gemeldeter Earnings-Termin (UTC, Rückblick). Historie siehe cda_earnings_history.';

-- Einmalige Übernahme: ein in der ZUKUNFT liegender earnings-Wert IST ein next_earnings-Wert.
UPDATE public.cda_master_universe
   SET next_earnings        = earnings,
       next_earnings_source = COALESCE(next_earnings_source, 'migrated_from_earnings')
 WHERE next_earnings IS NULL
   AND earnings > NOW();

-- ─────────────────────────────────────────────────────────────
-- 2. Historie vergangener Earnings-Termine
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.cda_earnings_history (
    id                 BIGSERIAL PRIMARY KEY,
    ticker             TEXT NOT NULL REFERENCES public.cda_master_universe(ticker) ON DELETE CASCADE,
    report_date        TIMESTAMPTZ NOT NULL,
    fiscal_quarter_end DATE,
    fiscal_year        INTEGER,
    fiscal_quarter     SMALLINT,
    eps_actual         NUMERIC,
    eps_estimate       NUMERIC,
    eps_surprise_pct   NUMERIC,
    revenue_actual     NUMERIC,
    revenue_estimate   NUMERIC,
    source             TEXT NOT NULL DEFAULT 'nasdaq',
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    last_updated       TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT cda_earnings_history_ticker_report_date_key UNIQUE (ticker, report_date),
    CONSTRAINT cda_earnings_history_fiscal_quarter_check
        CHECK (fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4)
);

CREATE INDEX IF NOT EXISTS cda_earnings_history_ticker_date_idx
    ON public.cda_earnings_history (ticker, report_date DESC);
CREATE INDEX IF NOT EXISTS cda_earnings_history_report_date_idx
    ON public.cda_earnings_history (report_date DESC);

COMMENT ON TABLE public.cda_earnings_history IS
    'Vergangene Earnings-Termine je Ticker (1 Zeile je Ticker+Termin). Zukunft steht in cda_master_universe.next_earnings.';

CREATE OR REPLACE FUNCTION cda_earnings_history_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.last_updated = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cda_earnings_history_updated_at ON public.cda_earnings_history;
CREATE TRIGGER trg_cda_earnings_history_updated_at
    BEFORE UPDATE ON public.cda_earnings_history
    FOR EACH ROW EXECUTE FUNCTION cda_earnings_history_set_updated_at();

-- ─────────────────────────────────────────────────────────────
-- 3. GRANTS
-- ─────────────────────────────────────────────────────────────
GRANT ALL ON TABLE public.cda_earnings_history TO anon, service_role;
GRANT ALL ON SEQUENCE public.cda_earnings_history_id_seq TO anon, service_role;
GRANT EXECUTE ON FUNCTION cda_earnings_history_set_updated_at TO anon, service_role;

-- PostgREST-Schema-Cache neu laden, damit die neue Tabelle sofort per REST erreichbar ist.
NOTIFY pgrst, 'reload schema';
