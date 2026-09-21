-- 024_rebuild_first_mentions_published_at.sql
-- (umbenannt von 022: die Nummer 022 ist durch 022_ticker_breadth.sql belegt und wurde
--  zuerst angewendet; diese Migration lief danach. Inhalt unveraendert.)
-- Konsistenz zwischen inkrementeller Pflege (updateFirstMentions -> upsert_first_mention)
-- und dem vollstaendigen Rebuild herstellen: Beide nutzen jetzt published_at (echte
-- Tweet-Zeit), Fallback created_at. Zuvor nutzte der Rebuild nur created_at; ein
-- Rebuild haette damit 63 Zeitstempel gegensaetzlich zur laufenden Tabelle gesetzt.
--
-- safe_timestamptz(): toleranter Cast, damit eine einzelne kaputte published_at-Angabe
-- nicht den kompletten Rebuild abbricht (dann greift created_at).

CREATE OR REPLACE FUNCTION public.safe_timestamptz(p text)
RETURNS timestamptz
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
  IF p IS NULL OR btrim(p) = '' THEN
    RETURN NULL;
  END IF;
  RETURN p::timestamptz;
EXCEPTION WHEN others THEN
  RETURN NULL;
END;
$$;

COMMENT ON FUNCTION public.safe_timestamptz(text) IS
  'Toleranter timestamptz-Cast: NULL bei leerem/kaputtem Input statt Fehler.';

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
           lower(
             CASE
               WHEN coalesce(nullif(t.metadata->>'author', ''), '') = '' THEN '@unknown'
               WHEN t.metadata->>'author' LIKE '@%' THEN t.metadata->>'author'
               ELSE '@' || (t.metadata->>'author')
             END
           ) AS author,
           -- Wie sanePublishedAt() im Worker: unplausible/kaputte published_at
           -- (< 2000, z. B. 1970-Outlier) fallen auf created_at zurueck, statt die
           -- Zeile zu verwerfen. Sonst weichen inkrementelle Pflege und Rebuild ab.
           CASE
             WHEN public.safe_timestamptz(t.metadata->>'published_at') >= timestamptz '2000-01-01'
               THEN public.safe_timestamptz(t.metadata->>'published_at')
             ELSE t.created_at
           END AS ts
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
  'Baut x_first_mentions idempotent aus agent_workspace.metadata.tickers neu; nutzt published_at (Fallback created_at) wie updateFirstMentions().';

notify pgrst, 'reload schema';
