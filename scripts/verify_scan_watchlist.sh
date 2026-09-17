#!/usr/bin/env bash
# scripts/verify_scan_watchlist.sh
#
# Abnahmetest der Auto-Watchlist `scan_latest` (Universal Scanner).
# Prueft gegen die laufende PCA-Service-API (Port 8794) genau die drei geforderten Eigenschaften:
#   1. Scan  -> scan_latest enthaelt ALLE Treffer (ohne Cap; die Response selbst bleibt limitiert)
#   2. Scan  -> Replace: der vorherige Stand ist vollstaendig verschwunden
#   3. Scan ohne Treffer -> scan_latest wird geleert (status=cleared)
#
# Nur curl + grep/sed/cut, keine Zusatz-Tools. Aufruf:  bash scripts/verify_scan_watchlist.sh
# Exit-Code 0 = alle Checks bestanden, 1 = mindestens ein Check fehlgeschlagen.

set -uo pipefail

BASE="${PCA_BASE_URL:-http://127.0.0.1:8794}"
PASS=0
FAIL=0

check() { # <beschreibung> <erwartet> <ist>
    if [ "$2" = "$3" ]; then
        printf '  [OK]   %s = %s\n' "$1" "$3"
        PASS=$((PASS + 1))
    else
        printf '  [FAIL] %s: erwartet=%s ist=%s\n' "$1" "$2" "$3"
        FAIL=$((FAIL + 1))
    fi
}

field() { # <json> <key>  -> erster Treffer, ohne Quotes
    printf '%s' "$1" | grep -o "\"$2\":[^,}]*" | head -1 | sed "s/^\"$2\"://" | tr -d '"'
}

wlblock() { # extrahiert das watchlist-Objekt (letztes Feld der Scanner-Response)
    printf '%s' "$1" | grep -o '"watchlist":{.*' | sed 's/.*"watchlist"://' | cut -d'}' -f1
}

scan() { # <json-body>
    curl -s -m 180 -X POST "$BASE/api/scanner/universal" \
        -H 'Content-Type: application/json' -d "$1"
}

watchlist() {
    curl -s -m 60 "$BASE/api/scanner/universal/watchlist"
}

echo "PCA-Basis: $BASE"
if ! curl -s -m 10 "$BASE/health" | grep -q '"status":"ok"'; then
    echo "ABBRUCH: pca-service nicht erreichbar oder nicht healthy."
    exit 1
fi

echo
echo "== Test 1: Scan mit 5 Tickern, limit=2 (Watchlist muss trotzdem alle 5 haben) =="
R1=$(scan '{"tickers":["AAPL","MSFT","NVDA","AMD","META"],"limit":2,"sort_by":"dcr"}')
B1=$(wlblock "$R1")
check "response.count (limit)"        "2"       "$(field "$R1" count)"
check "watchlist.status"              "updated" "$(field "$B1" status)"
check "watchlist.count (ohne Cap)"    "5"       "$(field "$B1" count)"
check "watchlist.matched_total"       "5"       "$(field "$B1" matched_total)"
check "watchlist.truncated"           "true"    "$(field "$B1" truncated)"
W1=$(watchlist)
check "GET watchlist.count"           "5"       "$(field "$W1" count)"
check "GET enthaelt AAPL"             "1"       "$(printf '%s' "$W1" | grep -c 'AAPL')"

echo
echo "== Test 2: Zweiter Scan mit 3 anderen Tickern (Replace, keine Reste) =="
R2=$(scan '{"tickers":["TSLA","NFLX","CRM"],"limit":50,"sort_by":"close"}')
B2=$(wlblock "$R2")
check "watchlist.status"              "updated" "$(field "$B2" status)"
check "watchlist.count"               "3"       "$(field "$B2" count)"
W2=$(watchlist)
check "GET watchlist.count"           "3"       "$(field "$W2" count)"
check "META ist entfernt (0 Treffer)" "0"       "$(printf '%s' "$W2" | grep -c 'META')"
check "AAPL ist entfernt (0 Treffer)" "0"       "$(printf '%s' "$W2" | grep -c 'AAPL')"

echo
echo "== Test 3: Scan ohne Treffer (Liste muss geleert werden) =="
R3=$(scan '{"tickers":["AAPL","MSFT"],"expression":"close < 0","limit":50}')
B3=$(wlblock "$R3")
check "watchlist.status"              "cleared" "$(field "$B3" status)"
check "watchlist.count"               "0"       "$(field "$B3" count)"
W3=$(watchlist)
check "GET watchlist.count"           "0"       "$(field "$W3" count)"

echo
echo "== Schritt 4: Abschluss-Scan (sinnvoller Inhalt statt Testdaten) =="
R4=$(scan '{"expression":"ibd_rs >= 90 AND close > ma_sma_50","limit":50,"sort_by":"ibd_rs"}')
B4=$(wlblock "$R4")
echo "  watchlist: $(field "$B4" status) | Ticker: $(field "$B4" count) von $(field "$B4" matched_total) Treffern"
echo "  Meta:      $(field "$(watchlist)" updated_at)"
echo "  Antwortzeit/Metrik: $(field "$R4" summary)"

echo
echo "== Ergebnis: $PASS bestanden, $FAIL fehlgeschlagen =="
[ "$FAIL" -eq 0 ] || exit 1
