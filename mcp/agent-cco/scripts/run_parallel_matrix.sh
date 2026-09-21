#!/usr/bin/env bash
# Kontrollierte Parallelitaets-/Batch-Matrix. Stoppt Produktions-Ollama und faehrt einen
# dedizierten Bench-Container (gleiches Modell-Volume). Danach wird der Produktions-Container
# garantiert wieder gestartet (trap). Voraussetzung: CCO-Worker sind gestoppt.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DENO="${DENO_BIN:-$HOME/.deno/bin/deno}"
PROBE="$ROOT/mcp/agent-cco/scripts/embed_parallel_probe.ts"
VOL=openbrain_openbrain_ollama
MODEL="${BENCH_MODEL:-mxbai-embed-large}"
REQ="${BENCH_REQUESTS:-48}"
REPS="${BENCH_REPS:-2}"

restore() { docker start llm-gw-ollama-cpu >/dev/null 2>&1 || true; docker rm -f ollama-bench >/dev/null 2>&1 || true; }
trap restore EXIT
echo "[matrix] stoppe Produktions-Ollama (Isolation)..."
docker stop llm-gw-ollama-cpu >/dev/null

cfg() { # $1=batch $2=conc
  "$DENO" run -A "$PROBE" --url=http://127.0.0.1:11434 --model="$MODEL" --label="$LABEL" --requests="$REQ" --batch="$1" --concurrency="$2" --repeats="$REPS" --chars=800
}
for NP in 1 2 4; do
  docker rm -f ollama-bench >/dev/null 2>&1 || true
  LABEL="NP=$NP"
  docker run -d --name ollama-bench -p 11434:11434 -e OLLAMA_NUM_PARALLEL=$NP -e OLLAMA_KEEP_ALIVE=-1 -e OLLAMA_CONTEXT_LENGTH=4096 -v $VOL:/root/.ollama ollama/ollama >/dev/null
  for i in $(seq 1 60); do curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
  curl -s -o /dev/null -X POST http://127.0.0.1:11434/api/embed -H 'Content-Type: application/json' -d "{\"model\":\"$MODEL\",\"input\":[\"warmup\"],\"keep_alive\":-1}"
  if [ "$NP" = "1" ]; then
    cfg 1 1; cfg 1 4; cfg 1 16; cfg 8 1; cfg 8 16
  else
    cfg 1 1; cfg 1 16; cfg 8 1
  fi
  docker rm -f ollama-bench >/dev/null 2>&1 || true
done
echo "[matrix] fertig."
