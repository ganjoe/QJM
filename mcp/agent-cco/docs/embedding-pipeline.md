# Embedding-Pipeline (CCO)

Stand: 2026-09-18 — **Modellwechsel auf mxbai-embed-large (1024 Dim) abgeschlossen und
abgenommen** (Details: `MXBAI_MIGRATION_REPORT.md` im Repo-Root).

Ergebnis der Migration: 259.014 Vektoren neu berechnet (226.916 YT-Chunks à 800 Zeichen +
32.098 X-Posts), 0 Fehler, 0 stale Zeilen, HNSW-Index aktiv. Abnahme:
R@1 0,9167 · R@5 0,9167 · R@10 1,000 · nDCG@10 0,9397 · MRR 0,9306 (vorher qwen3-8b:
nDCG@10 0,8943 · MRR 0,8634); interaktive Suche 35–824 ms; ~17× schnelleres Embedding.

## 1. Modell & harte Randbedingungen

| Eigenschaft | Wert |
|---|---|
| Modell | `mxbai-embed-large` (Mixedbread, BERT/334M, via Ollama CPU) |
| Dimension | **1024** |
| Kontext | **512 Token** (hart) |
| Sprache | **Englisch** (Korpus und Queries sind englisch) |
| Query-Prefix | `Represent this sentence for searching relevant passages: ` (**nur Queries**) |
| Dokument-Prefix | keiner |
| Vektor-Spalten | `agent_workspace.embedding`, `open_brain.embedding`, `x_users.embedding`, `yt_channels.embedding` — alle `extensions.vector(1024)` |
| Index | HNSW (`vector_cosine_ops`, m=16, ef_construction=64) |

Vorgänger war `qwen3-embedding:8b` (4096 Dim, `embedding_version='v3-qwen3-embedding-8b'`).
Der Wechsel war messbar ~17x schneller pro Zeichen bei praktisch gleichem Retrieval
(Golden-Set) und macht wegen 1024 <= 2000 erstmals einen HNSW-Index möglich.

### 512-Token-Grenze: Ollama antwortet mit HTTP 400 (gemessen 2026-09-18, Ollama 0.21.2)

* Ein Input mit **> 512 Tokens** führt zu
  `HTTP 400 {"error":"the input length exceeds the context length"}`.
  Ollama trunkiert **nicht zuverlässig** — nur manche langen Texte (z. B. stark repetitive)
  werden still auf 512 gekürzt. Verlässlich ist nur `<= 512 Tokens`.
* Die Token-Dichte ist textabhängig: dichte Transkripte mit Zeitstempeln ~**2,8 Zeichen/Token**,
  normale Prosa ~4–5 Zeichen/Token. Messung: 1440 Zeichen Transkript = 510 Tokens (OK),
  1450 Zeichen = HTTP 400.
* Die Grenze gilt **pro Input**, nicht pro Batch (2 x 498 Tokens in einem Request sind OK).
* `OLLAMA_CONTEXT_LENGTH=4096` im Ollama-Container ändert daran nichts; `truncate`/`num_ctx`
  als Request-Option helfen ebenfalls nicht.

**Konsequenz (dreifach abgesichert):**

1. `EMBED_MAX_CHARS=1100` — `fitForEmbedding()` kürzt jeden Dokumenttext **vor dem Hashing**
   an einer Wortgrenze (auch bei 2,8 Zeichen/Token nur ~390 Tokens ⇒ ~25 % Reserve).
2. `getDocumentEmbeddingsDetailed()` fängt einen Kontext-400er ab und versucht den Text
   einzeln mit schrittweise kleinerem Budget (`EMBED_SHRINK_BUDGETS`). Es liefert die
   **tatsächlich eingebetteten Texte zurück** — nur die dürfen gehasht/gespeichert werden.
3. `EMBED_DIM=1024`-Guard: Vektoren falscher Dimension werden verworfen, statt in den Index
   zu laufen (fängt eine falsche Gateway-Route laut ab).

## 2. Textaufbau

* **YT-Chunk** (Dokument): `[Video: "<Titel>" | Kanal: <handle> | Upload: <YYYY-MM-DD>]\n\n<chunk>`
  — dieser Text ist `agent_workspace.content` **und** die Basis von `source_hash`.
  `search_yt_chunks` entfernt den Header für die Anzeige per Regex.
