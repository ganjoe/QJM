-- 025: Refresh-Auftraege muessen einen expliziten Zustand tragen.
--
-- URSACHE (ausfuehrlich: docs/architecture/broker-truth-snapshot.md)
-- pta_execution_log.notes hat keinen DEFAULT. list_active_positions legte
-- REFRESH_REQUESTED-Zeilen ohne notes an, also mit NULL. Der Daemon selektierte
-- diese Zeilen mit `notes != 'PROCESSING'`. In SQL ist NULL <> 'PROCESSING'
-- gleich NULL und damit nicht wahr -> jede Anfrage fiel stillschweigend durch
-- den Filter. Der Portfolio-Snapshot wurde daraufhin nie aktualisiert und die
-- Agenten lieferten wochenlang eingefrorene Depotwerte aus, ohne jede Warnung.
--
-- Diese Migration macht den Fehler strukturell unmoeglich: eine Refresh-Anfrage
-- ohne notes kann nicht mehr geschrieben werden. Ein kuenftiger Aufrufer
-- scheitert laut beim INSERT, statt still wirkungslos zu bleiben.
--
-- !!! REIHENFOLGE BEACHTEN !!!
-- Diesen Schritt ERST ausfuehren, nachdem der MCP-Container den neuen Code
-- geladen hat:
--     docker restart llm-gw-mcp-pta
-- Die alte Tool-Version schreibt REFRESH_REQUESTED ohne notes. Laeuft sie nach
-- dieser Migration weiter, schlaegt ihr INSERT fehl und list_active_positions
-- bricht mit einem Fehler ab (laut statt still — aber unnoetig).

-- 1. Verwaiste Anfragen ohne notes entfernen. Solche Zeilen sind Ueberreste
--    fehlgeschlagener Laeufe: das Tool loescht seine Auftragszeile nach dem
--    Poll selbst, und der Daemon hat sie nie gesehen.
DELETE FROM public.pta_execution_log
WHERE event_type = 'REFRESH_REQUESTED'
  AND notes IS NULL;

-- 2. Invariante erzwingen (idempotent).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'pta_refresh_requires_notes'
  ) THEN
    ALTER TABLE public.pta_execution_log
      ADD CONSTRAINT pta_refresh_requires_notes
      CHECK (event_type <> 'REFRESH_REQUESTED' OR notes IS NOT NULL);
  END IF;
END $$;

COMMENT ON CONSTRAINT pta_refresh_requires_notes ON public.pta_execution_log IS
  'Ein Refresh-Auftrag muss einen expliziten notes-Zustand tragen (PENDING/PROCESSING/COMPLETED). Verhindert Auftraege, die kein Konsument sieht — genau daran ist die Portfolio-Bewertung zuvor gescheitert.';
