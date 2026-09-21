# Embedding-Benchmark (Modellwahl)

Ziel: das **kleinste Modell** finden, dessen Retrieval-Qualitaet auf **deinen** Daten nicht
signifikant unter der Referenz liegt — bevor ein teurer Re-Embed startet.

## Ergebnis der Modellwahl (2026-09-18, Entscheidung: mxbai-embed-large)

Fairer Vergleich auf **identischem** Pool (88 YT-Chunks aus dem Golden-Set-Umfeld,
60 Distraktoren, 12 Queries, beide Modelle auf dieselben auf 1100 Zeichen gekuerzten Texte
neu eingebettet, Batch=2 = Produktions-Setting, K=10):

| Modell | Dim | Recall@10 | nDCG@10 | MRR | docs/s | CPU avg/peak |
|---|---|---|---|---|---|---|
| qwen3-embedding:8b (alt) | 4096 | 1.000 | 0.8848 | 0.8472 | 0.217 | 57.6 / 72.4 % |
| **mxbai-embed-large (neu)** | **1024** | **1.000** | **1.000** | **1.000** | **3.808** | 55.1 / 61.2 % |

Auf diesem Set ist mxbai **gleichwertig bis besser** und **~17,5x schneller pro Dokument**.
Zusaetzlich: 1024 <= 2000 Dimensionen ⇒ erstmals **HNSW-Index** moeglich (mit 4096 nicht).
Rohdaten: `backups/mxbai-migration-20260918/benchmark_fair_baseline.log`.

Historische Referenz (vor der Migration, Produktionsindex qwen3-8b, ungekuerzte Chunks,
88 Chunks / 60 Distraktoren): Recall@10 1.000, nDCG@10 0.8943, MRR 0.8634.
Siehe `backups/mxbai-migration-20260918/benchmark_baseline.log`.

## Bausteine
- `scripts/embedding_benchmark.ts` — Harness. Liest nur (PostgREST GET) und rechnet in-memory.
  Embeddet den Pool + die Queries direkt gegen Ollama (`/api/embed`), cached pro Modell unter
  `--cache`. Bewertet auf **Unit-Ebene**: `yt_chunk -> video_id`, `x_post -> id`.
  Metriken: Recall@K, nDCG@K, MRR, Dimension, Durchsatz.
- `scripts/embedding_golden.json` — Seed-Golden-Set (12 echte Queries, 14 Videos). Format:
  `{ "queries": [ { "query": "...", "relevant": ["video_id" | "agent_workspace.id"] } ] }`.
  **Erweitern** lohnt: mehr Queries = stabilere Modellwahl.
- `scripts/run_embedding_benchmark.sh` — Wrapper, der die Isolation garantiert:
  stoppt den CCO-Container (keine Worker), wartet bis die Gateway-Queue leer ist, faehrt den
  Benchmark vom Host und startet den Container danach per `trap` wieder.

## Wichtig: Ollama-Kontextgrenze (HTTP 400)
Ein Input mit **> 512 Tokens** kann `{"error":"the input length exceeds the context length"}`
liefern (Ollama 0.21.2, mxbai-embed-large); die Grenze gilt pro Input, nicht pro Batch.
Dichte Transkripte tokenisieren mit nur ~2,8 Zeichen/Token. Der Harness faengt das ab:
Standard-Batch ist 2, und bei einem Batch-Fehler wird jeder Text einzeln versucht; nicht
einbettbare Texte werden gezaehlt und uebersprungen statt den Lauf abzubrechen.
Fuer Vergleiche auf gleicher Textbasis: `--truncate=1100`.

## Modell-Kandidaten ziehen
```bash
docker exec llm-gw-ollama-cpu ollama pull mxbai-embed-large
docker exec llm-gw-ollama-cpu ollama pull qwen3-embedding:0.6b   # Vergleich
docker exec llm-gw-ollama-cpu ollama pull bge-large              # Vergleich
```

## Lauf (isoliert, empfohlen)
```bash
bash mcp/agent-cco/scripts/run_embedding_benchmark.sh \
  --models=mxbai-embed-large --sample=60 --max-per-video=2 \
  --cache=/tmp/embbench --out=/tmp/embbench/result.json
```
Ergebnis: Tabelle auf stdout + JSON unter `--out`. (Explizite Argumente ueberschreiben die
Defaults des Wrappers, weil das **letzte** passende Argument gewinnt.)

## Manuell (falls kein Container-Stopp gewuenscht)
1. `manage_sync_pipeline` mit `action: "STOP"` (CCO-MCP).
2. Switchyard neu starten, um die Queue zu leeren: `docker restart llm-gw-switchyard`.
3. Pruefen: `curl -s http://127.0.0.1:4000/embeddings/status` -> `queue_depth` = 0 und
   `items_served` steigt nicht mehr.
4. Benchmark fahren (siehe oben).
5. `manage_sync_pipeline` mit `action: "START"`.

## Interpretation
- **Recall@K / nDCG@K / MRR** vergleichen die Modelle relativ.
- Faustregel: nimm das kleinste Modell, dessen **Recall@10 nicht >3-5 % unter** der Referenz liegt.
- Durchsatz/Dimension nur als Zusatzinfo — bei "maximaler Qualitaet" entscheidet Recall.
