# MXBAI-MIGRATION — Abschlussbericht

**Datum:** 2026-09-18 · **Status:** abgeschlossen und abgenommen
**Ziel:** Umstellung des gesamten Embedding-Systems von `qwen3-embedding:8b` (4096 Dim) auf
`mxbai-embed-large` (1024 Dim).

---

## 1. Ergebnis in Zahlen

| Kennzahl | Vorher (qwen3-8b) | Nachher (mxbai) |
|---|---|---|
| Dimension | 4096 | **1024** |
| YT-Chunks | 100.634 (1500 Zeichen) | **226.916** (800 Zeichen, Overlap 200) |
| X-Posts vektorisiert | 31.839 | **32.098** |
| Vektoren gesamt | 132.473 | **259.014** |
| Vektor-Index | keiner (4096 > 2000 ⇒ HNSW unmöglich) | **HNSW** (m=16, ef_construction=64), 2,0 GB |
| DB-Größe agent_workspace | 2.719 MB | 3.836 MB (Heap 378 MB + Index 2.089 MB) |
| Durchsatz | 0,146–0,217 docs/s | **3,8–5,7 docs/s** (~17×) |
| Retrieval Recall@10 | 1,000 | **1,000** |
| Retrieval nDCG@10 | 0,8943 | **0,9397** |
| Retrieval MRR | 0,8634 | **0,9306** |
| Retrieval Recall@1 | 1,000 (Referenzmessung) | 0,9167 |
| Interaktive Suche | — | **35–824 ms** |
| Re-Embed-Dauer | — | 50.760 s (14,1 h) für 259k Vektoren |

Modellwahl-Beleg (fairer Vergleich, identische Texte, gleicher Pool, Batch 2):

| Modell | Dim | Recall@10 | nDCG@10 | MRR | docs/s |
|---|---|---|---|---|---|
| qwen3-embedding:8b | 4096 | 1,000 | 0,8848 | 0,8472 | 0,217 |
| **mxbai-embed-large** | **1024** | **1,000** | **1,000** | **1,000** | **3,808** |

→ Auf identischem Material ist mxbai **gleichwertig bis besser** und **~17× schneller**.
Der einzige metrische Rückschritt ist R@1 (0,92 statt 1,00) bei einer von 12 Queries
("Positioning a portfolio for a stock market crash by holding bonds", Rang 6–10 statt 1) —
genau der in der Planungsphase gemessene und akzeptierte Trade-off; nDCG/MRR steigen deutlich.

## 2. Durchgeführte Schritte

| Phase | Inhalt | Ergebnis |
|---|---|---|
| P0 | `pg_dump -Fc` (2,88 GB), Schema, RPC-/View-Definitionen, Code-Snapshot, Baseline-Benchmark | `backups/mxbai-migration-20260918/` |
| P1 | `routes.toml` + `gateway.py` auf mxbai, Switchyard neu | 1024 Dim über `/v1/embeddings` verifiziert |
| P2 | `shared.ts`: Modell, Version, Query-Prefix, Truncation, Dim-Guard | `mxbai-v1`, `EMBED_DIM=1024` |
| P3 | Worker: Chunk 800/200, 512-Token-Schutz, korrekte Metadaten | `content` == exakt eingebetteter Text |
| P4 | Query-Prefix an allen 4 Suchstellen (+ Kanalauflösung) | 4 Stellen umgestellt |
| P5 | Spalten `vector(4096)` → `vector(1024)` (Rename-Strategie) | View + **GRANTs** wiederhergestellt |
| P6 | Re-Embed aller YT-Chunks und X-Posts | 259.014 Vektoren, 0 Fehler |
| P7 | HNSW-Indizes | agent_workspace + 3 Nebentabellen |
| P8 | Alte 4096er Spalten gedroppt, `VACUUM FULL` | 2.719 MB → 3.836 MB (jetzt inkl. Index) |
| P9 | qwen3-Modelle entfernt (~7,8 GB frei), Doku aktualisiert | nur noch mxbai aktiv |
| P10 | Abnahme (Schema, Daten, Retrieval, Latenz, Plan, Speicher) | alle Kriterien erfüllt |

## 3. Unterwegs gefundene und behobene Fehler

1. **HTTP 400 statt Truncation (kritisch).** Ollama 0.21.2 lehnt einen Input mit **> 512 Tokens**
   mit `the input length exceeds the context length` ab; es trunkiert nur *manche* Texte still.
   Die Grenze gilt **pro Input** (nicht pro Batch). Dichte Transkripte mit Zeitstempeln
   tokenisieren mit nur ~2,8 Zeichen/Token (gemessen: 1440 Zeichen = 510 Tokens OK,
   1450 Zeichen = HTTP 400). Lösung: `EMBED_MAX_CHARS=1100` + `fitForEmbedding()` **vor** dem
   Hashing, plus `getDocumentEmbeddingsDetailed()`, das einen 400er abfängt, den Text
   schrittweise verkleinert und die *tatsächlich eingebetteten* Texte für `content`/   `source_hash` zurückgibt (0 Hash-Inkonsistenzen).
