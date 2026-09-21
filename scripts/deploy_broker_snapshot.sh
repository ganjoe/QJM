#!/usr/bin/env bash
# scripts/deploy_broker_snapshot.sh
#
# Deployment des Broker-Truth-Snapshot — Portfolio-Bewertung aus dem Broker-Stand.
#
#   Phase 1: mcp-pta neu bauen + erzwingen  (Tool-Code: Frische-Kontrakt, read-only)
#   Phase 2: ibkr-sync neu bauen            (Daemon: Snapshot periodisch alle 30 s)
#   Phase 3: Migration 025                  (notes-Pflicht fuer Refresh-Auftraege)
#   Phase 4: Abnahme (automatisch)
#
# REIHENFOLGE IST ZWINGEND — Phase 1 MUSS vor Phase 3 laufen.
# Die alte Tool-Version schreibt REFRESH_REQUESTED ohne `notes`. Laeuft sie nach
# der Migration weiter, schlaegt ihr INSERT fehl und list_active_positions bricht
# ab. Das Skript haelt diese Reihenfolge ein.
#
# Aufruf:  bash scripts/deploy_broker_snapshot.sh
# Details: docs/architecture/broker-truth-snapshot.md

set -euo pipefail

DB_CONTAINER="openbrain-db"
DB_USER="postgres"
DB_NAME="postgres"
FRESH_MAX_SEC=120     # Abnahme: Snapshot darf nicht aelter sein
WAIT_MAX_SEC=180      # maximal auf den ersten periodischen Snapshot warten

cd "$(dirname "$0")/.."

echo "=============================================================="
echo " Broker-Truth-Snapshot — Deployment"
echo "=============================================================="

MODE=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c \
    "select value->>'active_mode' from system_settings where key = 'ib_gateway_config';" || true)
MODE=$(echo "${MODE:-live}" | tr -d '[:space:]')
echo "Aktiver Modus: ${MODE}"
echo

# ---------------------------------------------------------------------------
echo "== 1/4  mcp-pta neu bauen + erzwingen (Tool-Code laden) =="
bash restart.sh mcp-pta

# ---------------------------------------------------------------------------
echo
echo "== 2/4  ibkr-sync neu bauen (periodischer Snapshot) =="
( cd ibkr_live_daemon && docker compose up -d --build )

# ---------------------------------------------------------------------------
echo
echo "== 3/4  Migration 025 anwenden (notes-Pflicht, idempotent) =="
docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
    < migrations/025_pta_refresh_requires_notes.sql

# ---------------------------------------------------------------------------
echo
echo "== 4/4  Abnahme =="

echo "-- a) Invariante aktiv? (erwartet: pta_refresh_requires_notes) --"
CONSTRAINT=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c \
    "select conname from pg_constraint where conname = 'pta_refresh_requires_notes';" || true)
CONSTRAINT=$(echo "${CONSTRAINT:-}" | tr -d '[:space:]')
echo "   ${CONSTRAINT:-FEHLT}"

echo "-- b) warte auf einen frischen Snapshot (max ${WAIT_MAX_SEC}s) --"
echo "   (der Live-Reconnect braucht ggf. 2FA im Gateway — ohne Login kommt kein Snapshot)"
DEADLINE=$(( $(date +%s) + WAIT_MAX_SEC ))
AGE=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    AGE=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c \
        "select coalesce(round(extract(epoch from (now() - max(updated_at))))::int, -1) \
           from pta_ibkr_account_summary where mode = '${MODE}';" || true)
    AGE=$(echo "${AGE:- -1}" | tr -d '[:space:]')
    if [ "$AGE" -ge 0 ] && [ "$AGE" -le "$FRESH_MAX_SEC" ]; then
        break
    fi
    sleep 5
done
echo "   Snapshot-Alter: ${AGE}s (Grenze ${FRESH_MAX_SEC}s)"

echo "-- c) Kontostand aus dem Snapshot --"
docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c \
    "select account,
            round(net_liquidation::numeric, 2)     as nav_eur,
            round(total_cash_balance::numeric, 2) as cash_eur,
            round(extract(epoch from (now() - updated_at)))::int as alter_sek
       from pta_ibkr_account_summary
      where mode = '${MODE}';"

echo "-- d) Depotzeilen aus dem Snapshot --"
docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c \
    "select count(*)::int as depotzeilen,
            count(distinct ticker)::int as ticker
       from pta_ibkr_positions
      where mode = '${MODE}';"

echo "-- e) haengende Refresh-Auftraege (erwartet: 0 im Normalfall) --"
docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c \
    "select count(*) from pta_execution_log where event_type = 'REFRESH_REQUESTED';"

echo "-- f) Daemon-Log --"
docker logs --tail 60 ibkr-sync 2>&1 | grep -E "Snapshot \(" | tail -5 \
    || echo "   (noch keine Snapshot-Zeile im Log)"

# ---------------------------------------------------------------------------
echo
echo "=============================================================="
FAIL=0
if [ -z "$CONSTRAINT" ]; then
    echo " FEHLER: Constraint fehlt — Migration nicht angewendet."; FAIL=1
fi
if [ -z "$AGE" ] || [ "$AGE" -lt 0 ]; then
    echo " FEHLER: Kein Snapshot vorhanden. Log pruefen: docker logs ibkr-sync"; FAIL=1
elif [ "$AGE" -gt "$FRESH_MAX_SEC" ]; then
    echo " FEHLER: Snapshot ist ${AGE}s alt — der periodische Schreiber laeuft nicht."
    echo "         Log pruefen: docker logs --tail 100 ibkr-sync"; FAIL=1
fi

if [ "$FAIL" -eq 0 ]; then
    echo " ✅ Automatische Abnahme bestanden."
    echo
    echo " Letzter Schritt von Hand (nicht automatisierbar):"
    echo "   Vergleiche 'nav_eur' oben mit dem Wert, den dein Broker beim Login zeigt."
    echo "   Erwartet: Abweichung < 0,2 % (Rest ist FX-Rundung)."
    echo
    echo "   Danach im Agenten: list_active_positions() aufrufen — die Antwort muss"
    echo "   mit '=== SNAPSHOT: <Zeitpunkt> (Alter: <X>s) — ✅ frisch ===' beginnen."
else
    echo " ❌ Abnahme NICHT bestanden — siehe oben."
fi
echo "=============================================================="

exit "$FAIL"
