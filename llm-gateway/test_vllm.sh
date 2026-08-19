#!/usr/bin/env bash
# ============================================
# test_vllm.sh — Testet den vLLM-Container
# ============================================
set -euo pipefail

VLLM_URL="${VLLM_URL:-http://localhost:8100}"

echo "=== LLM Gateway — vLLM Test ==="
echo ""

# --- 1. Health Check ---
echo "1. Health Check..."
if curl -sf "${VLLM_URL}/health" > /dev/null 2>&1; then
    echo "   ✅ vLLM ist gesund"
else
    echo "   ❌ vLLM nicht erreichbar auf ${VLLM_URL}"
    echo "   → Prüfe: docker compose --profile vllm-only logs vllm"
    exit 1
fi

# --- 2. Geladenes Modell abfragen ---
echo ""
echo "2. Geladenes Modell..."
MODELS=$(curl -sf "${VLLM_URL}/v1/models")
echo "   ${MODELS}" | python3 -m json.tool 2>/dev/null || echo "   ${MODELS}"

# --- 3. Einfacher Completion-Test ---
echo ""
echo "3. Chat Completion Test (kurz)..."
RESPONSE=$(curl -sf "${VLLM_URL}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{
        "model": "'"$(echo "${MODELS}" | python3 -c "import sys,json; print(json.load(sys.stdin)['data'][0]['id'])" 2>/dev/null || echo "default")"'",
        "messages": [
            {"role": "user", "content": "Sage Hallo und nenne dein Modell in einem Satz."}
        ],
        "max_tokens": 100,
        "temperature": 0.7
    }')

echo "   Antwort:"
echo "   $(echo "${RESPONSE}" | python3 -c "
import sys, json
r = json.load(sys.stdin)
c = r['choices'][0]
msg = c['message']['content']
usage = r.get('usage', {})
print(f'  {msg[:200]}')
print(f'  ---')
print(f'  Prompt Tokens:     {usage.get(\"prompt_tokens\", \"?\")}')
print(f'  Completion Tokens: {usage.get(\"completion_tokens\", \"?\")}')
print(f'  Total Tokens:      {usage.get(\"total_tokens\", \"?\")}')
" 2>/dev/null || echo "   ${RESPONSE}")"

# --- 4. Metrics abrufen ---
echo ""
echo "4. Metriken (Auszug)..."
METRICS=$(curl -sf "${VLLM_URL}/metrics" 2>/dev/null || echo "nicht verfügbar")
if [ "${METRICS}" != "nicht verfügbar" ]; then
    echo "   $(echo "${METRICS}" | grep -E '^vllm:(num_requests|gpu_cache|avg_generation)' | head -5)"
    echo "   ✅ Prometheus-Metriken verfügbar"
else
    echo "   ⚠️  /metrics Endpoint nicht verfügbar"
fi

echo ""
echo "=== Test abgeschlossen ==="
