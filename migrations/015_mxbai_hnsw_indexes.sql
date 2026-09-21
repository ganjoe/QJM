-- 015_mxbai_hnsw_indexes.sql
--
-- P7: HNSW-Indizes (Cosine). Erst NACH dem vollstaendigen Re-Embed und nach dem
-- Aufraeumen der alten Spalten sinnvoll.
-- 1024 Dimensionen liegen unter dem pgvector-Limit von 2000 fuer HNSW
-- (mit 4096 war ein HNSW-Index nicht moeglich).
--
-- WICHTIG: maintenance_work_mem muss in DERSELBEN Session gesetzt werden wie CREATE INDEX.
-- Aufruf:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
--   SET maintenance_work_mem = '2GB';
--   \i /dev/stdin ... (bzw. diese Datei ohne den SET-Kommentar einspielen)
--   SQL
--
-- Bewusste Entscheidung: NUR der Hauptindex auf agent_workspace.
-- Partielle Indizes (artifact_type='yt_chunk' bzw. 'x_post') wurden verworfen, weil der
-- yt-Teilindex fast so gross wie der Hauptindex waere (~1 GB) und das Speicherziel
-- (~2,3 GiB inkl. Index) sonst deutlich ueberschritten wird.

SET maintenance_work_mem = '2GB';

CREATE INDEX IF NOT EXISTS idx_aw_embedding_hnsw
  ON public.agent_workspace USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

ANALYZE public.agent_workspace;
