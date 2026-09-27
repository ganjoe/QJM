# Trader-Journal MCP — Implementierungsplan

Stand: 2026-09-25 · Autor: DSH (Trader-Preset) · Status: **umgesetzt und abgenommen (Rev. 3)**

---

## 0. Auftrag, Entscheidungen und Erfolgskriterien

**Auftrag.** Ein Trader-Tagebuch als eigener MCP-Server. Der Nutzer diktiert eine Marktmeinung im
Fließtext; der Eintrag wird **wörtlich und am Stück** gespeichert, zusätzlich erzeugt ein LLM eine
Zusammenfassung, Keywords und **mehrere prüfbare Thesen**, und der Eintrag wird per Embedding
durchsuchbar. Einträge lassen sich **über UUID verknüpfen** (Nachtrag zum Original, bis hin zu
„widerlegt"), können **gelöscht** werden und tragen ein **Sekundärdatum**: liegt es in der Zukunft,
ist es eine **Wiedervorlage**, die der Agent von selbst anspricht. Die Tool-Beschreibungen erklären
dem Agenten das Tagebuch und bringen ihn dazu, sich die letzten Tage durchzulesen, zu erinnern und
aktuelle Aussagen daran zu spiegeln.

**Entscheidungen des Nutzers (verbindlich):**

| # | Frage | Entscheidung |
|---|---|---|
| 1 | Eigener Server oder in openbrain-cco? | **Eigener Server** openbrain-journal |
| 2 | Eine oder mehrere Thesen pro Eintrag? | **Mehrere Thesen** im LLM-Destillat; **der Originalbeitrag bleibt ein Eintrag am Stück** |
| 3 | Proaktives Schreiben durch den Agenten? | **Nein** — proaktiv ist nur Open Brain; das Journal wird nur auf Zuruf/Diktat geschrieben |
| 4 | PTA-Verknüpfung (trade_id), open_brain-Migration | **Nein**, beides nicht |
| 5 | Automatische Quellen (Breadth, Positionen, Trades) | **Nein**, nur manuelle Einträge |
| 6 | Verknüpfung von Einträgen | **Ja, über UUID**; Nachtrag hängt am Original und kann dessen Thesen als widerlegt markieren |
| 7 | Löschen | **Ja** — Standard ist Soft-Delete; Hard-Delete nur auf ausdrücklichen Wunsch |
| 8 | Sekundärdatum | **Ja**: reminder_date. Zukunft = Wiedervorlage, Vergangenheit = historische Referenz. Kein Push — der Agent bringt es beim Lesen zur Sprache |

**Erfolgskriterien.**

1. Ein MCP-Server **openbrain-journal** läuft aus QJM heraus (Container **llm-gw-mcp-journal**, Port **8801**).
2. Die Tools erscheinen in einer neuen DSH-Session als **mcp__openbrain-journal__***.
3. Ein diktierter Beitrag landet **byte-identisch** in **journal_entry.body** (SHA-256-Nachweis in der Antwort),
   mit **entry_date** (Europe/Berlin), **summary**, **keywords[]**, **tickers[]**, **companies[]**, **themes[]**
   und **embedding vector(1024)**.
4. Aus dem Beispieltext „Meta Muse / Cybersecurity / Agent-APIs / Mittelsmann / Google-Werbung" entstehen
   **mehrere Thesen** (journal_thesis), jede mit Ticker-/Firmenbezug, Horizont, Confidence und Falsifikationskriterium.
5. **journal_recent(days: 3)** liefert die Einträge chronologisch und kompakt; **journal_get** liefert den
   Originaltext am Stück.
6. **journal_capture** liefert in der Antwort **verwandte frühere Einträge** zurück (Erinnern beim Schreiben).
7. Ein Nachtrag („der Dieselbedarf ist schneller gesunken als das Angebot") lässt sich mit
   **relation = invalidates** an die UUID des Originaleintrags hängen; danach sind dessen Thesen
   **invalidated** und der Originaleintrag zeigt den Nachtrag an.
8. **journal_recent** zeigt fällige Wiedervorlagen mit an, auch wenn der Eintrag älter ist;
   **journal_delete** entfernt Einträge (soft per Default).
9. Scheitert LLM oder Embedding, ist der Eintrag **trotzdem vollständig gespeichert**.
10. Die Workitem-Rollen (cco, lead, pca, pta, cda, drawio) sehen das Journal **nicht**.

---

## 1. Ist-Zustand (verifiziert)

**Warum capture_thought / open_brain dafür nicht reicht** — belegt im Code:

| Befund | Fundstelle |
|---|---|
| extractMetadata() liefert keywords, tickers, topics — gespeichert wird nur thought_type. Der Rest wird verworfen. | mcp/agent-cco/tools/openbrain_tools.ts:60-68 |
| Kein Eintragsdatum, kein Backdating, **kein Datumsfilter in der Suche** (nur query/limit/threshold). | mcp/agent-cco/tools/openbrain_tools.ts:19-30 |
| Embedding des **Volltexts**, hart auf 1100 Zeichen gekürzt (EMBED_MAX_CHARS, fitForEmbedding). | mcp/agent-cco/tools/shared.ts:512,538,677 |
| Dedupe über **global unique** content_hash: derselbe Gedanke an einem anderen Tag erzeugt keinen neuen Eintrag. | /home/daniel/openBrain/init/01-schema.sql:72,189-198 |
| Typen nur observation/task/idea/reference; kein Status, kein Horizont, kein Outcome, keine Verknüpfung, kein Wiedervorlagedatum. | mcp/agent-cco/prompts/metadata-prompt.txt |
| GLOBAL_BRAIN_ACCESS=false, AGENT_ID=cco — Einträge hängen am CCO-Agenten. | llm-gateway/docker-compose.yml:87-88 |

**Vorhandene Bausteine, die wiederverwendet werden** (nicht neu erfinden):

* MCP-Server-Muster: mcp/agent-wiq/ (Deno, MCP-SDK 1.24.3, Hono, StreamableHTTPTransport, x-brain-key-Auth, /health).
* Embeddings: mxbai-embed-large, **1024 Dim**, EMBED_MAX_CHARS=1100, EMBED_VERSION=mxbai-v1, Query-Prefix nur bei Suchanfragen. Über Switchyard (getEmbedding / getQueryEmbedding).
* LLM: DeepSeek direkt (deepseekChatCompletion, Retry/Backoff, LlmUnavailableError).
* Vektor-RPCs brauchen SET search_path TO 'public','extensions' — sonst der Fehler „operator does not exist: extensions.vector <=> extensions.vector" (teuer gelernt, Migration 018).
* Hybrid-Suche als RRF-Fusion aus Vektor- und Keyword-CTE (Migration 013).
* Migrationen werden so eingespielt: docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 < migrations/NNN_*.sql (Kopf von 033).

**Nebenbefund (nicht Teil dieses Auftrags):** Der Kopf von agent-presets/trader/agent.cordis.yml dokumentiert
eine **trader-mcp-filter**-Zeile mit config.allow; eine solche Zeile existiert in der Datei **nicht**. Der
Trader-Agent sieht derzeit also alle global registrierten MCP-Tools. Für dieses Vorhaben ist das günstig
(der neue Server ist automatisch sichtbar); die veraltete Dokumentation sollte separat korrigiert werden.

---

## 2. Zielbild

    Trader-Agent (DSH, Preset "trader")
      │  MCP streamable-http, x-brain-key, Port 8801
      ▼
    openbrain-journal  (QJM/mcp/agent-journal, Container llm-gw-mcp-journal)
      │  DeepSeek       → Zusammenfassung, Keywords, Ticker/Firmen, Thesen, Wiedervorlagedatum
      │  Switchyard/mxbai → Embedding (1024) über summary+keywords+tickers
      ▼
    Supabase/Postgres:
      journal_entry (Original, append-only)  1 ── n  journal_thesis (prüfbar)
             ▲                                          ▲
             └── journal_entry_link (from → to, relation) ┘  (Verknüpfung per UUID)

Abgrenzung, die in **jeder** Tool-Beschreibung stehen muss:

* **Journal** = *meine eigene* Meinung, These, Beobachtung, Lektion — Tagebuch, chronologisch, dauerhaft.
* **Open Brain** (capture_thought) = fremde Posts, Rechercheergebnisse, Rohnotizen — unverändert.
* **PTA** = Ausführung und Portfolio. Das Journal verlinkt **nicht** auf Trades (Entscheidung 4).

---

## 3. Datenmodell — migrations/034_trader_journal.sql

Grundsätze: **Original ist unantastbar** (body wird nie überschrieben), **Thesen sind das prüfbare Destillat**,
**Verknüpfungen laufen ausschließlich über UUIDs und ausschließlich über eine Link-Tabelle**,
**kein globaler Dedupe-Zwang**, **Zeit in UTC, Datumsfelder in Europe/Berlin**.

    BEGIN;

    CREATE TABLE IF NOT EXISTS public.journal_entry (
      id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      agent_id           text NOT NULL DEFAULT 'journal',
      entry_date         date NOT NULL DEFAULT ((now() AT TIME ZONE 'Europe/Berlin')::date),
      reminder_date      date,                            -- Sekundärdatum/Wiedervorlage; Zukunft = Erinnerung
      created_at         timestamptz NOT NULL DEFAULT now(),
      updated_at         timestamptz NOT NULL DEFAULT now(),
      body               text NOT NULL,                   -- Originaltext, byte-identisch, append-only
      body_sha256        text NOT NULL,                   -- Nachweis der Unverfälschtheit
      summary            text,                            -- LLM, 1-3 Sätze
      keywords           text[] NOT NULL DEFAULT '{}',    -- LLM, 3-8
      tickers            text[] NOT NULL DEFAULT '{}',    -- nur explizit genannte Symbole
      companies          text[] NOT NULL DEFAULT '{}',    -- Meta, Google, ... (Firmen ohne Symbol im Text)
      themes             text[] NOT NULL DEFAULT '{}',    -- 1-3 Themen-Tags
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

    CREATE TABLE IF NOT EXISTS public.journal_thesis (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      entry_id      uuid NOT NULL REFERENCES public.journal_entry(id) ON DELETE CASCADE,
      statement     text NOT NULL,                        -- ein Satz, prüfbar formuliert
      tickers       text[] NOT NULL DEFAULT '{}',
      companies     text[] NOT NULL DEFAULT '{}',
      horizon_days  integer,                              -- Erwartungshorizont
      falsifier     text,                                 -- was müsste passieren, damit sie falsch ist
      confidence    smallint CHECK (confidence BETWEEN 1 AND 5),
      status        text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','confirmed','invalidated','expired')),
      review_at     date,                                 -- These neu bewerten (aus horizon_days)
      outcome_note  text,
      resolved_at   timestamptz,
      embedding     extensions.vector(1024),
      created_at    timestamptz NOT NULL DEFAULT now()
    );

    -- Verknüpfung: from_entry_id ist der Nachtrag, to_entry_id das Original.
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

    CREATE INDEX IF NOT EXISTS idx_journal_entry_date     ON public.journal_entry (agent_id, entry_date DESC, created_at DESC)
      WHERE deleted_at IS NULL;
    CREATE INDEX IF NOT EXISTS idx_journal_entry_reminder ON public.journal_entry (agent_id, reminder_date)
      WHERE deleted_at IS NULL AND reminder_date IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_journal_entry_kw       ON public.journal_entry USING gin (keywords);
    CREATE INDEX IF NOT EXISTS idx_journal_entry_tick     ON public.journal_entry USING gin (tickers);
    CREATE INDEX IF NOT EXISTS idx_journal_entry_comp     ON public.journal_entry USING gin (companies);
    CREATE INDEX IF NOT EXISTS idx_journal_entry_dedupe   ON public.journal_entry (dedupe_hash, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_journal_thesis_open    ON public.journal_thesis (status, review_at);
    CREATE INDEX IF NOT EXISTS idx_journal_thesis_tick    ON public.journal_thesis USING gin (tickers);
    CREATE INDEX IF NOT EXISTS idx_journal_link_from      ON public.journal_entry_link (from_entry_id);
    CREATE INDEX IF NOT EXISTS idx_journal_link_to        ON public.journal_entry_link (to_entry_id);

    CREATE INDEX IF NOT EXISTS idx_journal_entry_hnsw
      ON public.journal_entry USING hnsw (embedding extensions.vector_cosine_ops)
      WITH (m = 16, ef_construction = 64);
    CREATE INDEX IF NOT EXISTS idx_journal_thesis_hnsw
      ON public.journal_thesis USING hnsw (embedding extensions.vector_cosine_ops)
      WITH (m = 16, ef_construction = 64);

    -- Grants wie im Repo üblich (PostgREST greift als anon/service_role zu).
    GRANT SELECT, INSERT, UPDATE, DELETE ON public.journal_entry      TO anon, service_role;
    GRANT SELECT, INSERT, UPDATE, DELETE ON public.journal_thesis     TO anon, service_role;
    GRANT SELECT, INSERT, UPDATE, DELETE ON public.journal_entry_link TO anon, service_role;

    COMMIT;
    NOTIFY pgrst, 'reload schema';

**Bewusste Entscheidungen im Modell:**

* **Kein parent_id.** Die frühere Fassung hatte eine Selbstreferenz; jetzt gibt es **genau einen**
  Verknüpfungsmechanismus (journal_entry_link). Er kann 1:1 (Nachtrag) und n:m (mehrere Nachträge,
  mehrere Originale) und trägt die Beziehung als Wert.
* **Verknüpfungen sind gerichtet**: from = der neue Nachtrag, to = das Original. Angezeigt wird immer
  in beide Richtungen („Nachtrag vom …" am Original, „gehört zu …" am Nachtrag).
* **Beziehungen:** follow_up (neutraler Nachtrag), confirms (bestätigt), invalidates (widerlegt),
  related (sonstiger Bezug).
* **reminder_date ist ein reines Datum.** Zukunft/Heute = Wiedervorlage, Vergangenheit = historische
  Referenz. Es gibt keine Flags — die Semantik entsteht beim Lesen aus dem Vergleich mit heute.
* **Zwei Datumsbegriffe, klar getrennt:** journal_entry.reminder_date = „an diesen Eintrag erinnern"
  (Ereignis: Earnings, Datentermin, Entscheidung), journal_thesis.review_at = „diese These neu bewerten"
  (wird aus horizon_days berechnet).

**Bewusst nicht enthalten:** RLS (wie open_brain), PTA-Fremdschlüssel, open_brain-Verweise, Auto-Quellen.

---

## 4. RPCs (gleiche Migration)

Alle Vektor-RPCs **mit** SET search_path TO 'public','extensions'. Alle RPCs akzeptieren IDs als
vollständige UUID **oder als eindeutigen Präfix** (Auflösung serverseitig, siehe 4.6).

### 4.1 journal_capture(p_agent_id text, p_body text, p_entry_date date, p_reminder_date date, p_links jsonb, p_dedupe_window_hours int) RETURNS jsonb

* dedupe_hash analog upsert_open_brain: sha256(lower(trim(regexp_replace(body, '[[:space:]]+', ' ', 'g')))).
* Treffer im Zeitfenster → **kein Insert**, Rückgabe {entry_id: <bestehend>, duplicate_of: true}.
* sonst Insert (body, body_sha256, entry_date, reminder_date) → {entry_id, duplicate_of: false}.
* legt die Verknüpfungen aus p_links an, Format:
  [{ "to": "<uuid oder praefix>", "relation": "invalidates", "note": "…", "theses": ["<uuid>", …] }]
  (theses optional — ohne Angabe gelten **alle offenen** Thesen des Zieleintrags).
* **deterministische Folge der Relation** (das ist die gewünschte Klarheit, kein Autonomes Schreiben):
  * invalidates → Thesen des Ziels: status = invalidated, resolved_at = now(), outcome_note = note.
  * confirms    → Thesen des Ziels: status = confirmed,   resolved_at = now(), outcome_note = note.
  * follow_up / related → keine Statusänderung.
* Rückgabe zusätzlich: {link_ids, theses_touched}.

### 4.2 journal_link(p_action text, p_from text, p_to text, p_relation text, p_note text, p_theses uuid[]) RETURNS jsonb

* p_action = 'add' | 'remove'. add ist idempotent (UNIQUE-Constraint, ON CONFLICT DO NOTHING).
* Bei add mit relation invalidates/confirms gilt dieselbe deterministische Statusfolge wie in 4.1.
* Bei remove wird eine zuvor gesetzte Statusänderung **nicht** zurückgenommen — der Nutzer kann den
  Thesenstatus mit journal_update explizit korrigieren. (Begründung: Status ist eine Aussage des Nutzers,
  die Verknüpfung nur ihr Auslöser.)

### 4.3 journal_recent(p_agent_id text, p_days int, p_limit int) RETURNS TABLE(...)

* liefert die neuesten p_limit Einträge mit entry_date >= (heute Berlin - p_days), **aufsteigend**
  sortiert (alt → neu), ohne body, mit: entry_id, entry_date, reminder_date, entry_type, summary,
  keywords, tickers, companies, link_count, link_relations, thesis_total, thesis_open, thesis_due.
* Zusatzwerte last_entry_date (auch außerhalb des Fensters) und next_reminder_date.

### 4.4 journal_reminders(p_agent_id text, p_due_within_days int, p_limit int) RETURNS TABLE(...)

* Einträge mit reminder_date IS NOT NULL, nicht gelöscht, und
  reminder_date <= (heute Berlin + p_due_within_days) — **inklusive überfälliger**, aufsteigend nach
  reminder_date (dringendstes zuerst), mit Tagen Differenz, summary und Thesenstatus des Eintrags.
* Der Agent ruft das nicht separat auf: journal_recent ruft es intern und zeigt den Block
  „Wiedervorlagen" mit an. Der RPC existiert getrennt, damit die Abfrage testbar und der Tool-Output
  formatierbar bleibt.

### 4.5 hybrid_search_journal(p_query_embedding extensions.vector, p_query_text text, p_agent_id text, p_match_threshold double precision, p_match_count int, p_ticker text, p_from date, p_to date, p_full_text boolean) RETURNS TABLE(...)

RRF-Fusion nach dem Muster von hybrid_search_workspace (Migration 013): Vektor-CTE (embedding <=> query) plus
Keyword-CTE (keywords @> to_jsonb(q), tickers/companies, summary ILIKE, body ILIKE), kombiniert über 1/(60+rank).
Filter p_ticker prüft tickers **und** companies; p_from/p_to prüfen entry_date **und** reminder_date.
Rückgabe enthält reminder_date und link_count. body nur bei p_full_text = true.

### 4.6 Hilfsfunktionen

* journal_resolve_id(p_id text) RETURNS uuid — löst vollständige UUID oder eindeutigen Präfix (min. 6 Zeichen)
  gegen nicht gelöschte Einträge auf; bei Mehrdeutigkeit **Fehler** mit den Kandidaten, kein Raten.
* journal_delete(p_id text, p_hard boolean DEFAULT false) RETURNS jsonb
  * soft (Default): deleted_at = now() auf dem Eintrag. Thesen und Links bleiben für die Nachvollziehbarkeit
    in der DB, verschwinden aber aus allen Lese-RPCs (die joinen auf deleted_at IS NULL). Rückgabe mit
    Anzahl betroffener Thesen/Links.
  * hard (nur auf ausdrücklichen Wunsch): DELETE. Links kaskadieren (ON DELETE CASCADE); fremde Einträge
    bleiben bestehen, ihre Links auf den gelöschten Eintrag verschwinden.
* journal_theses(p_agent_id text, p_status text, p_due_only boolean, p_ticker text, p_limit int) RETURNS TABLE(...)
  — Thesen mit Eintragsdatum, Statement, Horizont, Falsifier, Confidence, Status, Link-Kontext;
  p_due_only filtert review_at <= heute.

---

## 5. LLM-Pipeline, Prompt-Vertrag, Fehlersemantik

**Ablauf bei journal_capture (Reihenfolge ist Absicht):**

1. **Sofort Insert** über journal_capture-RPC (Roh-Text und Verknüpfungen gerettet — auch wenn das LLM
   danach ausfällt, hängt der Nachtrag bereits am Original).
2. DeepSeek-Call mit prompts/journal-prompt.txt → JSON-Destillat (inkl. reminder_date).
3. Embedding über summary + keywords + tickers + companies (nicht den Volltext). Bei LLM-Fehler: Embedding
   des Roh-Texts (fitForEmbedding, 1100 Zeichen), damit der Eintrag auffindbar bleibt.
4. journal_enrich-RPC (Felder + Thesen atomar); ist im Destillat kein reminder_date enthalten und war
   auch keines explizit übergeben, bleibt das Feld NULL.
5. **Verwandte Einträge**: hybrid_search_journal mit demselben Vektor, Selbstausschluss, Schwelle 0.55, max. 3.
   Bei Kurztexten (< 40 Zeichen) entfällt Schritt 5.
6. Antwort an den Agenten: Datum, Kurzsummary, Thesen, Keywords/Ticker, **verwandte frühere Einträge mit
   UUID** und der Hinweis, dass ein Nachtrag mit journal_capture(links: …) angehängt werden kann.

**Prompt-Vertrag prompts/journal-prompt.txt (nur JSON):**

    {
      "summary": "1-3 Sätze, deutsch, keine Bewertung erfinden",
      "keywords": ["3-8 Begriffe"],
      "tickers": ["nur Symbole, die wörtlich im Text stehen"],
      "companies": ["Firmennamen ohne Symbol im Text, z.B. Meta, Google"],
      "themes": ["1-3 Themen-Tags"],
      "entry_type": "thesis|observation|trade_note|review|lesson|macro",
      "reminder_date": "YYYY-MM-DD oder null",
      "theses": [
        {
          "statement": "ein prüfbarer Satz",
          "tickers": [], "companies": [],
          "horizon_days": 90,
          "falsifier": "was müsste passieren, damit die These falsch ist",
          "confidence": 1
        }
      ]
    }

Regeln im Prompt: deutsch; **max. 5 Thesen**, jede mit Horizont und Falsifier; die Ticker-Regeln aus
mcp/agent-cco/prompts/metadata-prompt.txt werden **wörtlich übernommen** (keine Kurse, Beträge oder
Jahreszahlen als Ticker; Cashtags stärkstes Signal; Firmennamen gehören nach companies/Keywords).

**Regeln für reminder_date (explizit oder implizit):**

* nur setzen, wenn der Text auf ein **künftiges** Ereignis oder einen Termin verweist, den der Nutzer
  abwarten will („ich warte die Earnings am 2.12.26 ab", „nach dem FOMC am 17.09.").
* relative Angaben auflösen, wenn das Bezugsdatum eindeutig ist („in zwei Wochen", „nach dem
  Quartalsbericht" nur mit genanntem Datum); sonst null.
* **kein erfundenes Datum.** Ohne eindeutigen Bezug bleibt das Feld null.
* ein **explizit** im Tool-Aufruf übergebenes reminder_date gewinnt immer gegen das LLM-Feld.
* Vergangenheitsdaten sind erlaubt (historische Referenz) und werden nicht als Wiedervorlage gelistet.

**Fehlersemantik (harte Regel):** Ein Tagebucheintrag geht **nie** durch einen API-Fehler verloren.
LLM-Fehler → Eintrag bleibt, extraction_failed = true, extraction_error gesetzt, die Antwort sagt es klar.
Nachträge laufen über scripts/backfill_journal.ts (extraction_failed = true oder embedding IS NULL),
nicht über ein weiteres Tool.

---

## 6. Tool-Surface (8 Tools) mit Beschreibungstexten

**Sprache: Deutsch.** Der Nutzer diktiert deutsch, und die Beschreibung ist hier Verhaltenssteuerung,
nicht API-Doku. Jede Beschreibung folgt dem Muster der WiQ-Tools: Zweck → WHEN TO USE → WHEN NOT TO USE.
**Jede Ausgabe, die Einträge listet, zeigt die UUID (Kurzform, erste 8 Zeichen); alle ID-Parameter
akzeptieren volle UUID oder eindeutigen Präfix.**

### 6.1 journal_capture (write)

> **Trader-Tagebuch — Eintrag schreiben.** Speichert eine eigene Marktmeinung, These, Beobachtung oder
> Lektion **wörtlich und am Stück** (der Originaltext ist das Tagebuch und wird nie umgeschrieben).
> Zusätzlich erzeugt das System eine Zusammenfassung, Keywords, Ticker/Firmen, **prüfbare Thesen** mit
> Horizont und Falsifikationskriterium, ein Embedding und — falls der Text es hergibt — ein
> **Wiedervorlagedatum**.
> **Verknüpfen:** Über links: [{to: "<uuid>", relation: "follow_up|confirms|invalidates|related", note: "…"}]
> hängt der neue Eintrag an einen bestehenden. Bei relation invalidates werden die Thesen des Originals
> als widerlegt markiert, bei confirms als bestätigt. So wird zwei Monate später sichtbar, dass eine
> Makro-These erledigt ist, ohne den Originaltext anzufassen.
> Die Antwort enthält **verwandte frühere Einträge mit UUID** — nutze sie für Vergleiche oder als Ziel
> einer Verknüpfung.
> **WHEN TO USE:** immer wenn der Nutzer eine *eigene* Einschätzung, These, Prognose, Kausalitätskette,
> ein „ich glaube / ich erwarte / meiner Meinung nach" oder eine Lektion formuliert — auch mitten im
> Gespräch. Ebenfalls, wenn er eine frühere Aussage bestätigt, relativiert oder widerlegt: dann neuen,
> wörtlichen Eintrag **mit Verknüpfung** anlegen statt den alten zu ändern.
> **Wiedervorlage:** Nennt der Nutzer einen künftigen Termin („ich warte die Earnings am 2.12.26 ab"),
> übergib ihn als reminder_date; das System erkennt ihn auch implizit aus dem Text.
> Der Text wird **nicht** umformuliert, nicht in mehrere Einträge zerlegt und nicht gekürzt.
> **WHEN NOT TO USE:** für fremde Meinungen, Posts, Rechercheergebnisse (→ capture_thought bzw.
> openbrain-cco), für Messwerte, Scans, Kursdaten und für Handelsausführungen (→ openbrain-pta).
> **Nicht proaktiv schreiben:** nur auf ausdrücklichen Wunsch oder Diktat des Nutzers.

### 6.2 journal_recent (read) — der „erinnern"-Aufruf

> **Trader-Tagebuch — die letzten Tage lesen, inklusive Wiedervorlagen.** Liefert zwei Blöcke:
> (A) die Einträge der letzten days Tage **chronologisch (alt → neu)** und kompakt — UUID, Datum, Typ,
> Zusammenfassung, Keywords, Ticker/Firmen, Verknüpfungen, Thesen-Status;
> (B) **Wiedervorlagen**: Einträge, deren Sekundärdatum erreicht oder überschritten ist — auch weit
> ältere — mit „⏰ in X Tagen" bzw. „überfällig seit X Tagen".
> Ohne Suchbegriff, ohne Vektorsuche — deterministisch.
> **WHEN TO USE — ZUERST, vor der inhaltlichen Antwort:** zu Beginn jeder Session, in der es um
> Marktmeinungen, Thesen, Positionen, Strategie oder Einschätzungen geht; wenn der Nutzer „was habe ich
> damals gedacht", „erinnerst du dich", „was war letzte Woche", „wie war meine These" sagt; bevor du
> eine neue These kommentierst oder bewertest; wenn eine aktuelle Nachricht zu einem Thema passt, zu dem
> du etwas notiert haben könntest.
> **Pflicht:** Existiert ein früherer Eintrag zum aktuellen Thema, spiegle die neue Aussage daran
> („Am 12.09. war deine These X, heute sagst du Y — was hat sich geändert?") und sage, ob die These
> noch offen, bestätigt oder widerlegt ist. **Fällige Wiedervorlagen sprichst du von selbst an**, auch
> wenn der Nutzer nach etwas anderem fragt — das ist der Ersatz für eine Push-Nachricht. Ohne Treffer:
> sage das offen und behaupte kein Gedächtnis.
> **WHEN NOT TO USE:** für gezielte Themensuche (→ journal_search), für den Originalwortlaut (→ journal_get).
> Der Aufruf ist billig und lesend — Zögern ist teurer als Lesen.

### 6.3 journal_search (read)

> **Trader-Tagebuch — gezielt durchsuchen** (Vektor + Keyword, optional Ticker/Firma und Zeitraum; der
> Zeitraum trifft Erstelldatum **und** Wiedervorlagedatum).
> **WHEN TO USE:** „was habe ich über X gedacht/geschrieben", „meine Notizen zu Cybersecurity",
> „alle Einträge zu Meta in den letzten 6 Monaten".
> **WHEN NOT TO USE:** für „die letzten Tage" ohne Suchbegriff (→ journal_recent).

### 6.4 journal_get (read)

> **Trader-Tagebuch — einen Eintrag im Original lesen.** Gibt den vollständigen, unveränderten Text
> am Stück zurück, dazu Datum, Wiedervorlagedatum, Zusammenfassung, Keywords, Ticker, alle Thesen und
> **alle Verknüpfungen in beide Richtungen** („Nachträge: …", „gehört zu: …", „widerlegt durch: …").
> **WHEN TO USE:** wenn der genaue Wortlaut zählt — Zitate, Selbstprüfung, „zeig mir, was ich geschrieben
> habe"; außerdem vor jeder Verknüpfung, um das richtige Ziel zu bestätigen.
> **WHEN NOT TO USE:** als Massenausgabe (→ journal_recent / journal_search).

### 6.5 journal_theses (read)

> **Trader-Tagebuch — offene und fällige Thesen.** Zeigt Thesen mit Status, Horizont, Confidence,
> Falsifikationskriterium, Fälligkeit (due_only = Review-Termin erreicht) und dem Eintrag, aus dem sie
> stammen (UUID).
> **WHEN TO USE:** „was ist aus meiner These geworden", „welche Thesen laufen aus", „was muss ich prüfen",
> Jour-fixe, Wochenrückblick. **WHEN NOT TO USE:** für Einträge ohne Thesenbezug (→ journal_recent).

### 6.6 journal_link (write)

> **Trader-Tagebuch — Einträge verknüpfen.** action = "add" | "remove"; from = der spätere Eintrag,
> to = das Original; relation = follow_up | confirms | invalidates | related, dazu optional note und
> theses (einzelne Thesen-UUIDs; ohne Angabe alle offenen Thesen des Ziels).
> Bei invalidates werden die Thesen des Originals als widerlegt markiert, bei confirms als bestätigt.
> **WHEN TO USE:** wenn der Nutzer eine frühere Aussage aufgreift, ergänzt oder erledigt („das gehört zu
> <uuid>", „die These ist damit vom Tisch", „das bestätigt, was ich am 12.09. geschrieben habe").
> Nenne dem Nutzer danach in einem Satz, was verknüpft wurde und welcher Thesenstatus sich geändert hat.
> **WHEN NOT TO USE:** um zwei beliebige Einträge thematisch zu verbinden, ohne dass eine Aussage
> entsteht — dafür genügt die Ähnlichkeitssuche. **Der Originaltext wird nie geändert; Verknüpfen ist
> der Ersatz dafür.**

### 6.7 journal_update (write)

> **Trader-Tagebuch — These bewerten, Wiedervorlage setzen, Metadaten korrigieren.** Setzt an der These:
> status (open/confirmed/invalidated/expired), outcome_note, confidence, review_at, horizon_days.
> Am Eintrag: reminder_date (Datum setzen, verschieben oder mit null löschen), keywords, tickers,
> companies, themes, entry_date, summary.
> **Der Originaltext (body) ist nicht änderbar** — Ergänzungen sind neue Einträge mit Verknüpfung.
> **WHEN TO USE:** wenn der Nutzer eine These bestätigt/widerruft, ein Ergebnis festhält, eine
> Wiedervorlage verschiebt („erinnere mich nach den Zahlen") oder eine Fehlzuordnung korrigiert.
> Nach dem Ansprechen einer fälligen Wiedervorlage: **frage, ob sie erledigt ist** (reminder_date null)
> oder verschoben werden soll — ändere sie nicht von selbst.
> **WHEN NOT TO USE:** um Einträge nachträglich umzuschreiben.

### 6.8 journal_delete (write)

> **Trader-Tagebuch — Eintrag löschen.** Standard ist **soft**: der Eintrag verschwindet aus allen Listen,
> Suchen und Wiedervorlagen, bleibt aber in der Datenbank (Nachvollziehbarkeit, Links bleiben lesbar).
> hard = true entfernt die Zeile endgültig samt Verknüpfungen — **nur** wenn der Nutzer das ausdrücklich
> verlangt („endgültig löschen", „richtig weg").
> **WHEN TO USE:** wenn der Nutzer einen Eintrag verwirft, weil er falsch erfasst, doppelt oder
> gegenstandslos ist. Nenne vorher UUID und Kurzsummary des betroffenen Eintrags und hole eine
> Bestätigung ein. **WHEN NOT TO USE:** um eine These zu erledigen — dafür journal_update (Status) oder
> journal_link (invalidates); Löschen ist kein Urteil über eine These.

*(Phase 2, optional und nur auf Zuruf: journal_digest(days = 7) — LLM-Wochenrückblick über Einträge,
Thesen-Trefferquote und offene Wiedervorlagen. Nicht Teil der Abnahme.)*

---

## 7. Der Agent soll sich erinnern — Persona-Verankerung

Tool-Beschreibungen werden in **jedem** Turn mitgeladen, sind aber „weiche" Steuerung. DSH hat keinen
Session-Start-Hook für MCP-Tools; deshalb kommt die harte Regel ins Trader-Preset
(agent-presets/trader/agent.cordis.yml, Abschnitt 1) als **§1.F „Trader-Tagebuch"** plus Decision-Tree-Zeilen.

**Neuer §1.F (Textentwurf):**

    ### F. Trader-Journal (openbrain-journal) — dein eigenes Gedächtnis

    Das Journal ist DEIN Tagebuch: die eigenen Thesen, Meinungen und Lektionen des Nutzers,
    chronologisch und wörtlich. Es ist NICHT Open Brain (fremde Posts, Recherche) und NICHT
    PTA (Ausführung). Verbindliche Regeln:

    1. ERST LESEN. Zu Beginn jeder Session zu Markt-, Positions- oder Strategiethemen rufst du
       journal_recent(days: 3) auf, bevor du inhaltlich antwortest. Ist der letzte Eintrag älter,
       sage es und behandle die Lage als "kein frischer Kontext".
    2. SPIEGELN. Passt ein früherer Eintrag oder eine offene These zum aktuellen Thema, dann
       benenne ihn (Datum + These) und sage, ob die neue Aussage ihn stützt, verschiebt oder
       widerlegt. Das ist der Zweck des Journals.
    3. WIEDERVORLAGEN ANSPRECHEN. journal_recent liefert fällige und überfällige Wiedervorlagen
       mit. Sprich sie von selbst an ("Du wolltest die Earnings am 2.12. abwarten — der Termin
       ist durch"). Frage danach, ob die Wiedervorlage erledigt ist oder verschoben werden soll;
       ändere sie nicht von selbst. Wir haben keinen Push-Dienst — dieser Aufruf ist der Ersatz.
    4. NUR AUF ZURUF SCHREIBEN. journal_capture ausschließlich, wenn der Nutzer eine eigene
       Einschätzung diktiert oder ausdrücklich speichern will. Niemals proaktiv, niemals
       "zur Sicherheit", niemals aus fremden Posts.
    5. WÖRTLICH SPEICHERN. Den Beitrag unverändert und am Stück übergeben — nicht umformulieren,
       nicht zusammenfassen, nicht aufteilen. Die Zusammenfassung erzeugt das System.
    6. VERKNÜPFEN STATT NEU ERZÄHLEN. Stellt der Nutzer fest, dass sich eine frühere These
       erledigt hat ("der Dieselbedarf ist schneller gesunken als das Angebot"), dann lege den
       neuen Eintrag wörtlich an und hänge ihn mit links: [{to: <uuid>, relation: "invalidates"}]
       an das Original. Der Originaltext bleibt unangetastet; die Thesen des Originals gelten
       danach als widerlegt. Bestätigt der Nutzer eine These, nimm relation "confirms".
    7. THESEN PRÜFEN. Bei "was ist daraus geworden", Wochenrückblick oder vor einer neuen
       Bewertung: journal_theses(status: "open", due_only: true).
    8. LÖSCHEN nur auf ausdrücklichen Wunsch und erst nach Bestätigung; Standard ist soft
       (journal_delete ohne hard). Hart nur, wenn der Nutzer "endgültig" sagt.
    9. EHRLICHKEIT. Ohne Treffer behauptest du kein Gedächtnis. Kein erfundener Eintrag,
       kein erfundenes Datum, kein erfundenes Zitat.

**Decision-Tree-Ergänzungen (§2), fünf Zeilen:**

| Nutzer sagt | Weg |
|---|---|
| „Ich glaube, Cybersecurity profitiert von Metas Muse…" | journal_capture(content: <wörtlich>) → Antwort mit Summary, Thesen und verwandten Einträgen; diese spiegeln |
| „Was habe ich letzte Woche über X gedacht?" | journal_recent(days: 7), danach journal_search(query: "X") |
| „Was ist aus meiner These zu X geworden?" | journal_theses(status: "open", due_only: true) plus journal_get für den Originaltext |
| „Der Dieselbedarf ist schneller gesunken als das Angebot — die These von damals ist hin" | journal_capture(content: <wörtlich>, links: [{to: "<uuid des Originals>", relation: "invalidates", note: "…"}]) |
| „Ich warte die Earnings am 2.12.26 ab" | journal_capture(content: <wörtlich>, reminder_date: "2026-12-02"); am/ab 01.12. meldet journal_recent die Wiedervorlage von selbst |

Ergänzend in **§1.C** bei den Open-Brain-Tools eine Abgrenzungszeile: search_thoughts / capture_thought
sind fremde Inhalte und Rohrecherche; eigene Meinungen → journal_*.

---

## 8. Dateien und Änderungen

**Neu:**

| Pfad | Inhalt |
|---|---|
| migrations/034_trader_journal.sql | 3 Tabellen, Indizes, 7 RPCs, Grants |
| migrations/034_trader_journal.verify.sql | Prüf-Queries (Spalten, Indizes, RPC-Signaturen, search_path) — Muster 019_workitem_engine.verify.sql |
| mcp/agent-journal/index.ts | MCP-Server openbrain-journal, Hono, /health, Key-Auth, Port 8801 |
| mcp/agent-journal/tools/journal_tools.ts | die 8 Tools samt Beschreibungstexten |
| mcp/agent-journal/tools/llm.ts | getEmbedding/getQueryEmbedding (Switchyard), fitForEmbedding, deepseekChatCompletion, log — **bewusste Kopie** des minimal nötigen Teils aus agent-cco/tools/shared.ts; die bestehenden Server werden **nicht** angefasst |
| mcp/agent-journal/prompts/journal-prompt.txt | Destillat-Prompt inkl. reminder_date (Abschnitt 5) |
| mcp/agent-journal/deno.json, Dockerfile | wie mcp/agent-wiq (MCP-SDK 1.24.3, Hono 4.9.2, zod 4, supabase-js 2.49) |
| mcp/agent-journal/tests/call.sh | Smoke-Test: health, tools/list, capture→recent→search→get→link→thesis→update→delete |
| mcp/agent-journal/scripts/backfill_journal.ts | Nachtrag für extraction_failed / embedding IS NULL |

**Geändert:**

| Pfad | Änderung |
|---|---|
| llm-gateway/docker-compose.yml | neuer Service mcp-journal (Build ../mcp/agent-journal, Container llm-gw-mcp-journal, Port 8801:8801; env: SUPABASE_URL/SERVICE_ROLE_KEY, MCP_ACCESS_KEY, SWITCHYARD_URL, EMBED_MODEL/EMBED_DIM/EMBED_MAX_CHARS/EMBED_VERSION/EMBED_MODEL_NAME, DEEPSEEK_*, JOURNAL_DEDUPE_WINDOW_HOURS, JOURNAL_TZ=Europe/Berlin, AGENT_ID=journal) |
| restart.sh | mcp-journal in Stop-/rm-/Build-Listen und im Startbanner |
| agent-presets/trader/agent.cordis.yml | §1.F, §2-Zeilen, §1.C-Abgrenzung |
| roles/cco.cordis.yml, lead.cordis.yml, pca.cordis.yml, pta.cordis.yml, cda.cordis.yml, drawio.cordis.yml | je eine Zeile: id mcp-openbrain-journal, disabled true (Workitem-Rollen sehen das Journal nicht) |
| /home/daniel/.dsh/cordis.patch.yml | Zeile mcp-openbrain-journal (streamable-http, http://127.0.0.1:8801, x-brain-key) — **außerhalb des Workspace: braucht Freigabe oder Handanlage durch den Nutzer** |

---

## 9. Umsetzungsreihenfolge

1. **Migration + Verify** einspielen (docker exec -i openbrain-db psql …), RPC-Signaturen prüfen.
2. **Server** lokal auf dem Host starten (deno run -A index.ts mit SUPABASE_URL=http://127.0.0.1:8001) und
   mit tests/call.sh gegen die echten Endpunkte testen — vor jedem Container.
3. **Compose + restart.sh**, Container bauen und starten, /health und tools/list über Port 8801 prüfen.
4. **DSH-Patch** (Freigabe nötig) und **neue Session**: Tools als mcp__openbrain-journal__* sichtbar.
5. **Preset + Rollen** anpassen; Abnahmetests aus Abschnitt 10 in einer frischen Session fahren.
6. Optional Phase 2: journal_digest.

---

## 10. Tests und Abnahme

| # | Test | Erwartung |
|---|---|---|
| 1 | curl -H "x-brain-key: …" http://127.0.0.1:8801/health | status healthy, server openbrain-journal |
| 2 | tools/list | genau 8 Tools mit den Beschreibungen aus Abschnitt 6 |
| 3 | journal_capture mit dem Meta/Cybersecurity-Beispieltext | body **byte-identisch** (sha256(body) = body_sha256, gegen den Eingabestring geprüft), entry_date = heute Berlin, summary nicht leer, keywords ≥ 3, companies enthält Meta und Google, theses ≥ 2 |
| 4 | derselbe Text sofort erneut | duplicate_of true, kein zweiter Eintrag |
| 5 | Dedupe-Fenster: RPC direkt mit p_dedupe_window_hours = 0 | zweiter Eintrag entsteht |
| 6 | journal_recent(days: 3) | Eintrag erscheint, **aufsteigend** sortiert, ohne body, mit UUID-Kurzform, unter ~150 Tokens pro Eintrag |
| 7 | journal_search("Agent-APIs Mittelsmann Werbung") | Treffer über Vektor, ohne identisches Wort im Text |
| 8 | journal_get(id) | Originaltext am Stück, sha256 gleich Test 3 |
| 9 | **Verknüpfen:** zweiter Eintrag „Dieselbedarf schneller gesunken" mit links: [{to: <uuid Original>, relation: "invalidates"}] | Link angelegt; Thesen des Originals status = invalidated mit outcome_note; journal_get(Original) zeigt „widerlegt durch <uuid>"; journal_recent zeigt die Verknüpfung; **body des Originals unverändert** (sha256 gleich) |
| 10 | **Verknüpfen per Präfix** journal_link(to: "<erste 8 Zeichen>", relation: "confirms") | wird aufgelöst; mehrdeutiger Präfix liefert Fehler mit Kandidaten, kein Rateentscheid |
| 11 | **Wiedervorlage explizit:** reminder_date = heute | erscheint in journal_recent im Block Wiedervorlagen („heute") |
| 12 | **Wiedervorlage implizit:** Text „ich warte die Earnings am 2.12.26 ab" | LLM setzt reminder_date = 2026-12-02; mit reminder_date = morgen erscheint sie als „in 1 Tag"; überfällige mit negativer Tagesangabe |
| 13 | journal_update(reminder_date: null) | Wiedervorlage verschwindet aus dem Block |
| 14 | **Soft-Delete** eines verknüpften Eintrags | verschwindet aus recent/search/theses/reminders; der Nachtrag bleibt und zeigt „Original gelöscht"; DB-Zeile existiert weiter (deleted_at gesetzt) |
| 15 | **Hard-Delete** nach Bestätigung | Zeile und Links weg; fremde Einträge unversehrt; ohne hard-Flag nicht möglich |
| 16 | journal_theses(due_only: true) nach Setzen von review_at = gestern | These erscheint als fällig |
| 17 | LLM-Fehler (Testlauf mit ungültigem DEEPSEEK_API_KEY) | Eintrag gespeichert, extraction_failed true, Verknüpfung trotzdem angelegt, backfill_journal.ts ergänzt Summary/Thesen/Vektor |
| 18 | Grenzfall Datum: Container-UTC gegen Berlin, Test um 00:30 Berliner Zeit | entry_date und Wiedervorlage-Vergleich nutzen das Berliner Datum (konfigurierbar über JOURNAL_TZ) |
| 19 | neue DSH-Session | mcp__openbrain-journal__journal_recent im Katalog; Workitem-Rolle cco sieht es **nicht** |
| 20 | Gesprächstest | Nutzer diktiert These → Agent speichert wörtlich, nennt Summary, Thesen, verwandte Einträge; in einer Folgesession erinnert er sie und spricht eine fällige Wiedervorlage von selbst an |

---

## 11. Risiken und bewusste Entscheidungen

* **Embedding-Dimension.** Immer 1024/mxbai-v1. Ein 4096er-Vektor erzeugt harte
  „different vector dimensions"-Fehler; EMBED_DIM/Guard wie im CCO-Container setzen.
* **search_path.** Jede Vektor-RPC braucht SET search_path TO 'public','extensions' — sonst schlägt jeder
  Aufruf fehl (Fehler aus Migration 018). Steht in der Verify-Datei.
* **Zeitzone.** Container läuft UTC; entry_date und der Wiedervorlagen-Vergleich nutzen AT TIME ZONE
  gegen JOURNAL_TZ (Default Europe/Berlin). Timestamps bleiben UTC.
* **Statusänderung durch Verknüpfung ist gewollte Automatik.** invalidates/confirms setzen den Thesenstatus
  deterministisch — das ist die vom Nutzer gewünschte Klarheit, kein autonomes Schreiben. Sie ist auf
  einzelne Thesen einschränkbar (theses-Array) und wird in der Antwort immer gemeldet.
* **Kein Push.** Wiedervorlagen wirken nur, wenn der Agent journal_recent aufruft. Abgesichert durch die
  Persona-Regel 1 und 3; wer den Agenten nie zu Marktthemen befragt, wird nicht erinnert. Bewusst akzeptiert.
* **Präfix-Auflösung.** Mindestens 6 Zeichen, bei Mehrdeutigkeit Fehler statt Rateentscheid.
* **Verlustfreiheit vor Reichhaltigkeit.** Insert vor LLM. Ein fehlendes Destillat ist ein Schönheitsfehler,
  ein verlorener Tagebucheintrag nicht.
* **Kein UNIQUE auf dedupe_hash.** Anders als open_brain — derselbe Gedanke an einem anderen Tag ist eine
  neue Information (Überzeugung wächst).
* **Kopierte LLM-Helfer.** Bewusst Duplikat statt Refactoring von agent-cco/tools/shared.ts (46 kB, von
  Workern und Tools geteilt) — das Risiko am laufenden System wäre größer als die Redundanz.
* **Sandbox.** /home/daniel/.dsh/cordis.patch.yml liegt außerhalb des Workspace; Schreiben dort braucht eine
  Freigabe oder einen manuellen Eintrag durch den Nutzer. Compose und restart.sh liegen im Workspace.
* **Sichtbarkeit.** Der Trader-Agent sieht alle global registrierten MCP-Tools (kein Filter aktiv) — das
  Journal ist damit sofort verfügbar; die Workitem-Rollen werden einzeln abgeschaltet.
* **Tool-Verwechslung.** capture_thought (Open Brain) und journal_capture liegen beide im Katalog. Deshalb
  steht die Abgrenzung in **beiden** Richtungen in den Beschreibungen und im Preset.

---

## 12. Nicht im Scope

* Keine Verknüpfung zu Trades/Positionen (pta_execution_log) — Entscheidung 4.
* Keine Migration oder Verlinkung bestehender open_brain-Einträge — Entscheidung 4.
* Keine automatischen Einträge (Breadth, Kurse, Trades, CCO-Posts) — Entscheidung 5 und source = manual.
* Kein proaktives Schreiben durch den Agenten — Entscheidung 3.
* Kein Push-Dienst für Wiedervorlagen — die Erinnerung wirkt über journal_recent (bewusst akzeptiert).
* Kein Dashboard, keine Chart-Annotationen, keine Auswertung der Thesen-Trefferquote (Phase 2+).

---

## 13. Umsetzungsstand (2026-09-25)

**Status: umgesetzt und abgenommen.** Migration eingespielt, Container laeuft, Tools in DSH sichtbar,
Abnahmetest gruen.

| Baustein | Zustand | Nachweis |
|---|---|---|
| migrations/034_trader_journal.sql | eingespielt | 3 Tabellen, 12 Funktionen, GIN- und HNSW-Indizes |
| migrations/034_trader_journal.verify.sql | gruen | Tabellen, Spalten, Indizes, RPCs, search_path, Praefix-Aufloesung |
| mcp/agent-journal/ (index.ts, tools/, prompts/, Dockerfile, deno.json) | laeuft | Container llm-gw-mcp-journal, Port 8801, /health healthy |
| llm-gateway/docker-compose.yml, restart.sh | verdrahtet | docker compose build/up mcp-journal erfolgreich |
| /home/daniel/.dsh/cordis.patch.yml | registriert | Tools erscheinen als mcp__openbrain-journal__* |
| agent-presets/trader/agent.cordis.yml | ergaenzt | Abschnitt 1.F, Decision-Tree 23-27, Abgrenzung in 1.C |
| roles/*.cordis.yml (6 Rollen) | gesperrt | mcp-openbrain-journal disabled |
| mcp/agent-journal/tests/e2e.py | 30 Pruefungen gruen | zweimal gelaufen: lokaler Host-Server und Container |
| mcp/agent-journal/scripts/backfill_journal.ts | vorhanden | fuer extraction_failed / embedding IS NULL |

**Abweichungen vom Plan (klein, alle getestet):**

1. **Link-Operator:** Der Plan sah Vergleiche ueber to_jsonb vor. Postgres kennt text[] @> jsonb nicht —
   hybrid_search_journal schlug fehl. Korrigiert auf keywords @> ARRAY[q]. Gefunden vom Abnahmetest,
   nicht im Betrieb.
2. **journal_capture zusaetzlich:** Parameter parent_hint (nur fuer die Antwort) und force (ueberspringt
   das Dedupe-Fenster, wenn eine Wiederholung Absicht ist).
3. **Verknuepfungen werden vor dem Insert aufgeloest:** Ein ungueltiger Praefix bricht nicht die ganze
   Transaktion ab, sondern erscheint als Hinweis in der Antwort — der Eintrag ist gerettet.
4. **journal_enrich** prueft die Vektor-Dimension (1024) und wirft bei falschem Modell sofort; body und
   body_sha256 werden nie angefasst.
5. **Tests:** e2e.py deckt die Abnahmetests aus Abschnitt 10 ab; call.sh bleibt fuer Einzelaufrufe.
6. **Der DSH-Client wurde ohne Host-Neustart aktiv** — die Tools sind in der laufenden Session sichtbar.

**Offen (bewusst):** journal_digest (Phase 2), kein Push-Dienst fuer Wiedervorlagen, keine automatischen
Eintraege, keine PTA-/open_brain-Verknuepfung.

**Betriebshinweise:**

* Neustart nach Codeaenderung: ./restart.sh mcp-journal
* Migration erneut einspielen: docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 < migrations/034_trader_journal.sql
* Abnahmetest: python3 mcp/agent-journal/tests/e2e.py (raeumt sich selbst auf; hart geloescht wird nur, was mit E2E-TEST beginnt)
* Nachtrag fehlgeschlagener Anreicherungen: deno run -A mcp/agent-journal/scripts/backfill_journal.ts
* Das Erinnern haengt an Preset-Regel 1 (journal_recent vor der inhaltlichen Antwort) — die einzige
  Verhaltensregel, die ein Tool nicht selbst erzwingen kann.

---

## 14. Nachtrag 2026-09-25: Schreibtrigger geschaerft (Rev. 4)

**Befund des Nutzers:** Der Agent legte zu eifrig Eintraege an. Ursache war die eigene Formulierung:
Die Tool-Beschreibung sagte "WHEN TO USE: immer wenn der Nutzer eine eigene Einschaetzung, These,
Prognose ... formuliert", und Decision-Tree-Zeile 23 machte aus "Ich glaube, Cybersecurity profitiert
von Metas Muse" direkt einen journal_capture-Aufruf.

**Aenderung — Schreiben nur noch auf ausdruecklichen Befehl:**

* journal_capture: neue fuehrende Regel "NUR AUF AUSDRUECKLICHE ANWEISUNG" mit Ausloeserliste
  (schreib das ins Tagebuch / ins Journal / ins Diary / notier das / journal das / halt das fest /
  trag das ein / merk dir das) und explizitem KEIN AUSLOESER-Absatz: eine These, Meinung oder Prognose
  allein wird diskutiert. Ohne Befehl hoechstens anbieten und auf ein klares Ja warten; im Zweifel
  nicht schreiben.
* journal_link und journal_update: gleiche fuehrende Regel — Verknuepfen und Statuswechsel sind
  Schreibvorgaenge.
* journal_delete war bereits explizit-only.
* Preset-Regel 4 heisst jetzt "NUR AUF AUSDRUECKLICHEN BEFEHL SCHREIBEN" und gilt ausdruecklich fuer
  alle schreibenden Journal-Tools; Regel 6 verlangt fuer das Anhaengen einen ausdruecklichen Auftrag;
  die Abgrenzungszeile in Abschnitt 1.C nennt die Bedingung ebenfalls.
* Decision-Tree: Zeile 23 ist jetzt der Nicht-Schreibfall (These ohne Befehl -> nur diskutieren,
  hoechstens anbieten), neue Zeile 23b der Schreibfall; Zeile 26 verlangt den Zusatz "trag das ein".

**Grenze, ehrlich benannt:** Ob geschrieben wird, entscheidet am Ende das Modell anhand von
Tool-Beschreibung und Persona. Ein serverseitiger Guard ist nicht moeglich, weil der MCP-Server den
Gespraechsverlauf nicht sieht. Wirkt ab der naechsten Session (Persona wird beim Sessionstart geladen);
die Tool-Beschreibungen sind bereits live (Container neu gestartet, per tools/list verifiziert).
