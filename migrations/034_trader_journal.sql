-- ============================================================================
-- 034_trader_journal.sql
--
-- Trader-Tagebuch (MCP-Server openbrain-journal).
--
-- PRINZIPIEN (siehe TRADER_JOURNAL_MCP_PLAN.md):
--   * Der Originalbeitrag ist unantastbar: journal_entry.body wird nie ueberschrieben.
--     Ergaenzungen sind neue Eintraege, die per journal_entry_link angehaengt werden.
--   * Thesen sind das pruefbare Destillat (1:n zu einem Eintrag).
--   * reminder_date ist das Sekundaerdatum: Zukunft/Heute = Wiedervorlage,
--     Vergangenheit = historische Referenz. Es gibt keine Flags — die Semantik
--     entsteht beim Lesen aus dem Vergleich mit journal_today().
--   * Kein UNIQUE auf dedupe_hash (anders als open_brain): derselbe Gedanke an einem
--     anderen Tag ist eine neue Information. Dedupe nur innerhalb eines Zeitfensters.
--   * Vektor-RPCs brauchen SET search_path TO 'public','extensions' (Migration 018).
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/034_trader_journal.sql
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 0) Hilfsfunktionen
-- ---------------------------------------------------------------------------

-- "Heute" im Tagebuch-Zeitzonenraum. Der Container laeuft UTC; alle Datumsfelder
-- des Journals sind Berliner Kalendertage. Eine Aenderung hier MUSS mit
-- JOURNAL_TZ im Container (llm-gateway/docker-compose.yml, Service mcp-journal)
-- einhergehen.
CREATE OR REPLACE FUNCTION public.journal_today() RETURNS date
LANGUAGE sql STABLE AS $fn$
  SELECT (now() AT TIME ZONE 'Europe/Berlin')::date;
$fn$;

COMMENT ON FUNCTION public.journal_today() IS
  'Kalendertag des Journals (Europe/Berlin). Muss zu JOURNAL_TZ des MCP-Servers passen.';

-- Loest eine vollstaendige UUID oder einen eindeutigen Praefix (min. 6 Zeichen)
-- auf. Bei Mehrdeutigkeit wird ein Fehler geworfen — niemals geraten.
CREATE OR REPLACE FUNCTION public.journal_resolve_id(
  p_id text,
  p_include_deleted boolean DEFAULT false
) RETURNS uuid
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
  v_id uuid;
  v_n int;
  v_candidates text;
  v_pfx text;
BEGIN
  IF p_id IS NULL OR btrim(p_id) = '' THEN
    RAISE EXCEPTION 'journal: leere ID';
  END IF;

  BEGIN
    v_id := btrim(p_id)::uuid;
    RETURN v_id;
  EXCEPTION WHEN invalid_text_representation THEN
    NULL; -- kein vollstaendiger UUID-String -> Praefix-Aufloesung
  END;

  v_pfx := lower(btrim(p_id));
  IF length(v_pfx) < 6 THEN
    RAISE EXCEPTION 'journal: Praefix zu kurz (min. 6 Zeichen): %', p_id;
  END IF;

  SELECT count(*) INTO v_n
  FROM public.journal_entry e
  WHERE e.id::text LIKE v_pfx || '%'
    AND (p_include_deleted OR e.deleted_at IS NULL);

  IF v_n = 0 THEN
    RAISE EXCEPTION 'journal: kein Eintrag zu ID/Praefix % gefunden', p_id;
  END IF;

  IF v_n > 1 THEN
    SELECT string_agg(e.id::text, ', ' ORDER BY e.entry_date DESC, e.created_at DESC)
      INTO v_candidates
    FROM public.journal_entry e
    WHERE e.id::text LIKE v_pfx || '%'
      AND (p_include_deleted OR e.deleted_at IS NULL);
    RAISE EXCEPTION 'journal: Praefix % ist mehrdeutig (% Kandidaten): %', p_id, v_n, v_candidates;
  END IF;

  SELECT e.id INTO v_id
  FROM public.journal_entry e
  WHERE e.id::text LIKE v_pfx || '%'
    AND (p_include_deleted OR e.deleted_at IS NULL);

  RETURN v_id;
END;
$fn$;

