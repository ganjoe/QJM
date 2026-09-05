#!/bin/bash
# ==============================================================================
# LLM Gateway - Restart Script (Switchyard + DSH + LM Studio + Dashboard)
# ==============================================================================

cd "$(dirname "$0")" || exit 1

SERVICE=$1

if [ -z "$SERVICE" ] || [ "$SERVICE" = "all" ]; then
    echo "🔄 Stoppe bestehende Container & Orphans..."
    docker compose down --remove-orphans
    docker stop llm-gw-litellm llm-gw-orchestrator llm-gw-mcp-pta llm-gw-mcp-pca llm-gw-mcp-cda qjm-pca-service qjm-chart-viewer-server stock-data-node llm-gw-dsh 2>/dev/null || true
    docker rm llm-gw-litellm llm-gw-orchestrator llm-gw-mcp-pta llm-gw-mcp-pca llm-gw-mcp-cda qjm-pca-service qjm-chart-viewer-server stock-data-node llm-gw-dsh 2>/dev/null || true

    echo "🏗️ Baue lokale Images..."
    docker compose build switchyard dsh dashboard mcp-cco mcp-pta pca-service mcp-pca mcp-cda chart-viewer-server

    echo "🚀 Starte Kern-Services..."
    # Lade alle definierten Services hoch (die nicht durch profiles deaktiviert sind)
    docker compose up -d --remove-orphans

    echo ""
    echo "✅ Full Restart abgeschlossen!"
    echo "Switchyard Router:    http://10.20.0.23:4000/v1"
    echo "DeepSeek Harness UI:  http://10.20.0.23:3080"
    echo "Dashboard Control:    http://10.20.0.23:9000"
    echo "CCO MCP Server:       http://10.20.0.23:8788"
    echo "PTA MCP Server:       http://10.20.0.23:8789"
    echo "PCA MCP Server:       http://10.20.0.23:8790"
    echo "CDA MCP Server:       http://10.20.0.23:8795"
    echo "PCA Service (API):    http://10.20.0.23:8794"
    echo "Chart Viewer Server:  ws://10.20.0.23:8765 & http://10.20.0.23:8766"
else
    echo "🔄 Starte Service '$SERVICE' neu..."
    docker compose stop "$SERVICE"
    docker compose build "$SERVICE"
    docker compose up -d --force-recreate "$SERVICE"
    echo "✅ Service '$SERVICE' erfolgreich neu gestartet!"
fi
