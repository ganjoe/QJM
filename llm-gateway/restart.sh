#!/bin/bash
# ==============================================================================
# LLM Gateway - Restart Script
# 
# Startet alle Container neu und wendet lokale Code-Änderungen an (z.B. im
# Orchestrator), ohne externe Images (wie vLLM oder Ollama) neu herunterzuladen.
# ==============================================================================

cd "$(dirname "$0")" || exit 1

echo "🔄 Stoppe bestehende Container..."
docker compose --profile full down

echo "🏗️ Baue lokale Images neu (falls sich Code geändert hat)..."
# --build erzwingt das Bauen lokaler Dockerfiles (Orchestrator, Ollama-Router)
# Ohne --pull werden keine neuen Versionen von Basis-Images heruntergeladen!
docker compose --profile full build

echo "🚀 Starte alle Services..."
docker compose --profile full up -d

echo ""
echo "✅ Restart abgeschlossen!"
echo "Dashboard: http://localhost:9000"