-- Setzt die Thesen eines Zieleintrags deterministisch auf invalidated/confirmed.
-- Das ist die gewuenschte Klarheit einer Verknuepfung, kein autonomes Schreiben:
-- sie folgt ausschliesslich aus einer expliziten relation.
CREATE OR REPLACE FUNCTION public.journal_apply_relation(
  p_to uuid,
  p_relation text,
  p_note text DEFAULT NULL,
  p_theses uuid[] DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
  v_status text;
  v_n int := 0;
BEGIN
  IF p_relation = 'invalidates' THEN
    v_status := 'invalidated';
  ELSIF p_relation = 'confirms' THEN
    v_status := 'confirmed';
  ELSE
    RETURN 0;
  END IF;

  UPDATE public.journal_thesis t
     SET status = v_status,
         resolved_at = now(),
         outcome_note = COALESCE(
           NULLIF(btrim(COALESCE(p_note, '')), ''),
           t.outcome_note,
           CASE WHEN v_status = 'invalidated'
                THEN 'durch Nachtrag als widerlegt markiert'
                ELSE 'durch Nachtrag als bestaetigt markiert' END)
   WHERE t.entry_id = p_to
     AND t.status = 'open'
     AND (p_theses IS NULL OR t.id = ANY(p_theses));

  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN v_n;
END;
$fn$;

-- ---------------------------------------------------------------------------
-- 1) Tabellen
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.journal_entry (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id           text NOT NULL DEFAULT 'journal',
  entry_date         date NOT NULL DEFAULT public.journal_today(),
  reminder_date      date,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  body               text NOT NULL,
  body_sha256        text NOT NULL,
  summary            text,
  keywords           text[] NOT NULL DEFAULT '{}',
  tickers            text[] NOT NULL DEFAULT '{}',
  companies          text[] NOT NULL DEFAULT '{}',
  themes             text[] NOT NULL DEFAULT '{}',
  entry_type         text NOT NULL DEFAULT 'observation'
                     CHECK (entry_type IN ('thesis','observation','trade_note','review','lesson','macro')),
  source             text NOT NULL DEFAULT 'manual' CHECK (source = 'manual'),
  embedding          extensions.vector(1024),
  embedded_at        timestamptz,
  embedding_model    text,
  embedding_version  text,
  extraction_failed  boolean NOT NULL DEFAULT false,
  extraction_error   text,
  dedupe_hash        text,
  deleted_at         timestamptz
);

COMMENT ON TABLE public.journal_entry IS
  'Trader-Tagebuch: ein Eintrag = ein woertlich gespeicherter Beitrag. body ist append-only.';
COMMENT ON COLUMN public.journal_entry.reminder_date IS
  'Sekundaerdatum: Zukunft/Heute = Wiedervorlage, Vergangenheit = historische Referenz.';

