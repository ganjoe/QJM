# YouTube-Zeitachse & MCP-Suche — Migrations- und Abnahmekonzept

**Status:** Entwurf (es wurde noch KEIN Code/Datenbestand geändert)
**Stand:** 2026-09 (Systemzeit)
**Scope:** `agent-cco` (YouTube), Tabellen `yt_videos` / `agent_workspace`, MCP-Tools.
**Nicht-Scope:** `open_brain` (Notizen), X-Posts.

---

## 1. Problem

Die Suche nach "macro trading ideen von anfang 2022" ist heute nicht nachvollziehbar, weil:

1. Upload-Datum an drei Orten dupliziert und inkonsistent ist (`content`-Präfix, `created_at`, `metadata.published_at`).
2. 3.776 von 3.887 Videos kein Datum haben; die 111 vorhandenen sind teils approximativ.
3. `search_youtube_content` die Tabelle `open_brain` (41 Notizen) abfragt, nicht die 95.365 Chunks.
4. Es keinen Datumsfilter gibt, nur `days_back` auf dem überladenen `created_at`.
5. Kein stabiler Chunk-Schlüssel existiert (nur JSON `metadata->>'video_id'`), keine FK, kein Zeitcode-Modell.

Details und Messwerte: siehe Chat-Protokoll / vorherige Analyse.

---

## 2. Entscheidungen (ADR)

| # | Entscheidung |
|---|---|
| ADR-1 | Datums-Backfill **exakt** (per-Video `yt-dlp --skip-download`), nicht approximativ. |
| ADR-2 | `open_brain` gehört nicht zur YouTube-Suche. `search_youtube_content` sucht **Chunks**; Notizen laufen weiter über `search_thoughts`. |
| ADR-3 | `yt_videos.published_at` wird **ersetzt** durch `upload_date` (Breaking Rename + Typ `date`). |
| ADR-4 | **Option A:** `agent_workspace` in-place um typisierte Spalten erweitern + View `yt_chunks`. Kein physikalischer Umzug (1,6 GB Vektoren bleiben liegen). |
| ADR-5 | **Kein Re-Embedding jetzt.** Re-Embedding wird als eigener, atomarer Lauf vorbereitet und muss danach konsistente Daten erzeugen. |
| ADR-6 | **UTC** ist Default der Datenspeicherung. `upload_date date`, Zeitstempel `timestamptz` in UTC. |

---

## 3. Ziel-Datenmodell

### 3.1 `yt_videos` (Quelle, SSoT)

```sql
-- Skizze, NICHT ausgeführt (Phase 1)
ALTER TABLE yt_videos RENAME COLUMN published_at TO upload_date;
ALTER TABLE yt_videos
  ALTER COLUMN upload_date TYPE date
  USING (upload_date AT TIME ZONE 'UTC')::date;

ALTER TABLE yt_videos
  ADD COLUMN upload_date_source    text,   -- 'exact' | 'approximate'
  ADD COLUMN upload_date_precision text,   -- 'day' | 'month' | 'year'
  ADD COLUMN metadata_synced_at    timestamptz;

ALTER TABLE yt_videos
  ADD CONSTRAINT chk_yt_upload_date_source
  CHECK (upload_date IS NULL OR upload_date_source IS NOT NULL);
```

Regeln:
- `upload_date` ist tagesgenau, UTC, unveränderlich sobald `source='exact'`.
- `source='approximate'` darf später durch `exact` überschrieben werden, nie umgekehrt.
- Kein Tool liest mehr `created_at` als Event-Datum.

### 3.2 `agent_workspace` (Option A)

