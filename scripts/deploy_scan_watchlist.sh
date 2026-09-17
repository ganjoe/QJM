#!/usr/bin/env bash
# scripts/deploy_scan_watchlist.sh
#
# Deployment der Auto-Watchlist `scan_latest` (Universal Scanner).
#   Phase 1: Migration 006 (Metadaten-Tabelle) anwenden
#   Phase 2: pca-service neu bauen + erzwingen (laedt scan_watchlist.py, den Hook in
#            universal_scanner.py und die neuen SCAN_WATCHLIST_*-Env-Vars)
#   Phase 3: mcp-pca neu bauen + erzwingen (neue Tool-Ausgabe + Tool-Descriptions)
#
# Nutzt bewusst das repo-eigene restart.sh (docker compose ... --force-recreate im
# Projekt llm-gateway), damit Projektname und Image-Build identisch zum Standard-Weg sind.
#
# Aufruf:  bash scripts/deploy_scan_watchlist.sh

set -euo pipefail

DB_CONTAINER="openbrain-db"
DB_USER="postgres"
DB_NAME="postgres"
PCA_HOST_PORT="8794"

cd "$(dirname "$0")/.."

echo "== 1/6 Migration 006 anwenden =="
docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
    < migrations/006_scan_watchlist_meta.sql

echo "== 2/6 Tabelle pruefen =="
docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c \
    "select column_name || ':' || data_type from information_schema.columns where table_name = 'pca_scan_watchlist_meta' order by ordinal_position;"

echo "== 3/6 pca-service neu bauen + erzwingen =="
bash restart.sh pca-service

echo "== 4/6 mcp-pca neu bauen + erzwingen =="
bash restart.sh mcp-pca

echo "== 5/6 Health- und Smoke-Test =="
sleep 8
echo -n "pca-service /health:                  "
curl -s "http://127.0.0.1:${PCA_HOST_PORT}/health"; echo
echo -n "neuer Watchlist-Endpunkt (HTTP 200):  "
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:${PCA_HOST_PORT}/api/scanner/universal/watchlist"

echo "== 6/6 Abnahmetest (Fill / Replace / Clear) =="
bash scripts/verify_scan_watchlist.sh

echo
echo "Fertig. Stand danach pruefen mit"
echo "  curl -s 'http://127.0.0.1:${PCA_HOST_PORT}/api/scanner/universal/watchlist' | head -c 400"
echo "oder im Agenten: manage_watchlist (LOAD, list_name='scan_latest')."