CREATE TABLE IF NOT EXISTS public.journal_thesis (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entry_id      uuid NOT NULL REFERENCES public.journal_entry(id) ON DELETE CASCADE,
  statement     text NOT NULL,
  tickers       text[] NOT NULL DEFAULT '{}',
  companies     text[] NOT NULL DEFAULT '{}',
  horizon_days  integer,
  falsifier     text,
  confidence    smallint CHECK (confidence BETWEEN 1 AND 5),
  status        text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','confirmed','invalidated','expired')),
  review_at     date,
  outcome_note  text,
  resolved_at   timestamptz,
  embedding     extensions.vector(1024),
  created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.journal_thesis IS
  'Pruefbare Thesen eines Journal-Eintrags (1:n). status wird durch journal_link/relation gesetzt.';

CREATE TABLE IF NOT EXISTS public.journal_entry_link (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  from_entry_id  uuid NOT NULL REFERENCES public.journal_entry(id) ON DELETE CASCADE,
  to_entry_id    uuid NOT NULL REFERENCES public.journal_entry(id) ON DELETE CASCADE,
  relation       text NOT NULL DEFAULT 'follow_up'
                 CHECK (relation IN ('follow_up','confirms','invalidates','related')),
  note           text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (from_entry_id <> to_entry_id),
  UNIQUE (from_entry_id, to_entry_id, relation)
);

COMMENT ON TABLE public.journal_entry_link IS
  'Verknuepfung zweier Eintraege: from = der spaetere Nachtrag, to = das Original.';

CREATE INDEX IF NOT EXISTS idx_journal_entry_date     ON public.journal_entry (agent_id, entry_date DESC, created_at DESC)
  WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_journal_entry_reminder ON public.journal_entry (agent_id, reminder_date)
  WHERE deleted_at IS NULL AND reminder_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_journal_entry_kw       ON public.journal_entry USING gin (keywords);
CREATE INDEX IF NOT EXISTS idx_journal_entry_tick     ON public.journal_entry USING gin (tickers);
CREATE INDEX IF NOT EXISTS idx_journal_entry_comp     ON public.journal_entry USING gin (companies);
CREATE INDEX IF NOT EXISTS idx_journal_entry_dedupe   ON public.journal_entry (dedupe_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_journal_thesis_open    ON public.journal_thesis (status, review_at);
CREATE INDEX IF NOT EXISTS idx_journal_thesis_entry   ON public.journal_thesis (entry_id);
CREATE INDEX IF NOT EXISTS idx_journal_thesis_tick    ON public.journal_thesis USING gin (tickers);
CREATE INDEX IF NOT EXISTS idx_journal_link_from      ON public.journal_entry_link (from_entry_id);
CREATE INDEX IF NOT EXISTS idx_journal_link_to        ON public.journal_entry_link (to_entry_id);

CREATE INDEX IF NOT EXISTS idx_journal_entry_hnsw
  ON public.journal_entry USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_journal_thesis_hnsw
  ON public.journal_thesis USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.journal_entry      TO anon, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.journal_thesis     TO anon, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.journal_entry_link TO anon, service_role;

-- ---------------------------------------------------------------------------
-- 2) journal_capture — Eintrag anlegen (dedupe im Zeitfenster) + Verknuepfungen
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.journal_capture(
  p_agent_id text,
  p_body text,
  p_entry_date date DEFAULT NULL,
  p_reminder_date date DEFAULT NULL,
  p_links jsonb DEFAULT '[]'::jsonb,
  p_dedupe_window_hours int DEFAULT 12
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
  v_hash text;
  v_sha text;
  v_id uuid;
  v_dup boolean := false;
  v_entry_date date;
  v_link jsonb;
  v_to uuid;
  v_rel text;
  v_note text;
  v_theses uuid[];
  v_touched int := 0;
  v_links uuid[] := '{}';
  v_new_link uuid;
BEGIN
  IF p_agent_id IS NULL OR btrim(p_agent_id) = '' THEN
    RAISE EXCEPTION 'journal: agent_id fehlt';
  END IF;
  IF p_body IS NULL OR btrim(p_body) = '' THEN
    RAISE EXCEPTION 'journal: leerer Eintrag';
  END IF;

  v_entry_date := COALESCE(p_entry_date, public.journal_today());
  v_hash := encode(sha256(convert_to(lower(trim(regexp_replace(p_body, '[[:space:]]+', ' ', 'g'))), 'UTF8')), 'hex');

  IF COALESCE(p_dedupe_window_hours, 0) > 0 THEN
    SELECT e.id INTO v_id
    FROM public.journal_entry e
    WHERE e.agent_id = p_agent_id
      AND e.deleted_at IS NULL
      AND e.dedupe_hash = v_hash
      AND e.created_at > now() - make_interval(hours => p_dedupe_window_hours)
    ORDER BY e.created_at DESC
    LIMIT 1;
    IF v_id IS NOT NULL THEN
      v_dup := true;
    END IF;
  END IF;

  IF NOT v_dup THEN
    v_sha := encode(sha256(convert_to(p_body, 'UTF8')), 'hex');
    INSERT INTO public.journal_entry (agent_id, entry_date, reminder_date, body, body_sha256, dedupe_hash)
    VALUES (p_agent_id, v_entry_date, p_reminder_date, p_body, v_sha, v_hash)
    RETURNING id INTO v_id;
  END IF;

  IF NOT v_dup AND p_links IS NOT NULL AND jsonb_typeof(p_links) = 'array' THEN
    FOR v_link IN SELECT * FROM jsonb_array_elements(p_links) LOOP
      v_to := public.journal_resolve_id(v_link->>'to');
      IF v_to = v_id THEN
        RAISE EXCEPTION 'journal: Selbstverknuepfung ist nicht erlaubt';
      END IF;
      v_rel := COALESCE(NULLIF(btrim(COALESCE(v_link->>'relation', '')), ''), 'follow_up');
      IF v_rel NOT IN ('follow_up','confirms','invalidates','related') THEN
        RAISE EXCEPTION 'journal: unbekannte Relation %', v_rel;
      END IF;
      v_note := NULLIF(btrim(COALESCE(v_link->>'note', '')), '');

      v_theses := NULL;
      IF v_link ? 'theses' AND jsonb_typeof(v_link->'theses') = 'array' THEN
        SELECT array_agg(x::uuid) INTO v_theses
        FROM jsonb_array_elements_text(v_link->'theses') AS x;
      END IF;

      v_new_link := NULL;
      INSERT INTO public.journal_entry_link (from_entry_id, to_entry_id, relation, note)
      VALUES (v_id, v_to, v_rel, v_note)
      ON CONFLICT (from_entry_id, to_entry_id, relation) DO NOTHING
      RETURNING id INTO v_new_link;

      IF v_new_link IS NOT NULL THEN
        v_links := v_links || v_new_link;
        v_touched := v_touched + public.journal_apply_relation(v_to, v_rel, v_note, v_theses);
      END IF;
    END LOOP;
  END IF;

  RETURN jsonb_build_object(
    'entry_id', v_id,
    'duplicate_of', v_dup,
    'entry_date', v_entry_date,
    'reminder_date', p_reminder_date,
    'body_sha256', v_sha,
    'link_ids', to_jsonb(v_links),
    'theses_touched', v_touched
  );
END;
$fn$;

-- ---------------------------------------------------------------------------
-- 3) journal_enrich — LLM-Destillat + Thesen + Embedding nachtragen
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.journal_enrich(
  p_entry_id uuid,
  p_summary text DEFAULT NULL,
  p_keywords text[] DEFAULT NULL,
  p_tickers text[] DEFAULT NULL,
  p_companies text[] DEFAULT NULL,
  p_themes text[] DEFAULT NULL,
  p_entry_type text DEFAULT NULL,
  p_reminder_date date DEFAULT NULL,
  p_embedding extensions.vector DEFAULT NULL,
  p_embedding_model text DEFAULT NULL,
  p_embedding_version text DEFAULT NULL,
  p_theses jsonb DEFAULT '[]'::jsonb,
  p_extraction_failed boolean DEFAULT false,
  p_extraction_error text DEFAULT NULL,
  p_replace_theses boolean DEFAULT false
) RETURNS jsonb
LANGUAGE plpgsql
SET search_path TO 'public', 'extensions'
AS $fn$
DECLARE
  v_ids uuid[] := '{}';
  v_thesis jsonb;
  v_new uuid;
  v_existing int;
  v_dims int;
BEGIN
  IF p_entry_id IS NULL THEN
    RAISE EXCEPTION 'journal: entry_id fehlt';
  END IF;

  IF p_embedding IS NOT NULL THEN
    v_dims := vector_dims(p_embedding);
    IF v_dims <> 1024 THEN
      RAISE EXCEPTION 'journal: Embedding-Dimension % <> 1024 (falsche Gateway-Route?)', v_dims;
    END IF;
  END IF;

  UPDATE public.journal_entry e SET
    summary           = COALESCE(p_summary, e.summary),
    keywords          = COALESCE(p_keywords, e.keywords),
    tickers           = COALESCE(p_tickers, e.tickers),
    companies         = COALESCE(p_companies, e.companies),
    themes            = COALESCE(p_themes, e.themes),
    entry_type        = COALESCE(NULLIF(btrim(COALESCE(p_entry_type, '')), ''), e.entry_type),
    reminder_date     = COALESCE(p_reminder_date, e.reminder_date),
    embedding         = COALESCE(p_embedding, e.embedding),
    embedded_at       = CASE WHEN p_embedding IS NOT NULL THEN now() ELSE e.embedded_at END,
    embedding_model   = COALESCE(p_embedding_model, e.embedding_model),
    embedding_version = COALESCE(p_embedding_version, e.embedding_version),
    extraction_failed = p_extraction_failed,
    extraction_error  = p_extraction_error,
    updated_at        = now()
  WHERE e.id = p_entry_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'journal: Eintrag % nicht gefunden', p_entry_id;
  END IF;

  SELECT count(*) INTO v_existing FROM public.journal_thesis t WHERE t.entry_id = p_entry_id;

  IF v_existing > 0 AND p_replace_theses THEN
    DELETE FROM public.journal_thesis t WHERE t.entry_id = p_entry_id;
    v_existing := 0;
  END IF;

  IF v_existing = 0 AND p_theses IS NOT NULL AND jsonb_typeof(p_theses) = 'array' THEN
    FOR v_thesis IN SELECT * FROM jsonb_array_elements(p_theses) LOOP
      IF NULLIF(btrim(COALESCE(v_thesis->>'statement', '')), '') IS NULL THEN
        CONTINUE;
      END IF;
      INSERT INTO public.journal_thesis (
        entry_id, statement, tickers, companies, horizon_days, falsifier, confidence, review_at
      ) VALUES (
        p_entry_id,
        btrim(v_thesis->>'statement'),
        COALESCE(ARRAY(SELECT jsonb_array_elements_text(v_thesis->'tickers')), '{}'),
        COALESCE(ARRAY(SELECT jsonb_array_elements_text(v_thesis->'companies')), '{}'),
        NULLIF(v_thesis->>'horizon_days', '')::int,
        NULLIF(btrim(COALESCE(v_thesis->>'falsifier', '')), ''),
        NULLIF(v_thesis->>'confidence', '')::smallint,
        CASE WHEN NULLIF(v_thesis->>'horizon_days', '') IS NOT NULL
             THEN public.journal_today() + (NULLIF(v_thesis->>'horizon_days', '')::int)
             ELSE NULL END
      ) RETURNING id INTO v_new;
      v_ids := v_ids || v_new;
    END LOOP;
  END IF;

  RETURN jsonb_build_object('entry_id', p_entry_id, 'thesis_count', array_length(v_ids, 1), 'thesis_ids', to_jsonb(v_ids));
END;
$fn$;

-- ---------------------------------------------------------------------------
-- 4) journal_link — Verknuepfung hinzufuegen/entfernen
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.journal_link(
  p_action text,
  p_from text,
  p_to text,
  p_relation text DEFAULT 'follow_up',
  p_note text DEFAULT NULL,
  p_theses uuid[] DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
  v_from uuid;
  v_to uuid;
  v_rel text;
  v_note text;
  v_new uuid;
  v_touched int := 0;
  v_removed int := 0;
BEGIN
  v_from := public.journal_resolve_id(p_from);
  v_to := public.journal_resolve_id(p_to);
  IF v_from = v_to THEN
    RAISE EXCEPTION 'journal: Selbstverknuepfung ist nicht erlaubt';
  END IF;

  IF p_action = 'remove' THEN
    DELETE FROM public.journal_entry_link l
    WHERE l.from_entry_id = v_from AND l.to_entry_id = v_to
      AND (p_relation IS NULL OR btrim(p_relation) = '' OR l.relation = p_relation);
    GET DIAGNOSTICS v_removed = ROW_COUNT;
    RETURN jsonb_build_object('action', 'remove', 'from', v_from, 'to', v_to, 'removed', v_removed,
                              'note', 'Thesenstatus wurde nicht zurueckgenommen');
  END IF;

  IF p_action <> 'add' THEN
    RAISE EXCEPTION 'journal: unbekannte action % (erlaubt: add, remove)', p_action;
  END IF;

  v_rel := COALESCE(NULLIF(btrim(COALESCE(p_relation, '')), ''), 'follow_up');
  IF v_rel NOT IN ('follow_up','confirms','invalidates','related') THEN
    RAISE EXCEPTION 'journal: unbekannte Relation %', v_rel;
  END IF;
  v_note := NULLIF(btrim(COALESCE(p_note, '')), '');

  v_new := NULL;
  INSERT INTO public.journal_entry_link (from_entry_id, to_entry_id, relation, note)
  VALUES (v_from, v_to, v_rel, v_note)
  ON CONFLICT (from_entry_id, to_entry_id, relation) DO NOTHING
  RETURNING id INTO v_new;

  IF v_new IS NOT NULL THEN
    v_touched := public.journal_apply_relation(v_to, v_rel, v_note, p_theses);
  END IF;

  RETURN jsonb_build_object('action', 'add', 'link_id', v_new, 'from', v_from, 'to', v_to,
                            'relation', v_rel, 'inserted', v_new IS NOT NULL, 'theses_touched', v_touched);
END;
$fn$;

-- ---------------------------------------------------------------------------
-- 5) journal_recent — die letzten Tage (chronologisch, ohne body)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.journal_recent(
  p_agent_id text,
  p_days int DEFAULT 3,
  p_limit int DEFAULT 20
) RETURNS TABLE (
  entry_id uuid,
  entry_date date,
  reminder_date date,
  entry_type text,
  summary text,
  keywords text[],
  tickers text[],
  companies text[],
  link_count int,
  link_relations text[],
  thesis_total int,
  thesis_open int,
  thesis_due int,
  last_entry_date date,
  next_reminder_date date
)
LANGUAGE sql STABLE AS $fn$
  WITH base AS (
    SELECT e.id, e.entry_date AS edate, e.reminder_date AS rdate, e.entry_type AS etype,
           e.summary AS esummary, e.keywords AS ekw, e.tickers AS etick, e.companies AS ecomp,
           e.created_at AS ecreated,
           (SELECT count(*) FROM public.journal_entry_link l
             WHERE (l.from_entry_id = e.id OR l.to_entry_id = e.id)
               AND EXISTS (SELECT 1 FROM public.journal_entry o
                            WHERE o.id = CASE WHEN l.from_entry_id = e.id THEN l.to_entry_id ELSE l.from_entry_id END
                              AND o.deleted_at IS NULL))::int AS lcount,
           (SELECT array_agg(DISTINCT l.relation) FROM public.journal_entry_link l
             WHERE l.from_entry_id = e.id OR l.to_entry_id = e.id) AS lrels,
           (SELECT count(*) FROM public.journal_thesis t WHERE t.entry_id = e.id)::int AS ttotal,
           (SELECT count(*) FROM public.journal_thesis t WHERE t.entry_id = e.id AND t.status = 'open')::int AS topen,
           (SELECT count(*) FROM public.journal_thesis t
             WHERE t.entry_id = e.id AND t.status = 'open'
               AND t.review_at IS NOT NULL AND t.review_at <= public.journal_today())::int AS tdue
    FROM public.journal_entry e
    WHERE e.agent_id = p_agent_id
      AND e.deleted_at IS NULL
      AND e.entry_date >= public.journal_today() - GREATEST(COALESCE(p_days, 3), 0)
  ), win AS (
    SELECT * FROM base ORDER BY edate DESC, ecreated DESC LIMIT GREATEST(COALESCE(p_limit, 20), 1)
  )
  SELECT w.id, w.edate, w.rdate, w.etype, w.esummary, w.ekw, w.etick, w.ecomp,
         w.lcount, w.lrels, w.ttotal, w.topen, w.tdue,
         (SELECT max(e2.entry_date) FROM public.journal_entry e2
           WHERE e2.agent_id = p_agent_id AND e2.deleted_at IS NULL),
         (SELECT min(e3.reminder_date) FROM public.journal_entry e3
           WHERE e3.agent_id = p_agent_id AND e3.deleted_at IS NULL
             AND e3.reminder_date IS NOT NULL AND e3.reminder_date >= public.journal_today())
  FROM win w
  ORDER BY w.edate ASC, w.ecreated ASC;
