-- 039_float_history_numeric_widen.sql
-- cda_float_history.public_float_usd auf NUMERIC ohne Praezisionsgrenze erweitern.
--
-- Grund: SEC-Cover-Page-Werte sind gelegentlich fehlerhaft (Beispiel AEIS 10-K 2021:
-- 2.563.579.586.000.000.000 USD statt ~2,56 Mrd.). Mit NUMERIC(20,2) lief so ein Wert in
-- einen Overflow und riss den KOMPLETTEN Ticker-Upsert mit (numeric field overflow),
-- also auch die sauberen Aktienzahl-Zeilen. Die Quelle bleibt roh erhalten; wer rechnet,
-- filtert selbst (ein Public Float > 1e15 ist offensichtlich ein Filing-Fehler).
--
-- Die View cda_float_series haengt an der Spalte und wird dafuer neu aufgebaut.
-- Additiv und idempotent.

SET search_path = public, extensions;

DROP VIEW IF EXISTS public.cda_float_series;

ALTER TABLE public.cda_float_history
    ALTER COLUMN public_float_usd TYPE NUMERIC;

COMMENT ON COLUMN public.cda_float_history.public_float_usd IS
    'SEC-Public-Float (Nicht-Affiliate) in USD, roh wie im Filing gemeldet. Werte > 1e15 sind Filing-Fehler.';

CREATE OR REPLACE VIEW public.cda_float_series AS
    SELECT DISTINCT ON (ticker, as_of)
           ticker, as_of, free_float, free_float_percent, shares_outstanding,
           source, is_estimate, public_float_usd
      FROM public.cda_float_history
     WHERE free_float IS NOT NULL
     ORDER BY ticker, as_of, is_estimate ASC, source ASC;

COMMENT ON VIEW public.cda_float_series IS
    'Free-Float-Zeitreihe je Ticker: gemessene Massive-Werte haben Vorrang vor abgeleiteten Naeherungen.';

GRANT SELECT ON public.cda_float_series TO anon, service_role;

NOTIFY pgrst, 'reload schema';