```sql
ALTER TABLE agent_workspace
  ADD COLUMN video_id          text REFERENCES yt_videos(video_id),
  ADD COLUMN chunk_index       integer,
  ADD COLUMN t_start_sec       integer,
  ADD COLUMN t_end_sec         integer,
  ADD COLUMN embedding_model   text,
  ADD COLUMN embedding_version text,
  ADD COLUMN embedded_at       timestamptz,
  ADD COLUMN source_hash       text;

CREATE UNIQUE INDEX idx_aw_yt_chunk_uid
  ON agent_workspace(video_id, chunk_index)
  WHERE artifact_type='yt_chunk' AND video_id IS NOT NULL;
```

- Neue Spalten sind für `x_post` NULL — kein Konflikt.
- `chunk_uid` logisch = `video_id || ':' || chunk_index` (Unique-Index erzwingt Eindeutigkeit).
- `created_at` bedeutet ab jetzt **nur** "Zeile erzeugt", nicht Event-Datum.

### 3.3 View `yt_chunks`

```sql
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
```

Alle Tools/RPCs lesen aus dieser View. Datum/Kanal/Titel kommen per Join — nie aus dem Chunk kopiert.

### 3.4 Indizes

```sql
CREATE INDEX idx_yt_videos_upload_date ON yt_videos(upload_date DESC);
CREATE INDEX idx_aw_yt_video ON agent_workspace(video_id) WHERE artifact_type='yt_chunk';
CREATE INDEX idx_aw_yt_fts   ON agent_workspace
  USING gin (to_tsvector('english', content)) WHERE artifact_type='yt_chunk';
```

**Vektorindex:** `embedding vector(4096)`. pgvector indiziert `vector` bis 2.000 und `halfvec` bis 4.000 Dimensionen — 4.096 liegt darüber. Daher zunächst **Full Scan** (95k Zeilen, akzeptabel).
Später optional: Dimensionsreduktion (Matryoshka, z. B. auf 1.024/2.000) oder Bit-Quantisierung als Vorfilter. In Phase 1 verifizieren, nicht vorab annehmen.

---

## 4. UTC-Semantik

- `upload_date` = `date` (keine Zeitzone, per Definition UTC).
- "Anfang 2022" = halboffenes Intervall `[2022-01-01, 2022-04-01)`.
- `created_at`, `embedded_at`, `metadata_synced_at` = `timestamptz` (UTC gespeichert).
- DB-Session-Default `timezone='UTC'` explizit setzen, damit keine impliziten Lokalzeit-Konvertierungen passieren.

---

## 5. Ingestion-Pipeline (getrennte, idempotente Stufen)

| Stufe | Aufgabe | Idempotenz | Neustart-Sicherheit |
|---|---|---|---|
| A Discovery | `video_id` + **exaktes** `upload_date` | Upsert by `video_id` | überschreibt nur NULL/approximate |
| B Transkript | VTT -> `yt_videos.transcript` | Skip wenn vorhanden | unverändert |
| C Chunking | Text -> Chunks + Zeitcodes | Delete/Insert by `(video_id, chunk_index)` | kein Duplikat |
| D Embedding | nur fehlende/veraltete Embeddings | Skip wenn `embedding_version` aktuell | GPU nur bei Bedarf |
| E Metadata-Repair | nur Lücken/approximate neu abfragen | überschreibt nie `exact` | fasst B/C/D nie an |

Stufe E ist der "ohne neu runterladen"-Pfad (ADR-1).

---

## 6. Exakter Datums-Backfill (Phase 2)

Verfahren:
1. `SELECT video_id FROM yt_videos WHERE upload_date IS NULL OR upload_date_source='approximate'`.
2. Batches an `yt-dlp --cookies ... --batch-file /dev/stdin --skip-download --no-warnings --print "%(id)s|%(upload_date)s"`, parallel (z. B. 8 Worker).
3. Ergebnis in Temp-Tabelle laden, per `UPDATE ... FROM` nur NULL/approximate setzen:
   - `upload_date = to_date(...)`, `upload_date_source='exact'`, `upload_date_precision='day'`, `metadata_synced_at=now()`.
4. Dry-Run zuerst (Report: gematcht / nicht in DB / bleibt NULL / nicht mehr verfügbar).