$fn$;

-- ---------------------------------------------------------------------------
-- 6) journal_reminders — faellige und ueberfaellige Wiedervorlagen
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.journal_reminders(
  p_agent_id text,
  p_due_within_days int DEFAULT 3,
  p_limit int DEFAULT 10
) RETURNS TABLE (
  entry_id uuid,
  entry_date date,
  reminder_date date,
  days_delta int,
  entry_type text,
  summary text,
  thesis_open int,
  link_count int
)
LANGUAGE sql STABLE AS $fn$
  SELECT e.id, e.entry_date, e.reminder_date,
         (e.reminder_date - public.journal_today())::int,
         e.entry_type, e.summary,
         (SELECT count(*) FROM public.journal_thesis t WHERE t.entry_id = e.id AND t.status = 'open')::int,
         (SELECT count(*) FROM public.journal_entry_link l WHERE l.from_entry_id = e.id OR l.to_entry_id = e.id)::int
  FROM public.journal_entry e
  WHERE e.agent_id = p_agent_id
    AND e.deleted_at IS NULL
    AND e.reminder_date IS NOT NULL
    AND e.reminder_date <= public.journal_today() + GREATEST(COALESCE(p_due_within_days, 3), 0)
  ORDER BY e.reminder_date ASC, e.entry_date ASC
  LIMIT GREATEST(COALESCE(p_limit, 10), 1);
