#!/usr/bin/env bash
# Isolierter Embedding-Benchmark.
# Stoppt den CCO-Container (damit laufen KEINE Worker/Embedding-Jobs parallel), wartet bis
# die Gateway-Embedding-Queue leer ist, faehrt den Benchmark vom Host aus und startet den
# Container danach garantiert wieder (trap). Es wird nur gelesen; der Produktionsindex
# bleibt unberuehrt.
#
# Verwendung:
#   bash mcp/agent-cco/scripts/run_embedding_benchmark.sh \
#     --models=qwen3-embedding:8b,qwen3-embedding:0.6b --sample=400 --max-per-video=10
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT="${EMB_BENCH_SCRIPT:-$ROOT/mcp/agent-cco/scripts/embedding_benchmark.ts}"
GOLDEN="$ROOT/mcp/agent-cco/scripts/embedding_golden.json"
CACHE="${EMB_BENCH_CACHE:-/tmp/embbench}"
DENO="${DENO_BIN:-$HOME/.deno/bin/deno}"
CONTAINER=llm-gw-mcp-cco

# Supabase-Zugang + URLs fuer Host-Ausfuehrung (nur den benoetigten Key extrahieren)
SUPABASE_SERVICE_ROLE_KEY="$(grep -E '^SUPABASE_SERVICE_ROLE_KEY=' "$ROOT/llm-gateway/.env" | head -1 | sed -E 's/^SUPABASE_SERVICE_ROLE_KEY=//' | tr -d '"')"
export SUPABASE_SERVICE_ROLE_KEY
export SUPABASE_URL="http://127.0.0.1:8001"
export OLLAMA_URL="http://127.0.0.1:11434"

restart_workers() {
  echo "[bench] Starte $CONTAINER wieder (Worker)..."
  docker start "$CONTAINER" >/dev/null 2>&1 || true
}
trap restart_workers EXIT

echo "[bench] Stoppe $CONTAINER (isoliert alle Embedding-Worker)..."
docker stop "$CONTAINER" >/dev/null

echo "[bench] Leere die Gateway-Queue sofort (Switchyard-Neustart)..."
docker restart llm-gw-switchyard >/dev/null
sleep 4
q=$(curl -s -m 5 http://127.0.0.1:4000/embeddings/status 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(sum(d['queue_depth'].values()))" 2>/dev/null || echo "x")
echo "[bench] Queue nach Leerung: $q (Worker sind gestoppt)"

echo "[bench] Starte Benchmark (Worker gestoppt)..."
"$DENO" run -A "$SCRIPT" --golden="$GOLDEN" --cache="$CACHE" --out="$CACHE/result.json" "$@"
