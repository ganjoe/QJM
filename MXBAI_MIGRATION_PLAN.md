# MXBAI-MIGRATION — Implementierungsplan (Handoff für neue Session)

Stand: 2026-09-18. Ziel: das gesamte Embedding-System von qwen3-embedding:8b (4096 Dim) auf mxbai-embed-large (1024 Dim) umstellen.

---

## 0. Entscheidung & Begründung (gemessene Werte)

- Korpus ist rein englisch (Queries + Inhalte) -> englischer Encoder sinnvoll.
- Qualität (harter Chunk-Test: 74 Videos, 60 Distraktoren, R@1/@5/@10):
  mxbai R@1 0,92 / nDCG 0,97 vs. qwen3-8b (DB-Referenz) R@1 1,00 / nDCG 0,91.
  Auf diesem Set praktisch gleichwertig; qwen3-0.6b war R@1 1,00, nomic klar schwächer (0,67-0,75).
- Speed (kontrolliert, isoliert, Produktions-Ollama gestoppt):
  mxbai ~8,5 docs/s bei 800 Zeichen; qwen3-8b ~0,29 docs/s bei 1588 Zeichen.
  -> pro Zeichen ~17x schneller, pro Chunk ~30x. Voll-Re-Embed ~8-9 h statt ~8-10 Tage.
- Speicher (current corpus: 135 Mio. Transcript-Zeichen, 3.844 Videos, 31.833 X-Posts):
  1024 Dim + Chunks 800 -> ~1,45 GiB (ohne Index), ~2,3 GiB (mit HNSW). Heute: 2,65 GiB (4096 Dim).
  1024 <= 2000 -> HNSW möglich; 4096 nicht.
- Parallelität (kontrolliert): NUM_PARALLEL 1/2/4 und Client-Concurrency 1/4/16 und Batch 1/8
  liefern alle ~8,0-8,6 docs/s. CPU nur ~55-65 %. Also: NUM_PARALLEL=1 optimal, Batch egal.
  EMBED_BATCH_SIZE=2 (aktuell) ist für mxbai gratis und hält die interaktive Latenz niedrig.

---

## 1. Harte Randbedingungen

- mxbai Kontext = 512 Tokens -> YT-Chunks müssen auf <= ~800 Zeichen. Ollama wirft sonst HTTP 400
  ("input length exceeds the context length"); es trunkiert NICHT automatisch.
- Nur Englisch. (Deutsche Queries würden ausfallen.)
- Query-Prefix erforderlich: "Represent this sentence for searching relevant passages: " (nur Queries,
  Dokumente roh).
- Modellwechsel = vollständiger Re-Embed; kein Mischbetrieb verschiedener Modelle im selben Index.
- Alle Vektor-Spalten sind typisiert vector(4096) -> müssen auf vector(1024).

---

## 2. Zielzustand

- Modell: mxbai-embed-large (Ollama), 1024 Dim.
- Chunks: YT_CHUNK_SIZE=800, YT_CHUNK_OVERLAP=200.
- embedding_model="mxbai-embed-large", embedding_version="mxbai-v1".
- source_hash = sha256(exakt eingebetteter Text) (Mechanik existiert bereits).
- Spalten: agent_workspace.embedding, open_brain.embedding, x_users.embedding, yt_channels.embedding
  alle vector(1024); HNSW-Index (vector_cosine_ops).
- Query-Prefix an allen Query-Embedding-Stellen.
- RPCs bleiben funktional unverändert (Parameter extensions.vector ohne Dimension).

---

## 3. Betroffene Objekte (vollständig)

### 3.1 DB-Spalten (vector, aktuell 4096)
- agent_workspace.embedding — 132.473 Zeilen (Hauptposten)
- open_brain.embedding — 43 Zeilen, 39 Vektoren
- x_users.embedding — klein
- yt_channels.embedding — 11 Zeilen, 10 Vektoren
- View yt_chunks — spiegelt agent_workspace.embedding (nach Spaltenänderung neu erstellen)

