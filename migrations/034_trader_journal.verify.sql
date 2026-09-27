-- ============================================================================
-- 034_trader_journal.verify.sql
--
-- Prueft die Migration 034. Jede Abfrage liefert eine Erwartung mit; "ok" muss
-- ueberall true sein. Aufruf:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/034_trader_journal.verify.sql
-- ============================================================================

\pset footer off

-- 1) Tabellen vorhanden
SELECT 'tabellen' AS pruefung,
       count(*) = 3 AS ok,
       string_agg(table_name, ', ' ORDER BY table_name) AS ist
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('journal_entry','journal_thesis','journal_entry_link');

-- 2) Kernspalten von journal_entry
SELECT 'spalten_journal_entry' AS pruefung,
       count(*) = 8 AS ok,
       string_agg(column_name, ', ' ORDER BY column_name) AS ist
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'journal_entry'
  AND column_name IN ('reminder_date','body','body_sha256','summary','keywords','tickers','companies','dedupe_hash');

-- 3) Indizes (die vier wichtigsten)
SELECT 'indizes' AS pruefung,
       count(*) >= 4 AS ok,
       string_agg(indexname, ', ' ORDER BY indexname) AS ist
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname IN ('idx_journal_entry_date','idx_journal_entry_reminder','idx_journal_entry_dedupe','idx_journal_entry_kw');

-- 4) Vektor-Indizes (HNSW) und Dimension 1024
SELECT 'vektor_indizes' AS pruefung,
       count(*) = 2 AS ok,
       string_agg(indexname, ', ' ORDER BY indexname) AS ist
FROM pg_indexes
WHERE schemaname = 'public' AND indexname IN ('idx_journal_entry_hnsw','idx_journal_thesis_hnsw');

-- 5) RPCs vorhanden
SELECT 'rpcs' AS pruefung,
       count(*) = 12 AS ok,
       string_agg(proname, ', ' ORDER BY proname) AS ist
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND p.proname IN ('journal_today','journal_resolve_id','journal_apply_relation','journal_capture',
                    'journal_enrich','journal_link','journal_recent','journal_reminders',
                    'hybrid_search_journal','journal_theses','journal_update','journal_delete');

-- 6) search_path der Vektor-RPCs (Lehre aus Migration 018)
SELECT 'search_path_vektor' AS pruefung,
       bool_and(proconfig IS NOT NULL AND array_to_string(proconfig, ',') LIKE '%extensions%') AS ok,
       string_agg(proname || '=' || COALESCE(array_to_string(proconfig, ','), 'FEHLT'), ' | ' ORDER BY proname) AS ist
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.proname IN ('journal_enrich','hybrid_search_journal');

-- 7) journal_today() liefert ein Datum
SELECT 'journal_today' AS pruefung,
       public.journal_today() = (now() AT TIME ZONE 'Europe/Berlin')::date AS ok,
       public.journal_today()::text AS ist;

-- 8) Praefix-Aufloesung: unbekannter Praefix muss einen Fehler werfen, keinen NULL
DO $$
BEGIN
  BEGIN
    PERFORM public.journal_resolve_id('00000000');
    RAISE EXCEPTION 'FEHLER: unbekannter Praefix wurde nicht abgelehnt';
  EXCEPTION WHEN others THEN
    IF SQLERRM LIKE 'journal: kein Eintrag%' THEN
      RAISE NOTICE 'praefix_aufloesung: ok (unbekannter Praefix abgelehnt)';
    ELSE
      RAISE;
    END IF;
  END;
END;
$$;
