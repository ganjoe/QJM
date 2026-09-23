#!/usr/bin/env bash
# scripts/deploy_order_reconciliation.sh
#
# Deployment des Order-Reconciliation-Fixes (Report
# dsh_playground/report-ibkr-order-reconciliation.md, Verifikation in
# dsh_playground/report-ibkr-order-reconciliation-VERDICT.md).
#
#   Phase 1: mcp-pta neu starten        (Tool-Code ist bind-gemountet)
#   Phase 2: ibkr-sync neu bauen        (Daemon: reqAllOpenOrders, Guards, Read-back)
#   Phase 3: Migration 026              (Read-back-Spalten, STOP_REMOVED, Order-Felder)
#   Phase 4: Abnahme (automatisch)
#
# REIHENFOLGE: Phase 1 MUSS vor Phase 2/3 laufen. Das alte Tool schreibt
# Stop-Loeschungen als stop_loss = 0; der neue Daemon wuerde daraus eine
# Market-Order bauen. Erst das neue Tool, dann der neue Daemon.
#
# Aufruf:  bash scripts/deploy_order_reconciliation.sh

set -euo pipefail

DB_CONTAINER="openbrain-db"
DB_USER="postgres"
DB_NAME="postgres"
SYNC_CONTAINER="ibkr-sync"
MCP_CONTAINER="llm-gw-mcp-pta"

cd "$(dirname "$0")/.."

echo "=============================================================="
echo " Order-Reconciliation — Deployment"
echo "=============================================================="

# ---------------------------------------------------------------------------
echo "== 1/4  mcp-pta neu starten (Tool-Code laden) =="
# Die Tools sind als Volume gemountet -> ein restart genuegt.
docker restart "$MCP_CONTAINER" >/dev/null
sleep 3
docker ps --filter "name=${MCP_CONTAINER}" --format '   {{.Names}} {{.Status}}'

# ---------------------------------------------------------------------------
echo
echo "== 2/4  ibkr-sync neu bauen =="
# BUILDX_CONFIG/DOCKER_CONFIG umleitbar halten, falls ~/.docker nicht schreibbar ist.
( cd ibkr_live_daemon && BUILDX_CONFIG="${BUILDX_CONFIG:-/tmp/dsh-buildx}" \
    DOCKER_CONFIG="${DOCKER_CONFIG:-/tmp/dsh-docker}" docker compose up -d --build --force-recreate )
sleep 10

# ---------------------------------------------------------------------------
echo
echo "== 3/4  Migration 026 anwenden (idempotent) =="
docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
    < migrations/026_pta_order_reconciliation.sql >/dev/null
echo "   angewendet."

# ---------------------------------------------------------------------------
echo
echo "== 4/4  Abnahme =="
FAIL=0
q() { docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c "$1"; }

echo "-- a) Read-back-Spalten vorhanden? (erwartet 4) --"
N=$(q "select count(*) from information_schema.columns
        where table_name='pta_execution_log'
          and column_name in ('broker_status','broker_error_code','broker_error_msg','broker_verified_at');")
echo "   ${N}/4"
[ "${N:-0}" = "4" ] || { echo "   FEHLER: Spalten fehlen."; FAIL=1; }

echo "-- b) Order-Snapshot-Felder vorhanden? (erwartet 6) --"
N=$(q "select count(*) from information_schema.columns
        where table_name='pta_ibkr_open_orders'
          and column_name in ('order_ref','client_id','tif','filled','remaining','parent_id');")
echo "   ${N}/6"
[ "${N:-0}" = "6" ] || { echo "   FEHLER: Order-Felder fehlen."; FAIL=1; }

echo "-- c) View kennt STOP_REMOVED? (erwartet true) --"
V=$(q "select (pg_get_viewdef('public.pta_active_positions'::regclass) like '%STOP_REMOVED%');")
echo "   ${V}"
[ "${V:-f}" = "t" ] || { echo "   FEHLER: View wurde nicht aktualisiert."; FAIL=1; }

echo "-- d) tzdata im Daemon? (erwartet US/Eastern) --"
TZ=$(docker exec "$SYNC_CONTAINER" python -c "from zoneinfo import ZoneInfo; print(ZoneInfo('US/Eastern'))" 2>/dev/null || echo FEHLT)
echo "   ${TZ}"
[ "${TZ}" = "US/Eastern" ] || { echo "   FEHLER: tzdata fehlt — reqExecutions-Replay verliert Ausfuehrungen."; FAIL=1; }

echo "-- e) Daemon holt die vollstaendige Order-Sicht? --"
# Logs EINMAL einsammeln: ein "docker logs | grep -q" bricht unter "set -o
# pipefail" die Pipe vorzeitig ab (SIGPIPE) und meldet faelschlich einen Fehler.
SYNC_LOG=$(docker logs --since 3m "$SYNC_CONTAINER" 2>&1 || true)
echo "$SYNC_LOG" | grep -E 'reqAllOpenOrders\(\): .* bestaetigt' | tail -1 | sed 's/^/   /' || true
if ! echo "$SYNC_LOG" | grep -q 'reqAllOpenOrders'; then
    echo "   FEHLER: reqAllOpenOrders() laeuft nicht."; FAIL=1
fi

echo "-- f) Snapshot aktuell? --"
MODE=$(q "select value->>'active_mode' from system_settings where key='ib_gateway_config';" | tr -d '[:space:]')
AGE=$(q "select coalesce(round(extract(epoch from (now() - max(updated_at))))::int, -1)
           from pta_ibkr_account_summary where mode = '${MODE}';" | tr -d '[:space:]')
echo "   Modus ${MODE}, Snapshot-Alter ${AGE}s"
[ "${AGE:- -1}" -ge 0 ] && [ "${AGE:-9999}" -le 120 ] || { echo "   WARNUNG: Snapshot nicht frisch."; FAIL=1; }

echo "-- g) veraltete Fremd-Orders werden gefiltert? (informativ) --"
echo "   Treffer im Log: $(echo "$SYNC_LOG" | grep -c 'veraltete Fremd-Order' || true)"

echo
echo "=============================================================="
if [ "$FAIL" -eq 0 ]; then
    echo " ✅ Automatische Abnahme bestanden."
    echo
    echo " Letzter Schritt von Hand (Agent/LLM):"
    echo "   list_active_positions() aufrufen und pruefen, dass"
    echo "     - '=== BROKER OPEN ORDERS (from Broker, mode ...) ===' erscheint,"
    echo "     - '=== RECONCILIATION (Broker <-> Ledger) ===' erscheint,"
    echo "     - das Portfolio-Label den aktiven Modus traegt (kein 'LIVE' im PAPER-Modus),"
    echo "     - Stop-Felder als broker-confirmed / none (broker-confirmed) / UNKNOWN erscheinen."
else
    echo " ❌ Abnahme NICHT bestanden — siehe oben."
fi
echo "=============================================================="

exit "$FAIL"