### 3.2 RPCs mit extensions.vector (Signaturen NICHT ändern, nur Spaltenname bleibt "embedding")
- search_yt_chunks
- hybrid_search_workspace (2 Overloads)
- semantic_search_workspace (2 Overloads)
- hybrid_search_open_brain
- search_yt_channels
- search_influencers
- exact_search_workspace (kein Vektor)

### 3.3 Query-Embedding-Stellen (Prefix nötig)
- mcp/agent-cco/tools/x_tools.ts — search_influencer_posts (getEmbedding(trimmedQuery, "x_search"))
- mcp/agent-cco/tools/youtube_tools.ts — search_youtube_content (getEmbedding(query, "x_search"))
- mcp/agent-cco/tools/youtube_tools.ts — resolveChannelHandle (getEmbeddingsBatch([channel], "yt"))
- mcp/agent-cco/tools/openbrain_tools.ts — search_thoughts (getEmbedding(query, "x_search"))

### 3.4 Dokument-Embedding-Stellen (KEIN Prefix)
- mcp/agent-cco/workers/yt_ingestion_worker.ts (YT-Chunks)
- mcp/agent-cco/workers/embedding_worker.ts (X-Posts)
- mcp/agent-cco/tools/openbrain_tools.ts — capture_thought
- mcp/agent-cco/tools/youtube_tools.ts — manage_youtube_channels ADD
- mcp/agent-cco/tools/x_tools.ts — manage_influencers ADD (Profil-Embedding)
- mcp/agent-cco/tools/web_tools.ts — web_download_report
- mcp/agent-cco/scripts/repair_null_embeddings.ts

### 3.5 Gateway / Config / Doku
- llm-gateway/switchyard-config/routes.toml — [targets.ollama_embed_cpu] id = "qwen3-embedding:8b"
  -> "mxbai-embed-large"; Kommentare über 4096/qwen3 anpassen.
- llm-gateway/docker-compose.yml — EMBED_BATCH_SIZE=2, EMBED_QUEUE_TIMEOUT=3600 (beides bereits gesetzt),
  OLLAMA_NUM_PARALLEL=1 (behalten).
- mcp/agent-cco/tools/shared.ts — EMBED_MODEL_NAME, EMBED_VERSION, Query-Prefix-Helper.
- mcp/agent-cco/workers/yt_ingestion_worker.ts — Chunk-Defaults / Versionsnutzung.
- mcp/agent-cco/docs/embedding-pipeline.md — aktualisieren (Modell, 1024 Dim, HNSW).
- mcp/agent-cco/scripts/embedding_stale_report.sql — Version auf mxbai-v1 anpassen.

---

## 4. Bereits erledigt (nicht erneut bauen)

- Isolation-Wrapper (stoppt CCO-Container, leert Queue, garantiertes Restore per trap):
  mcp/agent-cco/scripts/run_embedding_benchmark.sh (unterstützt EMB_BENCH_SCRIPT für beliebige Skripte).
- Benchmark + Golden-Set: mcp/agent-cco/scripts/embedding_benchmark.ts, embedding_golden.json.
- Chunk-Experiment: mcp/agent-cco/scripts/embedding_chunk_experiment.ts (Best-Window, R@1/@5).
- Parallel-/Batch-Probe + kontrollierte Matrix: embed_parallel_probe.ts, run_parallel_matrix.sh.
- X-Hybrid-Suche: migrations/013_hybrid_search_workspace.sql + x_tools.ts (RRF).
- Vektor-Metadaten für X-Posts + zentrale Version/Hash: shared.ts (EMBED_VERSION, sha256Hex,
  getEmbeddingsBatchDetailed), embedding_worker.ts.
- Guards gegen Teil-/Fehlantworten in embedding_worker.ts und yt_ingestion_worker.ts.
- Null-Vektor-Reparatur: scripts/repair_null_embeddings.ts (6 Chunks bereits repariert).
- Llm-Gateway-Latenz: EMBED_BATCH_SIZE=2, EMBED_QUEUE_TIMEOUT=3600.
- P0-1-Trading-Fix: mcp/agent-pta/tools/pta.ts (EXIT-Richtung modus-sicher).

---

## 5. Phasenplan