Erwartung: ~3.774 Videos, ~1,2 s/Video seriell, mit 8 Workern ~10 min. **Keine** Transkript- oder Embedding-Operation.
Sicherung vorab: `CREATE TABLE yt_videos_backup_<ts> AS SELECT * FROM yt_videos;`

---

## 7. Chunk-Anreicherung ohne Re-Embedding (Phase 3)

Für alle `artifact_type='yt_chunk'`:
- `video_id`, `chunk_index`: aus `metadata->>'video_id'` / `metadata->>'block_index'`.
- `t_start_sec`, `t_end_sec`: aus den `[MM:SS]`-Markern im Chunk-Text (erster/letzter Marker).
- `metadata.upload_date`: aus Join gesetzt (Key analog ADR-3 umbenannt).
- `created_at` bleibt physisch, wird aber nicht mehr als Datum interpretiert.
- `content`-Präfix: `Datum: <upload_date>` statt `Unbekannt`, sofern Datum vorhanden.

Wichtig: Das **Embedding bleibt unverändert** (ADR-5). Der Präfix ändert sich nur textlich; der Vektor ist minimal abweichend. Das ist bewusst akzeptiert.

---

## 8. Re-Embedding-Vertrag (Zukunft, ADR-5)

Damit ein späterer Re-Embed-Lauf "alles ordentlich" macht:

1. Kontext-Header wird **immer aus dem Join** erzeugt (`yt_chunks`), nie aus gespeicherten Chunk-Daten.
2. `source_hash = hash(chunk_text + header)`; `embedding_version` wird beim Lauf gebumpt.
3. Modell/Route: `embedding_model='qwen3-embedding:8b'` (via Switchyard), Version = Digest/Zeitstempel.
4. Lauf schreibt zuerst in eine neue Version, schaltet dann atomar um (`UPDATE ... SET embedding=<neu>, embedding_version=<v2> WHERE ...`), erst danach `embedded_at`/`source_hash` aktualisieren.
5. Stale-Erkennung: `embedding_version <> aktuell OR source_hash <> hash(content+header)`.

---

## 9. Retrieval-RPC (Phase 4)

Eine kanonische Funktion, alle bisherigen YT-Suchpfade ersetzen:

```sql
search_yt_chunks(
  p_query text,
  p_embedding vector(4096),
  p_channels text[] DEFAULT NULL,
  p_date_from date DEFAULT NULL,
  p_date_to date DEFAULT NULL,      -- exklusiv
  p_tickers text[] DEFAULT NULL,
  p_language text DEFAULT NULL,
  p_min_similarity float DEFAULT 0.4,
  p_limit int DEFAULT 20
) RETURNS TABLE (
  chunk_id uuid, video_id text, channel text, title text,
  upload_date date, upload_date_source text, upload_date_precision text,
  chunk_index int, t_start_sec int, t_end_sec int,
  content text, url text, similarity float, keyword_rank real,
  score float, matched_terms text[]
);
```

Eigenschaften:
- Filter ausschließlich auf `upload_date`/Join-Feldern, nie auf `created_at`.
- Hybrid: Vektor + FTS + exakter Keyword-Treffer, kombiniert per RRF (Reciprocal Rank Fusion).
- Deterministisch: `ORDER BY score DESC, upload_date DESC NULLS LAST, video_id, chunk_index`.
- `url = 'https://www.youtube.com/watch?v=' || video_id` (optional mit `&t=<t_start_sec>`).

---

## 10. MCP-Tool-Verträge (Phase 5)

