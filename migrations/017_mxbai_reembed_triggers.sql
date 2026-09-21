-- 017_mxbai_reembed_triggers.sql
--
-- P6: Re-Embed anstossen (nach P5 und nach dem Gateway-Wechsel auf mxbai-embed-large).
--
--   YT:  'embedded' -> 'downloaded' -> der YT-Worker chunkt mit YT_CHUNK_SIZE=800 neu
--        (delete/insert je video_id) und embeddet mit mxbai.
--   X:   alle x_posts auf 'pending_embedding' -> der Embedding-Worker vektorisiert neu.
--        Die Metadaten (llm_categorized/topics/tickers) bleiben unangetastet.
--
-- open_brain / x_users / yt_channels laufen NICHT hierueber, sondern ueber
-- scripts/reembed_misc_tables.ts (dort liegen die Texte in anderen Spalten).

BEGIN;

UPDATE public.yt_videos
   SET status = 'downloaded',
       retry_count = 0,
       next_retry_at = now(),
       error_msg = NULL
 WHERE status = 'embedded';

UPDATE public.agent_workspace
   SET status = 'pending_embedding'
 WHERE artifact_type = 'x_post';

COMMIT;