$fn$;

-- ---------------------------------------------------------------------------
-- 7) hybrid_search_journal — Vektor + Keyword (RRF), Ticker/Zeitraum-Filter
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.hybrid_search_journal(
  p_query_embedding extensions.vector DEFAULT NULL,
  p_query_text text DEFAULT NULL,
  p_agent_id text DEFAULT NULL,
  p_match_threshold double precision DEFAULT 0.0,
  p_match_count integer DEFAULT 10,
  p_ticker text DEFAULT NULL,
  p_from date DEFAULT NULL,
  p_to date DEFAULT NULL,
  p_full_text boolean DEFAULT false
) RETURNS TABLE (
  entry_id uuid,
  entry_date date,
  reminder_date date,
  entry_type text,
  summary text,
  keywords text[],
  tickers text[],
  companies text[],
  body text,
  similarity double precision,
  keyword_rank real,
  score double precision
)
LANGUAGE plpgsql STABLE
SET search_path TO 'public', 'extensions'
AS $fn$
DECLARE
  v_q text := NULLIF(btrim(COALESCE(p_query_text, '')), '');
BEGIN
  RETURN QUERY
  WITH vec AS (
    SELECT e.id, 1 - (e.embedding <=> p_query_embedding) AS similarity,
           row_number() OVER (ORDER BY e.embedding <=> p_query_embedding) AS rnk
    FROM public.journal_entry e
    WHERE p_query_embedding IS NOT NULL
      AND e.embedding IS NOT NULL
      AND e.deleted_at IS NULL
      AND (p_agent_id IS NULL OR e.agent_id = p_agent_id)
      AND (p_ticker IS NULL OR p_ticker = ANY(e.tickers) OR p_ticker = ANY(e.companies))
      AND (p_from IS NULL OR e.entry_date >= p_from OR (e.reminder_date IS NOT NULL AND e.reminder_date >= p_from))
      AND (p_to IS NULL OR e.entry_date <= p_to OR (e.reminder_date IS NOT NULL AND e.reminder_date <= p_to))
      AND (1 - (e.embedding <=> p_query_embedding)) >= p_match_threshold
    ORDER BY e.embedding <=> p_query_embedding
    LIMIT 200
  ), kw AS (
    SELECT e.id, row_number() OVER (ORDER BY e.entry_date DESC, e.created_at DESC) AS rnk
    FROM public.journal_entry e
    WHERE v_q IS NOT NULL
      AND e.deleted_at IS NULL
      AND (p_agent_id IS NULL OR e.agent_id = p_agent_id)
      AND (p_ticker IS NULL OR p_ticker = ANY(e.tickers) OR p_ticker = ANY(e.companies))
      AND (p_from IS NULL OR e.entry_date >= p_from OR (e.reminder_date IS NOT NULL AND e.reminder_date >= p_from))
      AND (p_to IS NULL OR e.entry_date <= p_to OR (e.reminder_date IS NOT NULL AND e.reminder_date <= p_to))
      AND (
        e.keywords @> ARRAY[v_q]
        OR e.tickers @> ARRAY[v_q]
        OR e.companies @> ARRAY[v_q]
        OR EXISTS (SELECT 1 FROM unnest(e.keywords) k WHERE k ILIKE v_q)
        OR EXISTS (SELECT 1 FROM unnest(e.companies) c WHERE c ILIKE v_q)
        OR e.summary ILIKE '%' || v_q || '%'
        OR e.body ILIKE '%' || v_q || '%'
      )
    ORDER BY e.entry_date DESC, e.created_at DESC
    LIMIT 200
  ), fused AS (
    SELECT COALESCE(v.id, k.id) AS id,
           (COALESCE(1.0 / (60.0 + v.rnk), 0) + COALESCE(1.0 / (60.0 + k.rnk), 0))::double precision AS score,
           COALESCE(v.similarity, 0)::double precision AS similarity,
           COALESCE(k.rnk, 0)::real AS keyword_rank
    FROM vec v FULL OUTER JOIN kw k ON v.id = k.id
  )
  SELECT e.id, e.entry_date, e.reminder_date, e.entry_type, e.summary, e.keywords, e.tickers, e.companies,
         CASE WHEN p_full_text THEN e.body ELSE NULL END,
         f.similarity, f.keyword_rank, f.score
  FROM fused f
  JOIN public.journal_entry e ON e.id = f.id
  ORDER BY f.score DESC, e.entry_date DESC
  LIMIT GREATEST(COALESCE(p_match_count, 10), 1);
