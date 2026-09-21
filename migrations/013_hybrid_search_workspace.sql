-- 013_hybrid_search_workspace.sql
--
-- Problem: search_influencer_posts waehlte bisher ENTWEDER exact_search_workspace
-- ODER semantic_search_workspace (je nach Query-Form). Laengere Themen-Queries liefen
-- damit nur ueber die Vektorsuche ohne Keyword-Rueckhalt, Ticker nur ueber ILIKE.
-- search_yt_chunks macht es seit Phase 4 per RRF-Fusion. Diese Migration zieht das
-- additiv fuer agent_workspace nach; die bestehenden Funktionen bleiben unveraendert.
BEGIN;

CREATE OR REPLACE FUNCTION public.hybrid_search_workspace(
  query_embedding extensions.vector DEFAULT NULL,
  query_text text DEFAULT NULL,
  match_threshold double precision DEFAULT 0.0,
  match_count integer DEFAULT 200,
  p_agent_id text DEFAULT NULL,
  p_artifact_type text DEFAULT NULL,
  p_days_back integer DEFAULT NULL,
  p_authors text[] DEFAULT NULL
)
RETURNS TABLE(
  id uuid, agent_id text, artifact_type text, content text, metadata jsonb,
  similarity double precision, keyword_rank real, score double precision,
  created_at timestamp with time zone
)
LANGUAGE plpgsql
STABLE
SET search_path TO 'public', 'extensions'
AS $function$
DECLARE
  v_q text := NULLIF(btrim(coalesce(query_text, '')), '');
BEGIN
  RETURN QUERY
  WITH vec AS (
    SELECT t.id,
           1 - (t.embedding <=> query_embedding) AS similarity,
           row_number() OVER (ORDER BY t.embedding <=> query_embedding) AS rnk
    FROM agent_workspace t
    WHERE query_embedding IS NOT NULL
      AND t.embedding IS NOT NULL
      AND (p_agent_id IS NULL OR t.agent_id = p_agent_id)
      AND (p_artifact_type IS NULL OR t.artifact_type = p_artifact_type)
      AND (p_days_back IS NULL OR t.created_at >= NOW() - (p_days_back || ' days')::interval)
      AND (p_authors IS NULL OR array_length(p_authors, 1) IS NULL
           OR LOWER(t.metadata->>'author') = ANY(p_authors)
           OR LOWER(regexp_replace(t.metadata->>'author', '^[@]', '')) = ANY(p_authors))
      AND (1 - (t.embedding <=> query_embedding)) >= match_threshold
    ORDER BY t.embedding <=> query_embedding
    LIMIT 200
  ), kw AS (
    SELECT t.id,
           row_number() OVER (ORDER BY t.created_at DESC) AS rnk
    FROM agent_workspace t
    WHERE v_q IS NOT NULL
      AND (p_agent_id IS NULL OR t.agent_id = p_agent_id)
      AND (p_artifact_type IS NULL OR t.artifact_type = p_artifact_type)
      AND (p_days_back IS NULL OR t.created_at >= NOW() - (p_days_back || ' days')::interval)
      AND (p_authors IS NULL OR array_length(p_authors, 1) IS NULL
           OR LOWER(t.metadata->>'author') = ANY(p_authors)
           OR LOWER(regexp_replace(t.metadata->>'author', '^[@]', '')) = ANY(p_authors))
      AND (
        t.metadata->'tickers' @> to_jsonb(v_q)
        OR t.metadata->'keywords' @> to_jsonb(v_q)
        OR t.metadata->'topics' @> to_jsonb(v_q)
        OR UPPER(t.metadata->>'author') = UPPER(v_q)
        OR UPPER(t.metadata->>'author') = UPPER('@' || regexp_replace(v_q, '^[@]', ''))
        OR t.content ILIKE '%' || v_q || '%'
        OR EXISTS (
          SELECT 1 FROM x_users u
          WHERE (LOWER(u.username) = LOWER(regexp_replace(v_q, '^[@]', ''))
                 OR LOWER(u.screen_name) = LOWER(regexp_replace(v_q, '^[@]', '')))
            AND (UPPER(t.metadata->>'author') = UPPER('@' || u.username)
                 OR UPPER(t.metadata->>'author') = UPPER(u.username))
        )
      )
    LIMIT 200
  ), fused AS (
    SELECT COALESCE(v.id, k.id) AS id,
           (COALESCE(1.0/(60.0 + v.rnk), 0) + COALESCE(1.0/(60.0 + k.rnk), 0))::double precision AS score,
           COALESCE(v.similarity, 0)::double precision AS similarity,
           COALESCE(k.rnk, 0)::real AS keyword_rank
    FROM vec v FULL OUTER JOIN kw k ON v.id = k.id
  )
  SELECT t.id, t.agent_id, t.artifact_type, t.content, t.metadata,
         f.similarity, f.keyword_rank, f.score, t.created_at
  FROM fused f
  JOIN agent_workspace t ON t.id = f.id
  ORDER BY f.score DESC, t.created_at DESC
  LIMIT match_count;
END;
$function$;

COMMENT ON FUNCTION public.hybrid_search_workspace(extensions.vector, text, double precision, integer, text, text, integer, text[])
  IS 'Hybrid (Vektor + Keyword, RRF) fuer agent_workspace; Pendant zu search_yt_chunks.';

NOTIFY pgrst, 'reload schema';

COMMIT;
