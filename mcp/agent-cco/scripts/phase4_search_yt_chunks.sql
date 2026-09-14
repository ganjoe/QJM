-- Phase 4: kanonische Hybrid-Suche fuer YT-Chunks
CREATE OR REPLACE FUNCTION public.search_yt_chunks(
  p_query text,
  p_embedding extensions.vector DEFAULT NULL,
  p_channels text[] DEFAULT NULL,
  p_date_from date DEFAULT NULL,
  p_date_to date DEFAULT NULL,
  p_tickers text[] DEFAULT NULL,
  p_language text DEFAULT NULL,
  p_min_similarity double precision DEFAULT 0.0,
  p_limit integer DEFAULT 20
)
RETURNS TABLE (
  chunk_id uuid,
  video_id text,
  channel text,
  title text,
  upload_date date,
  upload_date_source text,
  upload_date_precision text,
  chunk_index integer,
  t_start_sec integer,
  t_end_sec integer,
  content text,
  url text,
  similarity double precision,
  keyword_rank real,
  score double precision,
  matched_terms text[]
)
LANGUAGE plpgsql STABLE
SET search_path TO 'public', 'extensions'
AS $fn$
DECLARE
  v_tsquery tsquery;
BEGIN
  IF p_query IS NOT NULL AND length(trim(p_query)) > 0 THEN
    v_tsquery := websearch_to_tsquery('english', p_query);
  END IF;

  RETURN QUERY
  WITH vec AS (
    SELECT c.id, 1 - (c.embedding <=> p_embedding) AS similarity,
           row_number() OVER (ORDER BY c.embedding <=> p_embedding) AS rnk
    FROM yt_chunks c
    WHERE p_embedding IS NOT NULL
      AND (p_channels IS NULL OR c.channel = ANY(p_channels))
      AND (p_date_from IS NULL OR (c.upload_date IS NOT NULL AND c.upload_date >= p_date_from))
      AND (p_date_to   IS NULL OR (c.upload_date IS NOT NULL AND c.upload_date <  p_date_to))
      AND (p_language IS NULL OR c.language = p_language)
      AND (p_tickers IS NULL OR c.tickers && p_tickers)
      AND (1 - (c.embedding <=> p_embedding)) >= p_min_similarity
    ORDER BY c.embedding <=> p_embedding
    LIMIT 200
  ),
  kw AS (
    SELECT c.id,
           ts_rank(to_tsvector('english', c.content), v_tsquery) AS rank,
           row_number() OVER (ORDER BY ts_rank(to_tsvector('english', c.content), v_tsquery) DESC) AS rnk
    FROM yt_chunks c
    WHERE v_tsquery IS NOT NULL
      AND to_tsvector('english', c.content) @@ v_tsquery
      AND (p_channels IS NULL OR c.channel = ANY(p_channels))
      AND (p_date_from IS NULL OR (c.upload_date IS NOT NULL AND c.upload_date >= p_date_from))
      AND (p_date_to   IS NULL OR (c.upload_date IS NOT NULL AND c.upload_date <  p_date_to))
      AND (p_language IS NULL OR c.language = p_language)
      AND (p_tickers IS NULL OR c.tickers && p_tickers)
    LIMIT 200
  ),
  fused AS (
    SELECT COALESCE(v.id, k.id) AS id,
           COALESCE(1.0 / (60.0 + v.rnk), 0) + COALESCE(1.0 / (60.0 + k.rnk), 0) AS score,
           COALESCE(v.similarity, 0) AS similarity,
           COALESCE(k.rank, 0) AS keyword_rank
    FROM vec v FULL OUTER JOIN kw k ON v.id = k.id
  )
  SELECT c.id, c.video_id, c.channel, c.video_title, c.upload_date, c.upload_date_source,
         c.upload_date_precision, c.chunk_index, c.t_start_sec, c.t_end_sec,
         regexp_replace(c.content, '^\[Video:[^\n]*\]\s*\n\s*\n', '') AS content,
         'https://www.youtube.com/watch?v=' || c.video_id ||
           CASE WHEN c.t_start_sec IS NOT NULL THEN '&t=' || c.t_start_sec || 's' ELSE '' END AS url,
         f.similarity, f.keyword_rank, f.score,
         ARRAY(SELECT w FROM unnest(regexp_split_to_array(lower(coalesce(p_query,'')), '\W+')) w
               WHERE length(w) > 2 AND c.content ILIKE '%' || w || '%') AS matched_terms
  FROM fused f
  JOIN yt_chunks c ON c.id = f.id
  ORDER BY f.score DESC, c.upload_date DESC NULLS LAST, c.video_id, c.chunk_index
  LIMIT p_limit;
END;
$fn$;
