-- 016_mxbai_drop_old_columns.sql
--
-- P8-Finalisierung: erst ausfuehren, wenn P6 (Re-Embed) UND P10 (Abnahme) erfolgreich sind.
-- Ab hier gibt es keinen einfachen Rollback mehr (dann nur noch Restore aus dem P0-Backup).
--
-- Freigabe (manuell pruefen):
--   * alle 4 Tabellen: embedding IS NOT NULL fuer alle Zeilen, die einen Vektor haben sollen
--   * keine NULL-Vektoren bei status='embedded'
--   * Golden-Set-Recall innerhalb der Toleranz
--   * HNSW-Indizes aus 015 vorhanden

BEGIN;

ALTER TABLE public.agent_workspace DROP COLUMN IF EXISTS embedding_old_4096;
ALTER TABLE public.open_brain      DROP COLUMN IF EXISTS embedding_old_4096;
ALTER TABLE public.x_users         DROP COLUMN IF EXISTS embedding_old_4096;
ALTER TABLE public.yt_channels     DROP COLUMN IF EXISTS embedding_old_4096;

COMMIT;

VACUUM (ANALYZE) public.agent_workspace;
VACUUM (ANALYZE) public.open_brain;
VACUUM (ANALYZE) public.x_users;
VACUUM (ANALYZE) public.yt_channels;

NOTIFY pgrst, 'reload schema';