### P0 — Preflight & Backup
- docker exec llm-gw-ollama-cpu ollama pull mxbai-embed-large  (bereits vorhanden)
- DB-Backup der betroffenen Tabellen (mind. agent_workspace.embedding, open_brain, x_users, yt_channels).
- Snapshot der aktuellen Benchmark-Zahlen (8B-Referenz) als Abnahmebasis.
- Sicherstellen: CCO-Worker stoppen für alle Benchmarks (manage_sync_pipeline STOP), Queue 0.

### P1 — Modell am Gateway umstellen
- routes.toml: [targets.ollama_embed_cpu] id = "mxbai-embed-large".
- docker compose up -d --force-recreate switchyard.
- Verifizieren: POST /v1/embeddings {model:"embeddings", input:["x"]} -> Antwort model = "mxbai-embed-large",
  Dimension 1024.
- ACHTUNG: erst nach P2/P3 produktiv nutzen, sonst schreiben Tools 1024er in die 4096er-Spalte (Fehler).

### P2 — shared.ts
- EMBED_MODEL_NAME = "mxbai-embed-large".
- EMBED_VERSION = "mxbai-v1".
- Konstante QUERY_PREFIX = "Represent this sentence for searching relevant passages: ".
- Neuer Helper getQueryEmbedding(text, priority) = getEmbedding(QUERY_PREFIX + text, priority).
  (Alternativ getEmbeddingsBatch um einen role-Parameter "query"|"document" erweitern.)

### P3 — Worker
- yt_ingestion_worker.ts: liest YT_CHUNK_SIZE (Compose auf 800 setzen) und YT_CHUNK_OVERLAP=200;
  nutzt EMBED_MODEL_NAME/EMBED_VERSION aus shared.ts; header = [Video: ... | Kanal: ... | Upload: <date>].
- embedding_worker.ts: schreibt embedding_model (aus Gateway-Antwort), embedding_version=mxbai-v1,
  embedded_at, source_hash (bereits implementiert).
- WICHTIG: 512-Token-Grenze abfangen. Lange X-Posts / open_brain-Inhalte können HTTP 400 auslösen.
  Lösung: Vor dem Embedden auf ~1500 Zeichen kürzen (Content-Teil) ODER pro Request splitten und
  mean-poolen. Entscheidung + Guard in embedding_worker.ts einbauen.
  (Betrifft besonders open_brain capture_thought mit langen Notizen und lange X-Posts.)

### P4 — Query-Prefix verdrahten
- Die 4 Stellen aus 3.3 auf getQueryEmbedding(...) umstellen.
- Dokument-Stellen (3.4) unverändert lassen.
- Verifizieren mit einer Suche, dass Ergebnisse plausibel bleiben.

### P5 — DB-Schema additiv
- Neue Spalten anlegen (kein Datenverlust, alter Index bleibt während des Re-Embeds nutzbar):
  ALTER TABLE agent_workspace ADD COLUMN embedding_new vector(1024);
  ALTER TABLE open_brain      ADD COLUMN embedding_new vector(1024);
  ALTER TABLE x_users         ADD COLUMN embedding_new vector(1024);
  ALTER TABLE yt_channels     ADD COLUMN embedding_new vector(1024);
  NOTIFY pgrst, 'reload schema';
- Code schreibt während P6 über eine Env-Variable EMBED_COLUMN=embedding_new in die neue Spalte
  (Update-Aufrufe entsprechend parametrisieren; Default "embedding").

### P6 — Re-Embed
- YT: alle eingebetteten Videos auf downloaded/zurückstellen:
  UPDATE yt_videos SET status='downloaded', retry_count=0, next_retry_at=now()
   WHERE status='embedded';
  Der Worker chunkt dann mit 800 neu (delete/insert je video_id) und embeddet mit mxbai.
- X-Posts: status auf pending_embedding zurücksetzen (analog, z. B. per SQL-Update), Worker embeddet neu.
- open_brain (43), yt_channels (11), x_users: über die bestehenden Tools/Worker neu embedden
  oder per Einmal-Skript (Muster: repair_null_embeddings.ts).