| Tool | Vorher | Nachher |
|---|---|---|
| `search_youtube_content` | liest `open_brain` | liest `yt_chunks`; Parameter `query, channels, date_from, date_to, tickers, limit`; Ausgabe mit Provenienz + `applied_filters` |
| `show_yt_content` | `published_at`, "Unbekannt" | `upload_date` + Qualitäts-Flag, optionaler Datumsbereich |
| `show_yt_transcript` | Titel + Text | zzgl. `video_id`, Kanal, `upload_date`, Zeitcode-Format |
| `manage_youtube_channels` | LIST/ADD/REMOVE | unverändert |
| **NEU** `manage_yt_metadata` | — | `STATUS`, `BACKFILL_DATES` (exact/approximate, dry-run), `SYNC_CHANNEL_METADATA` |
| `search_thoughts` | `open_brain` | unverändert (klare Trennung, ADR-2) |

Jedes Suchergebnis liefert: Kanal, Titel, `video_id`, `upload_date` + Qualität, `chunk_index`, Zeitcode, URL, Score, Treffergrund.

---

## 11. Phasen und Rollback

| Phase | Inhalt | Rollback |
|---|---|---|
| 0 | Read-only Messung, Golden Queries einfrieren | — |
| 1 | Schema additiv (Rename/Typ, Spalten, FK, View, Indizes) | Spalten/View droppen, Rename zurück |
| 2 | Exakter Datums-Backfill | `yt_videos_backup_<ts>` zurückspielen |
| 3 | Chunk-Anreicherung (ohne Re-Embedding) | neue Spalten auf NULL setzen |
| 4 | Such-RPC `search_yt_chunks` | alte RPC unverändert lassen, nur additiv |
| 5 | MCP-Tools umstellen | alten Tool-Code zurücksetzen |
| 6 | Abnahme + Doku | — |

Jede Phase separat, testbar, reversibel. Kein Schritt fasst Transkripte an.

---

## 12. Abnahmetests

- **T1 Datumsfenster:** "macro trading ideen von anfang 2022" liefert ausschließlich `upload_date in [2022-01-01, 2022-04-01)`; angewandte Filter im Output.
- **T2 Provenienz:** jedes Ergebnis hat `channel, title, video_id, upload_date, chunk_index, url, score`; `t_start_sec` gesetzt, wenn Zeitmarker existieren.
- **T3 Determinismus:** zwei identische Läufe -> identische Reihenfolge.
- **T4 Qualität:** `approximate`-Treffer sind als solche markiert, nicht als exakt.
- **T5 Vollständigkeit:** Report "im Fenster fehlen noch N Datumsangaben".
- **T6 Isolation:** X-Post-Suche (`semantic_search_workspace`, `artifact_type='x_post'`) unverändert.
- **T7 Kein Re-Download:** Transkript-Anzahl und Stichproben-Hashes vor/nach Backfill identisch.
- **T8 Re-Embed-Bereitschaft:** Dry-Run markiert alle Chunks als `current` ODER listet exakt die zu erneuernden.

---

## 13. Blast Radius (nur zur Info, noch keine Änderung)

- `mcp/agent-cco/workers/yt_ingestion_worker.ts` (Insert/Select/Order, Header, `created_at`)
- `mcp/agent-cco/workers/company_extraction_worker.ts` (Order by Datum)
- `mcp/agent-cco/tools/youtube_tools.ts` (`show_yt_content`, Suche, Transkript)
- RPC `get_yt_chunks_with_tickers` (`metadata->>'published_at'`)
- `mcp/agent-cco/scripts/backfill_yt_dates.sh` (auf `upload_date` umstellen)
- **Nicht anfassen:** `published_at` bei X-Posts (`x_ingestion_worker`, `metadata_worker`, `x_tools`).

---

## 14. Offene Punkte

1. Vektorindex-Strategie bei 4.096 Dim (Full Scan vs. Dimensionsreduktion) — Phase 1 verifizieren.
2. Exakte Definition `upload_date_precision` bei yt-dlp-Sonderfällen (Livestreams, Premieres).
3. Ob `get_yt_chunks_with_tickers` erhalten oder durch `search_yt_chunks` ersetzt wird.
4. Ob der `content`-Header das Datum behalten soll (Empfehlung: ja, aber normalisiert aus dem Join).