END;
$fn$;

-- ---------------------------------------------------------------------------
-- 8) journal_theses — offene/faellige Thesen
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.journal_theses(
  p_agent_id text,
  p_status text DEFAULT 'open',
  p_due_only boolean DEFAULT false,
  p_ticker text DEFAULT NULL,
  p_limit int DEFAULT 50
) RETURNS TABLE (
  thesis_id uuid,
  entry_id uuid,
  entry_date date,
  reminder_date date,
  statement text,
  tickers text[],
  companies text[],
  horizon_days int,
  falsifier text,
  confidence smallint,
  status text,
  review_at date,
  outcome_note text,
  entry_summary text
)
LANGUAGE sql STABLE AS $fn$
  SELECT t.id, e.id, e.entry_date, e.reminder_date, t.statement, t.tickers, t.companies,
         t.horizon_days, t.falsifier, t.confidence, t.status, t.review_at, t.outcome_note, e.summary
  FROM public.journal_thesis t
  JOIN public.journal_entry e ON e.id = t.entry_id
  WHERE e.agent_id = p_agent_id
    AND e.deleted_at IS NULL
    AND (p_status IS NULL OR btrim(p_status) = '' OR p_status = 'all' OR t.status = p_status)
    AND (NOT COALESCE(p_due_only, false)
         OR (t.review_at IS NOT NULL AND t.review_at <= public.journal_today()))
    AND (p_ticker IS NULL OR p_ticker = ANY(t.tickers) OR p_ticker = ANY(t.companies))
  ORDER BY (t.review_at IS NULL), t.review_at ASC, e.entry_date DESC
  LIMIT GREATEST(COALESCE(p_limit, 50), 1);