- Fortschritt/Störungen beobachten; keine 400er in den Logs (sonst P3-Truncation nachziehen).
- Dauer grob 8-9 h (bei ~8,5 docs/s, ~257k Vektoren), im Hintergrund mit Prioritätsklassen.

### P7 — HNSW-Index (nach dem Cutover sinnvoll, kann aber auf embedding_new vorbereitet werden)
- CREATE INDEX idx_aw_embedding_hnsw ON agent_workspace
    USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
- Optional zwei partielle Indizes (artifact_type='yt_chunk' bzw. 'x_post').
- maintenance_work_mem hochsetzen; Build-Zeit/RAM einplanen.
- Analog für open_brain, x_users, yt_channels (klein).

### P8 — Cutover (kurze Transaktion; ab hier kein einfacher Rollback mehr)
BEGIN;
DROP VIEW yt_chunks;
ALTER TABLE agent_workspace DROP COLUMN embedding;
ALTER TABLE agent_workspace RENAME COLUMN embedding_new TO embedding;
ALTER TABLE open_brain      DROP COLUMN embedding;
ALTER TABLE open_brain      RENAME COLUMN embedding_new TO embedding;
ALTER TABLE x_users         DROP COLUMN embedding;
ALTER TABLE x_users         RENAME COLUMN embedding_new TO embedding;
ALTER TABLE yt_channels     DROP COLUMN embedding;
ALTER TABLE yt_channels     RENAME COLUMN embedding_new TO embedding;
CREATE OR REPLACE VIEW yt_chunks AS ( ... aw.embedding ... );
NOTIFY pgrst, 'reload schema';
COMMIT;
- Danach EMBED_COLUMN zurück auf "embedding" setzen und CCO neu starten.
- Indizes auf embedding neu aufbauen (P7), da der Rename den Index auf embedding_new mitnimmt;
  ggf. vorher auf embedding_new anlegen und nach dem Rename umbenennen.

### P9 — Cleanup
- qwen3-embedding:8b / 4b / 0.6b in Ollama entfernen (ollama rm), falls nicht anderweitig gebraucht.
- Alte 4096-Vektoren sind durch P8 weg; Speicher prüfen (Ziel ~1,45 GiB).
- docs/embedding-pipeline.md und routes.toml-Kommentare auf mxbai/1024/HNSW aktualisieren.
- embedding_stale_report.sql auf embedding_version mxbai-v1 anpassen.

### P10 — Verifikation / Abnahme
- Schema: alle 4 Spalten vector(1024); yt_chunks-View korrekt; HNSW-Index vorhanden.
- Daten: keine NULL-Vektoren; embedding_model=mxbai-embed-large; embedding_version=mxbai-v1;
  source_hash gesetzt (sha256 des eingebetteten Textes).
- Retrieval: Golden-Set R@1/@5/@10 (embedding_benchmark.ts / run_embedding_benchmark.sh) gegen die
  dokumentierten 8B-Werte; keine Regression > 3-5 %.
- Latenz: interaktive Suche (search_youtube_content, search_influencer_posts, search_thoughts) < 2 s
  bei leerer Queue.
- Index: EXPLAIN zeigt Index Scan (HNSW) statt Seq Scan.
- Speicher: pg_total_relation_size ~1,45 GiB (+ Index).
- 0 HTTP-400/503 in den Logs während eines Lasttests.

### P11 — Rollback
- Vor P8: EMBED_COLUMN zurück auf "embedding", Gateway-Route auf qwen3-embedding:8b zurück,
  embedding_new droppen. Alter Zustand intakt.
- Nach P8: Restore aus dem P0-Backup (Spalten/Tabellen).

---

## 6. SQL-Skizzen

Neue Spalten (P5):
    ALTER TABLE agent_workspace ADD COLUMN embedding_new vector(1024);
    ALTER TABLE open_brain      ADD COLUMN embedding_new vector(1024);
    ALTER TABLE x_users         ADD COLUMN embedding_new vector(1024);
    ALTER TABLE yt_channels     ADD COLUMN embedding_new vector(1024);
    NOTIFY pgrst, 'reload schema';

