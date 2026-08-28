#!/bin/bash
# ==============================================================================
# DeepSeek Harness (DSH) Update & Version Check Script
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/llm-gateway" || exit 1

echo "🔍 Prüfe DSH-Versionen..."

# 1. Installierte Version im laufenden Container ermitteln
CURRENT_VER=$(docker exec llm-gw-dsh node -e 'try{console.log(require("/usr/local/lib/node_modules/@deepseek-ai/dsh/package.json").version)}catch(e){console.log("nicht gefunden")}' 2>/dev/null || echo "Container offline / unbekannt")

# 2. Neueste Version aus der NPM Registry abfragen
REMOTE_VER=$(python3 -c "
import urllib.request, json
try:
    req = urllib.request.Request('https://registry.npmjs.org/@deepseek-ai/dsh', headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        tags = data.get('dist-tags', {})
        print(tags.get('next') or tags.get('latest') or 'unbekannt')
except Exception as e:
    print('Fehler bei Abfrage')
")

echo "--------------------------------------------------"
echo " Aktuell im Container:  $CURRENT_VER"
echo " Neueste Version (NPM): $REMOTE_VER"
echo "--------------------------------------------------"

if [ "$1" = "--check" ] || [ "$1" = "-c" ]; then
    if [ "$CURRENT_VER" != "$REMOTE_VER" ] && [ "$REMOTE_VER" != "Fehler bei Abfrage" ]; then
        echo "⚡ Ein Update ist verfügbar: $CURRENT_VER -> $REMOTE_VER"
        echo "Führe './update_dsh.sh' aus, um das Update zu installieren."
    else
        echo "✅ DSH ist bereits auf dem neuesten Stand ($CURRENT_VER)."
    fi
    exit 0
fi

if [ "$CURRENT_VER" = "$REMOTE_VER" ] && [ "$1" != "--force" ] && [ "$1" != "-f" ]; then
    echo "✅ DSH ist bereits auf dem neuesten Stand ($CURRENT_VER)."
    read -p "Möchtest du das Image trotzdem ohne Cache neu bauen? (j/N) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[JjYy]$ ]]; then
        echo "Abgebrochen."
        exit 0
    fi
fi

echo "🚀 Starte DSH Update (Build ohne Docker Cache)..."
docker compose build --no-cache dsh
docker compose up -d --force-recreate dsh

NEW_VER=$(docker exec llm-gw-dsh node -e 'try{console.log(require("/usr/local/lib/node_modules/@deepseek-ai/dsh/package.json").version)}catch(e){console.log("unbekannt")}' 2>/dev/null || echo "unbekannt")

echo ""
echo "✅ DSH erfolgreich aktualisiert & neu gestartet!"
echo "   Neue aktive Version: $NEW_VER"
echo "   Web-UI erreichbar:   http://localhost:3080"
