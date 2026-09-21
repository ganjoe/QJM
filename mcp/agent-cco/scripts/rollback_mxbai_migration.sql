-- P11 ROLLBACK (vor dem Cutover/P8-Finalisierung): Zustand von vor der MXBAI-Migration
-- wiederherstellen. Danach zusaetzlich:
--   1) llm-gateway/switchyard-config/routes.toml: id = "qwen3-embedding:8b"
--      llm-gateway/switchyard/gateway.py: Fallback-Default zurueck auf qwen3-embedding:8b
--      (oder: git checkout -- llm-gateway/)
--   2) docker compose up -d --force-recreate switchyard
--   3) Code zurueckrollen: git checkout -- mcp/agent-cco/
--   4) docker restart llm-gw-mcp-cco
--
-- Die neuen 1024er Vektoren gehen dabei verloren (sie sind ohnehin unvollstaendig).
-- Die alten 4096er Vektoren sind vollstaendig in *_old_4096 erhalten.

BEGIN;

DROP VIEW IF EXISTS public.yt_chunks;

ALTER TABLE public.agent_workspace DROP COLUMN IF EXISTS embedding;
ALTER TABLE public.open_brain      DROP COLUMN IF EXISTS embedding;
ALTER TABLE public.x_users         DROP COLUMN IF EXISTS embedding;
ALTER TABLE public.yt_channels     DROP COLUMN IF EXISTS embedding;

ALTER TABLE public.agent_workspace RENAME COLUMN embedding_old_4096 TO embedding;
ALTER TABLE public.open_brain      RENAME COLUMN embedding_old_4096 TO embedding;
ALTER TABLE public.x_users         RENAME COLUMN embedding_old_4096 TO embedding;
ALTER TABLE public.yt_channels     RENAME COLUMN embedding_old_4096 TO embedding;

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

GRANT SELECT ON TABLE public.yt_chunks TO anon;
GRANT SELECT ON TABLE public.yt_chunks TO service_role;
GRANT SELECT ON TABLE public.yt_chunks TO authenticator;

COMMIT;

NOTIFY pgrst, 'reload schema';
