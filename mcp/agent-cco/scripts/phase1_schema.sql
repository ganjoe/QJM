-- Phase 1: additive Schema-Migration (kompatibel; published_at bleibt vorerst)
BEGIN;
ALTER TABLE yt_videos
  ADD COLUMN IF NOT EXISTS upload_date date,
  ADD COLUMN IF NOT EXISTS upload_date_source text,
  ADD COLUMN IF NOT EXISTS upload_date_precision text,
  ADD COLUMN IF NOT EXISTS metadata_synced_at timestamptz;

ALTER TABLE yt_videos DROP CONSTRAINT IF EXISTS chk_yt_upload_date_source;
ALTER TABLE yt_videos ADD CONSTRAINT chk_yt_upload_date_source
  CHECK (upload_date IS NULL OR upload_date_source IS NOT NULL);
ALTER TABLE yt_videos DROP CONSTRAINT IF EXISTS chk_yt_upload_date_source_valid;
ALTER TABLE yt_videos ADD CONSTRAINT chk_yt_upload_date_source_valid
  CHECK (upload_date_source IS NULL OR upload_date_source IN ('exact','approximate'));
ALTER TABLE yt_videos DROP CONSTRAINT IF EXISTS chk_yt_upload_date_precision_valid;
ALTER TABLE yt_videos ADD CONSTRAINT chk_yt_upload_date_precision_valid
  CHECK (upload_date_precision IS NULL OR upload_date_precision IN ('day','month','year'));

CREATE INDEX IF NOT EXISTS idx_yt_videos_upload_date ON yt_videos(upload_date DESC);
CREATE INDEX IF NOT EXISTS idx_yt_videos_channel_date ON yt_videos(channel, upload_date DESC);

ALTER TABLE agent_workspace
  ADD COLUMN IF NOT EXISTS video_id text,
  ADD COLUMN IF NOT EXISTS chunk_index integer,
  ADD COLUMN IF NOT EXISTS t_start_sec integer,
  ADD COLUMN IF NOT EXISTS t_end_sec integer,
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_version text,
  ADD COLUMN IF NOT EXISTS embedded_at timestamptz,
  ADD COLUMN IF NOT EXISTS source_hash text;

ALTER TABLE agent_workspace DROP CONSTRAINT IF EXISTS fk_aw_video;
ALTER TABLE agent_workspace ADD CONSTRAINT fk_aw_video
  FOREIGN KEY (video_id) REFERENCES yt_videos(video_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_aw_yt_chunk_uid
  ON agent_workspace(video_id, chunk_index)
  WHERE artifact_type='yt_chunk' AND video_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_aw_yt_video
  ON agent_workspace(video_id) WHERE artifact_type='yt_chunk';
CREATE INDEX IF NOT EXISTS idx_aw_yt_fts
  ON agent_workspace USING gin (to_tsvector('english', content)) WHERE artifact_type='yt_chunk';

CREATE OR REPLACE VIEW yt_chunks AS
SELECT aw.id, aw.video_id, aw.chunk_index, aw.t_start_sec, aw.t_end_sec,
       aw.content, aw.embedding, aw.metadata,
       aw.created_at AS chunk_created_at, aw.embedded_at,
       aw.embedding_model, aw.embedding_version,
       v.channel, v.title AS video_title, v.upload_date,
       v.upload_date_source, v.upload_date_precision, v.language, v.tickers
FROM agent_workspace aw
JOIN yt_videos v ON v.video_id = aw.video_id
WHERE aw.artifact_type='yt_chunk';
COMMIT;