* **X-Post** (Dokument): `Author: <handle>\nTickers: ...\nKeywords: ...\nContent:\n<text>`
* **open_brain** (Dokument): `content`
* **x_users** (Dokument): `username: <u> screen_name: <n> notes: <notes>`
* **yt_channels** (Dokument): `handle: <h> title: <t> notes: <notes>`
* **Query** (Suche): `EMBED_QUERY_PREFIX + Query` über `getQueryEmbedding()` /
  `getQueryEmbeddingsBatch()` (x_tools, youtube_tools inkl. resolveChannelHandle, openbrain_tools).

## 3. Chunking

`YT_CHUNK_SIZE=800`, `YT_CHUNK_OVERLAP=200` (Header ~90 Zeichen ⇒ ~890 Zeichen ≈ 320 Tokens).
Der Chunker bricht bevorzugt an Zeilen-/Satzgrenzen um (`chunkTranscript`).

## 4. Metadaten-Vertrag (`agent_workspace`)

| Spalte | Bedeutung |
|---|---|
| `embedding` | Vektor (1024) |
| `embedding_model` | aus der Gateway-Antwort (Fallback `EMBED_MODEL_NAME`) |
| `embedding_version` | `mxbai-v1` (`EMBED_VERSION`); **jede** Änderung an Modell/Textaufbau bumpen |
| `embedded_at` | Zeitstempel |
| `source_hash` | `sha256(exakt eingebetteter Text)` |
| `status` | `pending_embedding` → `embedded` |

Stale-Report: `scripts/embedding_stale_report.sql` (Version-, Hash-, Dimensions- und
NULL-Vektor-Prüfung). Fortschritt einer Migration: `scripts/migration_progress.sql`.

## 5. Gateway

`llm-gateway/switchyard-config/routes.toml`: `[targets.ollama_embed_cpu] id = "mxbai-embed-large"`.
Es gibt genau **ein** Embedding-Backend und **keinen** Modell-Fallback.
Der Switchyard-Scheduler priorisiert `x_search` (30) > `x_post` (20) > `yt` (10) und bündelt
Backend-Calls mit `EMBED_BATCH_SIZE=2` (kurze in-flight-Blöcke ⇒ niedrige interaktive Latenz).
`EMBED_QUEUE_TIMEOUT=3600` deckt lange Videos ab.

## 6. Betrieb

```bash
# Modell
docker exec llm-gw-ollama-cpu ollama pull mxbai-embed-large

# Gateway neu (nach routes.toml-Änderung)
cd llm-gateway && docker compose up -d --force-recreate switchyard
curl -s -X POST http://127.0.0.1:4000/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"embeddings","input":["hello"]}' | head -c 200   # model=mxbai-embed-large, 1024 Dim

# CCO neu (lädt tools/ + workers/ neu)
docker restart llm-gw-mcp-cco

# Fortschritt / Stale
docker exec -i openbrain-db psql -U postgres -d postgres < mcp/agent-cco/scripts/migration_progress.sql
docker exec -i openbrain-db psql -U postgres -d postgres < mcp/agent-cco/scripts/embedding_stale_report.sql

# Nebentabellen (open_brain/x_users/yt_channels) neu einbetten
docker exec llm-gw-mcp-cco deno run -A /app/scripts/reembed_misc_tables.ts

# Goldset-Benchmark (isoliert, stoppt CCO und stellt ihn per trap wieder her)
bash mcp/agent-cco/scripts/run_embedding_benchmark.sh \
  --models=mxbai-embed-large --sample=60 --max-per-video=2

# Parallel-/Batch-Matrix (stoppt Produktions-Ollama, trap-Restore)
bash mcp/agent-cco/scripts/run_parallel_matrix.sh
```

## 7. Migration (2026-09-18) — Spaltenstrategie

Statt eines Dual-Column-Schalters im Code (`EMBED_COLUMN`) wurden die alten Spalten
**umbenannt** und die neue Spalte heißt wieder `embedding`:

```sql
ALTER TABLE agent_workspace RENAME COLUMN embedding TO embedding_old_4096;
ALTER TABLE agent_workspace ADD COLUMN embedding extensions.vector(1024);
DROP VIEW yt_chunks; CREATE VIEW yt_chunks AS ... aw.embedding ...;   -- Pflicht: RENAME hängt das View sonst an die ALTE Spalte
```

Vorteile: kein Code-Schalter (keine vergessene Schreibstelle), alle RPCs bleiben unverändert
und finden die neue Spalte automatisch, kein Dimensionsfehler gegen alte Vektoren, und
während des Re-Embeds liefern die Hybrid-RPCs weiter Keyword-Treffer.
Rollback vor dem Aufräumen: `scripts/rollback_mxbai_migration.sql`.
Nach erfolgreicher Abnahme: `migrations/016_mxbai_drop_old_columns.sql`.