HNSW (P7):
    SET maintenance_work_mem = '2GB';
    CREATE INDEX idx_aw_embedding_hnsw ON agent_workspace
      USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

Cutover (P8): siehe oben.

Re-Embed-Trigger YT (P6):
    UPDATE yt_videos SET status='downloaded', retry_count=0, next_retry_at=now()
     WHERE status='embedded';

Stale-Report (P10):
    SELECT embedding_version, count(*),
           count(*) FILTER (WHERE source_hash = encode(sha256(convert_to(content,'UTF8')),'hex'))
    FROM agent_workspace WHERE artifact_type='yt_chunk' AND embedding IS NOT NULL GROUP BY 1;

---

## 7. Betrieb / Befehle

- Modell: docker exec llm-gw-ollama-cpu ollama pull mxbai-embed-large
- Gateway neu: cd llm-gateway && docker compose up -d --force-recreate switchyard
- CCO neu: docker restart llm-gw-mcp-cco
- Worker-Isolation + Benchmark:
    bash mcp/agent-cco/scripts/run_embedding_benchmark.sh --models=... (stoppt Worker, leert Queue)
- Parallele kontrollierte Matrix:
    bash mcp/agent-cco/scripts/run_parallel_matrix.sh (stoppt Produktions-Ollama, trap-Restore)
- Vor jedem Benchmark: manage_sync_pipeline STOP; danach START.
- Host-Grundlast prüfen: mpstat 1 3  (idle sollte > 85 % sein).

---

## 8. Risiken & offene Fragen

1. 512-Token-Grenze: lange X-Posts/open_brain-Notizen können HTTP 400 werfen. Muss in P3 behandelt werden
   (Kürzen/Splitten). Nicht unterschätzen.
2. Englisch-only: falls je deutsche Queries kommen, fällt mxbai aus. Fallback-Strategie definieren.
3. HNSW-Build: RAM/Zeit bei ~225k x 1024 Dim; ggf. maintenance_work_mem und Zeitfenster einplanen.
4. Cutover P8 ist der Point of no return -> Backup zwingend.
5. Dual-Column-Ansatz erfordert ein sauberes EMBED_COLUMN-Schalter im Code (P5); sonst Maintenance-Fenster
   mit zwischenzeitlich degradierter Suche.
6. Offene Frage: Sollen open_brain/yt_channels wegen langer Texte auf einem Long-Context-Modell bleiben
   (z. B. nomic 2048) oder auch mxbai (mit Kürzen)?
7. Offene Frage: Re-Embed aller X-Posts nötig, oder reicht ein Teil (nur neuere)? Version-Wechsel markiert
   sonst alle als stale.

---

## 9. Referenz-Dateien (im Repo)

- mcp/agent-cco/tools/shared.ts
- mcp/agent-cco/workers/embedding_worker.ts, workers/yt_ingestion_worker.ts
- mcp/agent-cco/tools/x_tools.ts, youtube_tools.ts, openbrain_tools.ts, web_tools.ts
- mcp/agent-cco/scripts/embedding_benchmark.ts, embedding_golden.json, embedding_chunk_experiment.ts,
  embed_parallel_probe.ts, repair_null_embeddings.ts, embedding_stale_report.sql,
  run_embedding_benchmark.sh, run_parallel_matrix.sh
- mcp/agent-cco/docs/embedding-benchmark.md, docs/embedding-pipeline.md
- migrations/013_hybrid_search_workspace.sql
- llm-gateway/switchyard-config/routes.toml, llm-gateway/docker-compose.yml
- mcp/agent-pta/tools/pta.ts (P0-1-Fix, nicht Teil dieser Migration)

---

## 10. Nächster konkreter Schritt für die neue Session

1. P0 lesen, Backup ziehen, mxbai prüfen.
2. P1 (routes.toml) + P2 (shared.ts) + P3 (Worker/Truncation) + P4 (Query-Prefix) implementieren.
3. P5 (neue Spalten) + P6 (Re-Embed im Hintergrund) starten.
4. P7/P8/P9 nach erfolgreichem Re-Embed.
5. P10-Abnahme mit dem Golden-Set; erst dann qwen3 entfernen.
