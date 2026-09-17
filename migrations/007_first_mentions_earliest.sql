-- 007: Erst-Erwähnungen dürfen nur vom frühesten Post gesetzt/überschrieben werden.
--
-- Problem: Der Metadata-Worker nutzte ein blindes Upsert
--   .upsert(..., { onConflict: "ticker,author" })
-- Dadurch überschrieb JEDE spätere Erwähnung first_mentioned_at nach vorne;
-- max(first_mentioned_at) war praktisch immer der Verarbeitungszeitpunkt und
-- discover_ticker_mentions lieferte "letzte" statt "erste" Erwähnungen.
CREATE OR REPLACE FUNCTION public.upsert_first_mention(
  p_ticker text,
  p_author text,
  p_first_mentioned_at timestamptz,
  p_post_id uuid
)
RETURNS void
LANGUAGE sql
AS $$
  INSERT INTO public.x_first_mentions (ticker, author, first_mentioned_at, post_id)
  VALUES (
    upper(regexp_replace(p_ticker, '^[$#]', '')),
    lower(p_author),
    p_first_mentioned_at,
    p_post_id
  )
  ON CONFLICT (ticker, author) DO UPDATE
    SET first_mentioned_at = EXCLUDED.first_mentioned_at,
        post_id            = EXCLUDED.post_id
    WHERE EXCLUDED.first_mentioned_at < public.x_first_mentions.first_mentioned_at;
$$;

COMMENT ON FUNCTION public.upsert_first_mention(text, text, timestamptz, uuid) IS
  'Idempotenter Erst-Erwähnungs-Upsert: hält immer den frühesten Post pro (Ticker, Autor).';

-- Rebaut x_first_mentions vollständig aus den (nach Backfill korrigierten)
-- metadata.tickers. created_at ist der Publikationszeitpunkt des Posts.
CREATE OR REPLACE FUNCTION public.rebuild_x_first_mentions()
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
  n integer;
BEGIN
  DELETE FROM public.x_first_mentions;
  INSERT INTO public.x_first_mentions (ticker, author, first_mentioned_at, post_id)
  SELECT DISTINCT ON (ticker, author) ticker, author, ts, id
  FROM (
    SELECT t.id,
           upper(regexp_replace(tk, '^[$#]', '')) AS ticker,
           lower(coalesce(nullif(t.metadata->>'author', ''), 'unknown')) AS author,
           t.created_at AS ts
    FROM public.agent_workspace t
    CROSS JOIN LATERAL jsonb_array_elements_text(t.metadata->'tickers') AS tk
    WHERE t.artifact_type = 'x_post'
      AND jsonb_typeof(t.metadata->'tickers') = 'array'
      AND tk ~ '^[$#]?[A-Za-z0-9]'
  ) s
  WHERE ts IS NOT NULL AND ts >= timestamptz '2000-01-01'
  ORDER BY ticker, author, ts ASC, id;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

COMMENT ON FUNCTION public.rebuild_x_first_mentions() IS
  'Baut x_first_mentions idempotent aus agent_workspace.metadata.tickers neu.';

-- Kandidaten-Auswahl für den idempotenten Backfill. Keyset-Pagination über
-- (created_at, id), damit bereits verarbeitete Zeilen die Seiten nicht verschieben.
-- DROP nötig, weil sich die RETURNS TABLE-Signatur (has_embedding) geändert hat.
DROP FUNCTION IF EXISTS public.x_metadata_candidates(timestamptz, timestamptz, boolean, integer, timestamptz, uuid);
CREATE OR REPLACE FUNCTION public.x_metadata_candidates(
  p_from timestamptz DEFAULT NULL,
  p_to timestamptz DEFAULT NULL,
  p_only_cashtag boolean DEFAULT true,
  p_limit integer DEFAULT 100,
  p_cursor_at timestamptz DEFAULT NULL,
  p_cursor_id uuid DEFAULT NULL
)
RETURNS TABLE(id uuid, content text, created_at timestamptz, metadata jsonb, has_embedding boolean)
LANGUAGE sql
STABLE
AS $$
  SELECT t.id, t.content, t.created_at, t.metadata, (t.embedding IS NOT NULL) AS has_embedding
  FROM public.agent_workspace t
  WHERE t.artifact_type = 'x_post'
    AND (t.metadata->'tickers' IS NULL OR t.metadata->'tickers' = '[]'::jsonb)
    AND (p_from IS NULL OR t.created_at >= p_from)
    AND (p_to IS NULL OR t.created_at <= p_to)
    AND (NOT p_only_cashtag OR t.content LIKE '%$%')
    AND (p_cursor_at IS NULL OR (t.created_at, t.id) < (p_cursor_at, p_cursor_id))
  ORDER BY t.created_at DESC, t.id DESC
  LIMIT p_limit;
$$;

COMMENT ON FUNCTION public.x_metadata_candidates(timestamptz, timestamptz, boolean, integer, timestamptz, uuid) IS
  'X-Posts mit leerem metadata.tickers im Datumsbereich (optional nur mit $), Keyset-paginiert für den Backfill.';