$fn$;

-- ---------------------------------------------------------------------------
-- 9) journal_update — Thesen bewerten, Wiedervorlage/Metadaten korrigieren
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.journal_update(
  p_entry text DEFAULT NULL,
  p_thesis text DEFAULT NULL,
  p_entry_patch jsonb DEFAULT '{}'::jsonb,
  p_thesis_patch jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
  v_entry uuid;
  v_thesis uuid;
  v_n_entry int := 0;
  v_n_thesis int := 0;
  v_patch jsonb := COALESCE(p_entry_patch, '{}'::jsonb);
  v_tpatch jsonb := COALESCE(p_thesis_patch, '{}'::jsonb);
BEGIN
  IF p_entry IS NOT NULL THEN
    v_entry := public.journal_resolve_id(p_entry);
    UPDATE public.journal_entry e SET
      summary       = CASE WHEN v_patch ? 'summary'   THEN NULLIF(btrim(COALESCE(v_patch->>'summary', '')), '') ELSE e.summary END,
      keywords      = CASE WHEN v_patch ? 'keywords'  THEN COALESCE(ARRAY(SELECT jsonb_array_elements_text(v_patch->'keywords')), '{}') ELSE e.keywords END,
      tickers       = CASE WHEN v_patch ? 'tickers'   THEN COALESCE(ARRAY(SELECT jsonb_array_elements_text(v_patch->'tickers')), '{}') ELSE e.tickers END,
      companies     = CASE WHEN v_patch ? 'companies' THEN COALESCE(ARRAY(SELECT jsonb_array_elements_text(v_patch->'companies')), '{}') ELSE e.companies END,
      themes        = CASE WHEN v_patch ? 'themes'    THEN COALESCE(ARRAY(SELECT jsonb_array_elements_text(v_patch->'themes')), '{}') ELSE e.themes END,
      entry_date    = CASE WHEN v_patch ? 'entry_date'    THEN (v_patch->>'entry_date')::date ELSE e.entry_date END,
      reminder_date = CASE WHEN v_patch ? 'reminder_date' THEN NULLIF(v_patch->>'reminder_date', '')::date ELSE e.reminder_date END,
      updated_at    = now()
    WHERE e.id = v_entry;
    GET DIAGNOSTICS v_n_entry = ROW_COUNT;
  END IF;

  IF p_thesis IS NOT NULL THEN
    v_thesis := public.journal_resolve_id(p_thesis);
    UPDATE public.journal_thesis t SET
      status       = CASE WHEN v_tpatch ? 'status'       THEN v_tpatch->>'status' ELSE t.status END,
      outcome_note = CASE WHEN v_tpatch ? 'outcome_note' THEN NULLIF(btrim(COALESCE(v_tpatch->>'outcome_note', '')), '') ELSE t.outcome_note END,
      confidence   = CASE WHEN v_tpatch ? 'confidence'   THEN NULLIF(v_tpatch->>'confidence', '')::smallint ELSE t.confidence END,
      review_at    = CASE WHEN v_tpatch ? 'review_at'    THEN NULLIF(v_tpatch->>'review_at', '')::date ELSE t.review_at END,
      horizon_days = CASE WHEN v_tpatch ? 'horizon_days' THEN NULLIF(v_tpatch->>'horizon_days', '')::int ELSE t.horizon_days END,
      resolved_at  = CASE WHEN v_tpatch ? 'status' AND v_tpatch->>'status' IN ('confirmed','invalidated','expired') THEN now()
                          WHEN v_tpatch ? 'status' THEN NULL
                          ELSE t.resolved_at END
    WHERE t.id = v_thesis;
    GET DIAGNOSTICS v_n_thesis = ROW_COUNT;
  END IF;

  IF v_n_entry = 0 AND v_n_thesis = 0 THEN
    RAISE EXCEPTION 'journal: nichts aktualisiert (entry/thesis fehlt oder keine Patch-Felder)';
  END IF;

  RETURN jsonb_build_object('entry_updated', v_n_entry, 'thesis_updated', v_n_thesis);
END;
$fn$;

-- ---------------------------------------------------------------------------
-- 10) journal_delete — soft (Default) oder hard
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.journal_delete(
  p_id text,
  p_hard boolean DEFAULT false
) RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
  v_id uuid;
  v_theses int := 0;
  v_links int := 0;
  v_summary text;
  v_entry_date date;
BEGIN
  v_id := public.journal_resolve_id(p_id, true);

  SELECT e.summary, e.entry_date INTO v_summary, v_entry_date
  FROM public.journal_entry e WHERE e.id = v_id;

  SELECT count(*) INTO v_theses FROM public.journal_thesis t WHERE t.entry_id = v_id;
  SELECT count(*) INTO v_links FROM public.journal_entry_link l
   WHERE l.from_entry_id = v_id OR l.to_entry_id = v_id;

  IF p_hard THEN
    DELETE FROM public.journal_entry e WHERE e.id = v_id;
    RETURN jsonb_build_object('deleted', true, 'hard', true, 'entry_id', v_id,
                              'entry_date', v_entry_date, 'summary', v_summary,
                              'theses_removed', v_theses, 'links_removed', v_links);
  END IF;

  UPDATE public.journal_entry e SET deleted_at = now(), updated_at = now() WHERE e.id = v_id;

  RETURN jsonb_build_object('deleted', true, 'hard', false, 'entry_id', v_id,
                            'entry_date', v_entry_date, 'summary', v_summary,
                            'theses_hidden', v_theses, 'links_kept', v_links);
END;
$fn$;

COMMIT;

NOTIFY pgrst, 'reload schema';
