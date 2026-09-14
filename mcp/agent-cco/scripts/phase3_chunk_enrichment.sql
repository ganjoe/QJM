-- Phase 3: strukturierte Chunk-Anreicherung (KEIN Re-Embedding, content unveraendert)
BEGIN;
CREATE OR REPLACE FUNCTION _yt_tc_to_sec(a text, b text, c text) RETURNS int
LANGUAGE sql IMMUTABLE AS $fn$
  SELECT CASE
    WHEN a IS NULL THEN NULL
    WHEN c IS NULL THEN a::int * 60 + b::int
    ELSE a::int * 3600 + b::int * 60 + c::int
  END;
$fn$;

WITH tc AS (
  SELECT aw.id,
    (array_agg(m[1] ORDER BY ord ASC))[1]  AS f1,
    (array_agg(m[2] ORDER BY ord ASC))[1]  AS f2,
    (array_agg(m[3] ORDER BY ord ASC))[1]  AS f3,
    (array_agg(m[1] ORDER BY ord DESC))[1] AS l1,
    (array_agg(m[2] ORDER BY ord DESC))[1] AS l2,
    (array_agg(m[3] ORDER BY ord DESC))[1] AS l3
  FROM agent_workspace aw
  CROSS JOIN LATERAL regexp_matches(aw.content, '(?m)^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]$', 'g') WITH ORDINALITY AS rm(m, ord)
  WHERE aw.artifact_type='yt_chunk'
  GROUP BY aw.id
)
UPDATE agent_workspace aw SET
  video_id          = aw.metadata->>'video_id',
  chunk_index       = (aw.metadata->>'block_index')::int,
  t_start_sec       = _yt_tc_to_sec(tc.f1, tc.f2, tc.f3),
  t_end_sec         = _yt_tc_to_sec(tc.l1, tc.l2, tc.l3),
  embedding_model   = 'qwen3-embedding:8b',
  embedding_version = 'legacy-v1',
  embedded_at       = aw.created_at,
  source_hash       = md5(aw.content)
FROM tc
WHERE aw.id = tc.id AND aw.artifact_type='yt_chunk';

UPDATE agent_workspace aw SET
  video_id          = aw.metadata->>'video_id',
  chunk_index       = (aw.metadata->>'block_index')::int,
  embedding_model   = 'qwen3-embedding:8b',
  embedding_version = 'legacy-v1',
  embedded_at       = aw.created_at,
  source_hash       = md5(aw.content)
WHERE aw.artifact_type='yt_chunk' AND aw.video_id IS NULL;
COMMIT;
