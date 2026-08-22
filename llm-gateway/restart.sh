#!/bin/bash
# ==============================================================================
# LLM Gateway - Restart Script (Switchyard + DSH + LM Studio + Dashboard)
# ==============================================================================

cd "$(dirname "$0")" || exit 1

echo "🔄 Stoppe bestehende Container & Orphans..."
docker compose down --remove-orphans
docker stop llm-gw-litellm llm-gw-orchestrator 2>/dev/null || true
docker rm llm-gw-litellm llm-gw-orchestrator 2>/dev/null || true

echo "🏗️ Baue lokale Images (Switchyard, DSH, Dashboard)..."
docker compose build switchyard dsh dashboard

echo "🚀 Starte Kern-Services (Switchyard, DSH, LM Studio, Dashboard)..."
docker compose up -d --remove-orphans switchyard dsh lm-studio dashboard

echo ""
echo "✅ Restart abgeschlossen!"
echo "Switchyard Router:    http://10.20.0.23:4000/v1"
echo "DeepSeek Harness UI:  http://10.20.0.23:3080"
echo "Dashboard Control:    http://10.20.0.23:9000"