---

## 15. Umsetzungsstand (Ist)

### Durchgeführt

| Phase | Status | Ergebnis |
|---|---|---|
| 0 | ✅ | Backups `yt_videos_backup_20260914_032843` (3.887) und `aw_yt_backup_20260914_032843` (95.365); Baseline in `yt-migration-baseline.md` |
| 1 | ✅ | `upload_date date`, `upload_date_source`, `upload_date_precision`, `metadata_synced_at`; Chunk-Spalten in `agent_workspace`; FK `fk_aw_video`; View `yt_chunks`; Indizes (upload_date, channel+date, FK, FTS-GIN) |
| 2 | ✅ | **3.885 exakt, 2 dauerhaft nicht verfügbar** (`Video unavailable`, gelöscht/gesperrt). Durchbruch: der YouTube-Block hing an der **Cookie-Session**; cookieloser Lauf (`NO_COOKIES=1`) lieferte 100 % Treffer. |
| 3 | ✅ | 95.365 Chunks: `video_id`, `chunk_index`, `t_start_sec`, `t_end_sec` (92.955 mit Timecode), `embedding_model`, `embedding_version='legacy-v1'`, `embedded_at`, `source_hash`. **Kein Re-Embedding**, `content` unverändert |
| 4 | ✅ | `search_yt_chunks` (Hybrid, Filter auf `upload_date`, deterministisch, Provenienz) + `get_yt_chunks_with_tickers` auf Join/upload_date umgestellt |
| 5 | ✅ | `youtube_tools.ts` (Suche auf Chunks, show/transcript mit Qualität, neu `manage_yt_metadata`), `yt_ingestion_worker.ts` (upload_date, Timecodes, Vektor-Metadaten), `company_extraction_worker.ts` |
| 6 | ✅ | Abnahmetests s.u.; `published_at` entfernt (Phase 5b). |

### Abnahmetests

- **T1 Datumsfenster:** Query "macro trading ideen anfang 2022" mit `[2022-01-01, 2022-04-01)` liefert @traderlion "Crash Course: ... How To Trade in 2022" (2022-02-22, exakt) und @42macro "ETF THINK TANK | FEBRUARY 17, 2022" (2022-02-19, exakt). ✅
- **T2 Provenienz:** Kanal, Titel, video_id, upload_date, chunk_index, Timecode, URL, Score, Trefferterme vorhanden. ✅
- **T3 Determinismus:** zwei identische Läufe -> identische Reihenfolge (4 Treffer). ✅
- **T4 Qualität:** exakt/approximativ wird ausgewiesen. ✅
- **T5 Vollständigkeit:** `manage_yt_metadata STATUS` liefert exakt/approximativ/fehlend je Kanal. ✅
- **T6 Isolation:** X-Posts unverändert (29.218 Zeilen, `video_id` NULL). ✅
- **T7 Kein Re-Download:** Transkripte 3.281 vor/nach identisch; Videos 3.887. ✅
- **T8 Re-Embed-Bereitschaft:** alle Chunks `embedding_version='legacy-v1'`; Re-Embed über `source_hash` + Version steuerbar. ✅ (Plan)

### Vorfall

YouTube rate-limitierte **die Cookie-Session** nach ~488 schnellen Abfragen ("rate-limited for up to an hour"); aggressive Läufe mit Cookies verschärften das. **Lösung:** identische Abfragen **ohne Cookies** funktionierten sofort (anonyme Session) und lieferten 100 % Treffer. Zwischenzeitlich diente `phase2b_approx_dates.sh` (Flag `approximate`) als Überbrückung. `phase2e_datapi.sh` (YouTube Data API v3) bleibt als Option, war aber wegen API-Key-Einschränkung nicht nutzbar.

### Offen