2. **`hybrid_search_open_brain` war dauerhaft kaputt.** Einzige Vektor-RPC ohne
   `SET search_path TO public, extensions`; PostgREST läuft ohne `extensions` ⇒ jeder Aufruf
   scheiterte an `operator does not exist: extensions.vector <=> extensions.vector`.
   Das MCP-Tool `search_thoughts` hat nie funktioniert (auch vor der Migration nicht).
   Behoben in `migrations/018_...sql`.
3. **GRANTs beim View-Neubau verloren.** `DROP VIEW yt_chunks` löscht die ACLs;
   ohne `GRANT SELECT ... TO anon, service_role, authenticator` schlägt jede Suche über
   PostgREST mit `permission denied for view yt_chunks` fehl. In Migration 014 + Rollback
   ergänzt.
4. **Interaktive Query in der Massen-Lane.** `resolveChannelHandle` embeddete mit der
   Prioritätsklasse `yt`; die Anfrage landete hinter ~150 bereits eingereihten Chunks eines
   Bulk-Videos und wartete gemessen **37 s** (MCP-Timeout). Jetzt `x_search` ⇒ 0,4 s.
5. **Gateway-Flaschenhals Batch-Größe.** Mit batch 2 stand das Backend zeitweise still
   (Queue = 0), während der Worker DB-Writes machte. Klassenspezifische Batches
   (`EMBED_BATCH_SIZE_YT`, interaktiv 2) + `YT_EMBED_CONCURRENCY=4` ⇒ Durchsatz von
   3,0 auf ~5 docs/s. Batch 16 brachte keinen Mehrdurchsatz (0,22 s/Dokument ist flach),
   deshalb bewusst bei 8 geblieben (halbiert die Wartezeit auf den laufenden Call).
6. **`/dev/shm` des DB-Containers = 64 MB.** Der parallele HNSW-Build mit
   `maintenance_work_mem=2GB` scheiterte an `could not resize shared memory segment`.
   Serieller Build (`max_parallel_maintenance_workers=0`, 1 GB) in 2 Minuten erfolgreich.

## 4. Abnahme (P10)

* **Schema:** alle 4 Tabellen + View `vector(1024)`; 4 HNSW-Indizes vorhanden.
* **Daten:** 0 Zeilen ohne Vektor, 0 Stale (`embedding_version <> 'mxbai-v1'`),
  226.916/226.916 YT-Chunk-Hashes konsistent (`sha256(content) == source_hash`),
  alle Vektoren 1024-dim, `embedding_model = mxbai-embed-large`.
* **Retrieval:** R@1 0,9167 · R@5 0,9167 · R@10 1,000 · nDCG@10 0,9397 · MRR 0,9306.
* **Latenz** (leere Queue): `search_youtube_content` 52 ms, `search_influencer_posts` 824 ms,
  `search_thoughts` 35 ms — alle < 2 s.
* **Plan:** `EXPLAIN` zeigt `Index Scan using idx_aw_embedding_hnsw` (auch über die View).
* **Logs:** 0 HTTP-400 (`exceeds the context length`) und 0 HTTP-503 während des gesamten
  Re-Embeds.

## 5. Rollback

* **Vor P8** (nicht mehr möglich, P8 ist ausgeführt): `scripts/rollback_mxbai_migration.sql`
  + `git checkout -- mcp/agent-cco llm-gateway` + qwen3-Modell zurückziehen.
* **Jetzt (nach P8/P9):** Restore aus
  `backups/mxbai-migration-20260918/openbrain_full.dump` (`pg_restore`), qwen3-Modelle
  erneut ziehen, Gateway-Route zurücksetzen, Code aus dem Backup-Stand
  `backups/mxbai-migration-20260918/pre_change_code/` bzw. per `git`.

## 6. Offene Punkte / bewusste Entscheidungen

* **R@1 0,92 statt 1,00** bei einer Query — akzeptierter Modelltrade-off (nDCG/MRR besser).
* **53 Videos ohne Auto-Captions** bleiben dauerhaft in `status='pending'`
  (Selbstreparatur-Backoff, unbegrenzte Wiederholung) — unabhängig von dieser Migration.
* **1 X-Post** (`metadata_failed`, LLM-JSON-Parsefehler) wurde manuell wieder in die
  Embedding-Queue gestellt; die Metadaten des Posts bleiben unvollständig.
* **4 X-Posts `pending_metadata`** sind normale neue Ingestion-Arbeit.
* **nomic-embed-text (274 MB) und bge-large (670 MB)** bleiben als Benchmark-Kandidaten
  in Ollama; sie werden produktiv nicht genutzt.
* **HNSW-Index ist 2,0 GB** (größer als die geplanten ~0,9 GB, weil pgvector die Vektoren im
  Index mitschreibt). Partielle Indizes wurden deshalb bewusst NICHT angelegt.
* **Englisch-only:** deutsche Queries würden schlechter performen. Der Korpus ist durchgehend
  englisch; ein Fallback ist nicht implementiert.
