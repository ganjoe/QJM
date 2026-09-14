CREATE OR REPLACE FUNCTION public.get_yt_chunks_with_tickers(
  p_agent_id text, p_channel_filter text DEFAULT NULL, p_days_back integer DEFAULT NULL, p_limit integer DEFAULT 500)
RETURNS TABLE(id uuid, content text, metadata jsonb, created_at timestamp with time zone)
LANGUAGE plpgsql
AS $fn$
BEGIN
  RETURN QUERY
  SELECT aw.id, aw.content, aw.metadata, aw.created_at
  FROM agent_workspace aw
  JOIN yt_videos v ON v.video_id = aw.video_id
  WHERE aw.agent_id = p_agent_id
    AND aw.artifact_type = 'yt_chunk'
    AND jsonb_array_length(aw.metadata->'tickers') > 0
    AND (p_channel_filter IS NULL OR aw.metadata->>'channel' = p_channel_filter)
    AND (p_days_back IS NULL OR v.upload_date >= ((now() at time zone 'UTC')::date - p_days_back))
  ORDER BY v.upload_date DESC NULLS LAST
  LIMIT p_limit;
END;
$fn$;