-- 014_mxbai_embedding_columns.sql
--
-- P5 der MXBAI-Migration (2026-09-18): Umstellung von qwen3-embedding:8b (4096 Dim)
-- auf mxbai-embed-large (1024 Dim).
--
-- STRATEGIE (bewusste Abweichung vom urspruenglichen Dual-Column-Plan):
-- Die alten Vektoren werden NICHT gedroppt, sondern als <col>_old_4096 umbenannt und die
-- neue Spalte heisst wieder exakt "embedding". Vorteile:
--   * KEIN Code-Schalter noetig (kein EMBED_COLUMN-Env, keine Gefahr, eine Schreibstelle
--     zu vergessen) — alle Tools/Worker schreiben unveraendert in "embedding".
--   * Die RPCs (search_yt_chunks, hybrid_search_workspace, ...) bleiben unveraendert und
--     finden automatisch die neue Spalte (plpgsql loest Spaltennamen zur Laufzeit auf).
--     Der alte 4096er-Vektor kann nie mit einem 1024er-Queryvektor kollidieren
--     (das waere ein harter "different vector dimensions"-Fehler gewesen).
--   * Waehrend des Re-Embeds liefern die Hybrid-RPCs weiter Keyword-Treffer
--     (Vektor-CTE ist leer, Keyword-CTE laeuft) statt Fehler.
--   * Rollback vor dem Cutover: neue Spalte droppen, alte zurueckbenennen.
--
-- ACHTUNG: Das yt_chunks-View MUSS neu erstellt werden. CREATE OR REPLACE VIEW scheitert
-- am Typwechsel (extensions.vector(4096) -> extensions.vector(1024)) und ein Spalten-
-- RENAME haengt das View an die ALTE Spalte (verifiziert am 2026-09-18).

BEGIN;

-- 1) Alte 4096er Spalten umbenennen (Daten bleiben als Rollback erhalten)
ALTER TABLE public.agent_workspace RENAME COLUMN embedding TO embedding_old_4096;
ALTER TABLE public.open_brain      RENAME COLUMN embedding TO embedding_old_4096;
ALTER TABLE public.x_users         RENAME COLUMN embedding TO embedding_old_4096;
ALTER TABLE public.yt_channels     RENAME COLUMN embedding TO embedding_old_4096;

-- 2) Neue 1024er Spalten (instant, kein Table-Rewrite)
ALTER TABLE public.agent_workspace ADD COLUMN embedding extensions.vector(1024);
ALTER TABLE public.open_brain      ADD COLUMN embedding extensions.vector(1024);
ALTER TABLE public.x_users         ADD COLUMN embedding extensions.vector(1024);
ALTER TABLE public.yt_channels     ADD COLUMN embedding extensions.vector(1024);

-- 3) yt_chunks-View auf die NEUE Spalte zeigen lassen
DROP VIEW IF EXISTS public.yt_chunks;
CREATE VIEW public.yt_chunks AS
 SELECT aw.id,
    aw.video_id,
    aw.chunk_index,
    aw.t_start_sec,
    aw.t_end_sec,
    aw.content,
    aw.embedding,
    aw.metadata,
    aw.created_at AS chunk_created_at,
    aw.embedded_at,
    aw.embedding_model,
    aw.embedding_version,
    v.channel,
    v.title AS video_title,
    v.upload_date,
    v.upload_date_source,
    v.upload_date_precision,
    v.language,
    v.tickers
   FROM public.agent_workspace aw
     JOIN public.yt_videos v ON v.video_id = aw.video_id
  WHERE aw.artifact_type = 'yt_chunk'::text;

-- WICHTIG: DROP VIEW loescht die ACLs. Ohne diese GRANTs schlaegt jede Suche ueber
-- PostgREST mit "permission denied for view yt_chunks" fehl (PostgREST nutzt die Rolle
-- anon/service_role). Exakt die Rechte aus dem Zustand vor der Migration.
GRANT SELECT ON TABLE public.yt_chunks TO anon;
GRANT SELECT ON TABLE public.yt_chunks TO service_role;
GRANT SELECT ON TABLE public.yt_chunks TO authenticator;

COMMIT;

NOTIFY pgrst, 'reload schema';