1. ~~Exakten Backfill abschließen~~ **erledigt:** 3.885 exakt; 2 Videos sind dauerhaft nicht abrufbar. `phase2e_datapi.sh` bleibt für künftige Läufe bereit.
2. ~~`yt_videos.published_at` physisch droppen~~ **erledigt** (Phase 5b: `phase5b_drop_published_at.sql`, Spalte entfernt, alle Backfill-Skripte bereinigt).
3. ~~Golden Query 3~~ **erledigt:** @traderlion "Power Earnings Gaps" (2021) und "How To Trade in 2022" (2022-02-22, exakt) verifiziert.
4. `manage_yt_metadata` erscheint im DSH-Client erst nach einer neuen Session (Server ist registriert).
5. Vektorindex-Strategie bei 4.096 Dim (Full Scan vs. Reduktion) bleibt offen.

---

## 16. Nachbesserung: Fehlende Transkripte & Retry-Logik

### Ursache
- `-dv_2h61a2o` wurde als **Premiere** entdeckt und 16 min vor Start abgefragt: `Premieres in 16 minutes`. Der Worker setzte `status='failed'` — und `failed` war **terminal** (Download-Loop liest nur `pending`; Discovery fügt nur neue Videos ein).
- Verteilung der ursprünglich 654 Fehler: 538 `Video unavailable`, 50 `Keine Auto-Captions`, 48 Embedding-503, 15 Rate-Limit, 2 Premiere.
- **Korrektur der Erstanalyse:** Die 538 `Video unavailable` galten zunächst als „dauerhaft". Stichproben zeigten: die Videos sind **`public`/`not_live`** (537 davon @42macro) und haben Auto-Captions; auch 4/4 der `Keine Auto-Captions`-Fälle hatten wieder Captions. Die Fehler waren **transient** (Session-/Rate-Limit-bedingt), nicht endgültig.

### Sofortmaßnahmen (durchgeführt)
- 65 zuerst reaktiviert (17 `pending`, 48 `downloaded`), nach der Korrektur **alle 588** erneut auf `pending`.
- Verifiziert: `-dv_2h61a2o` (183.352 Zeichen), `J-I6iLGjp1Q` (128.300), `bnNUtSvMjTg` (74.158); `8QKr9o6VyIk` ist `public` und der VTT-Download funktioniert.
- Downloader holt die 588 Transkripte nach; `failed` = 0.

### Retry-Logik (Code)
- Neue Spalten `retry_count`, `next_retry_at`.
- `scheduleRetry(...)`: **unbegrenzte** Wiederholung mit gedeckeltem Backoff (5 min × 2^n, max. 6 h). **Kein Max-Cap und kein endgültiges `failed`** (nur `PERMANENT_ERROR_PATTERNS`, aktuell leer). Ein dauerhaft nicht verfügbares Video wird also alle 6 h erneut versucht und kann sich jederzeit selbst reparieren.
- **Supervisor:** `startYtWorker` legt `runYtLoop` in eine Aufsichtsschleife, die die Pipeline nach einem unerwarteten Ende automatisch neu startet (30 s Pause).
- Verifiziert per Fehlertest: `bnNUtSvMjTg` mit ungültiger Sprache -> `status=pending`, `retry_count=1`, `next_retry_at=+5 min`, `error_msg="Retry #1 ..."` (kein `failed`); nach Korrektur wieder `downloaded` (74.158 Zeichen).
- Beide Loops filtern `next_retry_at <= now()`; Update-Fehler werden geprüft und geloggt.

### Cache-Fallstrick (wichtig für künftige Migrationen)
- Nach `ALTER TABLE ... ADD COLUMN` kennt PostgREST die Spalte erst nach Schema-Reload. Sonst scheitern Worker-Updates **still** mit `PGRST204` und die Zeile bleibt `pending` -> Endlosschleife (beobachtet und behoben).
- Regel: bei jeder Spaltenänderung `NOTIFY pgrst, 'reload schema';` (in `phase6_retry_and_requeue.sql` ergänzt).



