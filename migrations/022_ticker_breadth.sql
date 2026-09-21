-- ============================================================================
-- 022_ticker_breadth.sql
--
-- Zwei RPC-Funktionen fuer Report-Aufgaben ueber den Influencer-Korpus.
-- Sie ersetzen den teuersten Fehler des Laufs 2dd854d4: der CCO hat 1107 Posts
-- Volltext durch den Modellkontext gezogen (4 Chunks, je 300-480k Tokens,
-- alle gerissen, 2,02 Mio insgesamt), obwohl die Ticker bereits in
-- agent_workspace.metadata->'tickers' liegen.
--
-- Arbeitsteilung (siehe WORKITEM_HARDENING_PLAN.md, WP1/WP2):
--   ticker_breadth   — ERSTER Schritt: welche Ticker, wie viele VERSCHIEDENE
--                      Autoren, und ist der Ticker neu? Ein Aufruf, ~2-4k Tokens.
--   ticker_evidence  — ZWEITER Schritt: 2-3 Belege NUR fuer die Top-Kandidaten,
--                      mit Post-ID (x_external_id).
--
-- Bewusst NICHT ueber Regex auf content: metadata->'tickers' ist die gepflegte
-- Quelle (LLM-Extraktion beim Ingest, erkennt auch Ticker ohne Cashtag) und
-- liefert mehr Treffer als ein \$-Regex. Stand 20.09.2026 im Fenster 19./20.09.:
-- metadata 422 Ticker / 1065 Nennungen gegen Regex 309 / 755 — metadata ist
-- Obermenge, nicht Ersatz.
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/022_ticker_breadth.sql
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Breite eines Tickers in einem Zeitfenster.
-- ---------------------------------------------------------------------------
create or replace function ticker_breadth(
  p_start       timestamptz,
  p_end         timestamptz,
  p_min_authors integer default 2,       -- 1 = alles, 2 = nur Mehrfach-Belege
  p_only_new    boolean default false,   -- nur Ticker, deren Erstnennung im Fenster liegt
  p_limit       integer default 50
) returns table (
  ticker             text,
  autoren            integer,     -- VERSCHIEDENE Autoren im Fenster
  nennungen          integer,
  erstmals_im_korpus timestamptz, -- aus x_first_mentions
  neu_im_fenster     boolean,     -- erstmals_im_korpus >= p_start
  nicht_us           boolean      -- Suffix wie .L/.WA/.SR/.DE/.PA
)
language sql
stable
as $$
  with fenster as (
    select w.metadata->>'author' as autor,
           upper(t.ticker)       as ticker
      from agent_workspace w,
           jsonb_array_elements_text(w.metadata->'tickers') as t(ticker)
     where w.artifact_type = 'x_post'
       and coalesce((w.metadata->>'published_at')::timestamptz, w.created_at)
           between p_start and p_end
  ),
  agg as (
    select f.ticker,
           count(distinct f.autor) as autoren,
           count(*)                as nennungen
      from fenster f
     group by f.ticker
  ),
  erst as (
    select upper(fm.ticker) as ticker, min(fm.first_mentioned_at) as erstmals
      from x_first_mentions fm
     group by upper(fm.ticker)
  )
  select a.ticker,
         a.autoren::integer,
         a.nennungen::integer,
         e.erstmals,
         coalesce(e.erstmals >= p_start, false)      as neu_im_fenster,
         a.ticker ~ '\.[A-Z]{1,3}$'                 as nicht_us
    from agg a
    left join erst e on e.ticker = a.ticker
   where a.autoren >= p_min_authors
     and (not p_only_new or coalesce(e.erstmals >= p_start, false))
   order by a.autoren desc, a.nennungen desc, a.ticker
   limit greatest(p_limit, 1);
$$;

comment on function ticker_breadth(timestamptz, timestamptz, integer, boolean, integer) is
  'Breiten-Ranking der in einem Zeitfenster genannten Ticker. Liefert verschiedene '
  'Autoren, Nennungen, Erstnennung im Korpus und ein Nicht-US-Flag. Erster Schritt '
  'jedes Watchlist-/Rangberichts — ersetzt das Lesen des Korpus durch das Modell.';

-- ---------------------------------------------------------------------------
-- Belege zu EINEM Ticker, gedeckelt. Zweiter Trichter-Schritt.
-- ---------------------------------------------------------------------------
create or replace function ticker_evidence(
  p_ticker text,
  p_start  timestamptz,
  p_end    timestamptz,
  p_limit  integer default 3
) returns table (
  autor   text,
  zeit    timestamptz,
  post_id text,     -- x_external_id (Tweet-ID); fehlte dem CCO bisher komplett
  auszug  text      -- erste 240 Zeichen, Whitespace normalisiert
)
language sql
stable
as $$
  select w.metadata->>'author'                                             as autor,
         coalesce((w.metadata->>'published_at')::timestamptz, w.created_at) as zeit,
         w.x_external_id                                                   as post_id,
         left(regexp_replace(w.content, '\s+', ' ', 'g'), 240)             as auszug
    from agent_workspace w
   where w.artifact_type = 'x_post'
     and exists (
       select 1
         from jsonb_array_elements_text(w.metadata->'tickers') as x(t)
        where upper(t) = upper(p_ticker)
     )
     and coalesce((w.metadata->>'published_at')::timestamptz, w.created_at)
         between p_start and p_end
   order by (case when (w.metadata->'public_metrics'->>'like_count') ~ '^[0-9]+$'
                  then (w.metadata->'public_metrics'->>'like_count')::numeric
                  else 0 end) desc,
            zeit desc
   limit greatest(p_limit, 1);
$$;

comment on function ticker_evidence(text, timestamptz, timestamptz, integer) is
  'Beleg-Posts zu einem Ticker im Zeitfenster, nach Engagement sortiert, mit '
  'x_external_id als nachvollziehbarer Post-ID.';

-- ---------------------------------------------------------------------------
-- Rechte wie bei den uebrigen neuen Objekten (PostgREST greift als anon/service_role).
-- Beide Funktionen laufen security invoker — agent_workspace hat die noetigen Grants.
-- ---------------------------------------------------------------------------
grant execute on function ticker_breadth(timestamptz, timestamptz, integer, boolean, integer)
  to anon, service_role;
grant execute on function ticker_evidence(text, timestamptz, timestamptz, integer)
  to anon, service_role;

notify pgrst, 'reload schema';
