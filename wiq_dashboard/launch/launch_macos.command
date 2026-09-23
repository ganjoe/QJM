#!/usr/bin/env bash
# ============================================================================
# WIQ Dashboard - Client fuer macOS
#
# Holt den Client-Code vom QJM-Server, richtet einmalig eine eigene venv mit
# PySide6 ein und startet das Fenster. Muster: chart_viewer/launch_macos.command
#
#   SERVER_HOST=10.20.0.23 ./launch_macos.command
#   FORCE_SYNC=1 ./launch_macos.command    Code neu laden
#   RESET_VENV=1 ./launch_macos.command    venv neu aufbauen
# ============================================================================
set -u

SERVER_HOST="${SERVER_HOST:-10.20.0.23}"
SERVER="http://${SERVER_HOST}:8799"
CACHE_DIR="$HOME/Library/Application Support/WIQDashboard"
VENV_DIR="$CACHE_DIR/venv"
VENV_PY="$VENV_DIR/bin/python"
VERSION_FILE="$CACHE_DIR/sync_version"

echo "========================================================"
echo "            WIQ Dashboard  (Server: $SERVER)"
echo "========================================================"
echo

mkdir -p "$CACHE_DIR" || { echo "FEHLER: $CACHE_DIR nicht beschreibbar"; exit 1; }

fail() { echo; echo "FEHLER: $1"; echo; read -r -p "Enter zum Beenden..."; exit 1; }

# --- 1/4 Server erreichbar? ------------------------------------------------
echo "[1/4] Pruefe Server..."
curl -s -f -m 8 "$SERVER/health" -o "$CACHE_DIR/health.json" \
  || fail "Server nicht erreichbar unter $SERVER — laeuft der Container qjm-wiq-dashboard auf $SERVER_HOST?"
echo "      [OK]"

# --- 2/4 Code abgleichen ---------------------------------------------------
echo "[2/4] Pruefe Client-Version..."
NEW_VER="$(curl -s -f -m 8 "$SERVER/api/sync_version" || true)"
OLD_VER=""
[ -f "$VERSION_FILE" ] && OLD_VER="$(cat "$VERSION_FILE")"

if [ "${FORCE_SYNC:-0}" = "1" ] || [ "$NEW_VER" != "$OLD_VER" ]; then
    echo "      Lade Client-Code vom Server..."
    curl -s -f -m 30 "$SERVER/api/sync" -o "$CACHE_DIR/client.tar" \
      || fail "Code konnte nicht geladen werden."
    tar -xf "$CACHE_DIR/client.tar" -C "$CACHE_DIR" || fail "Entpacken fehlgeschlagen."
    rm -f "$CACHE_DIR/client.tar"
    printf '%s' "$NEW_VER" > "$VERSION_FILE"
    echo "      [OK] Stand ${NEW_VER:0:12}"
else
    echo "      Code ist aktuell."
fi

# --- 3/4 Python und PySide6 ------------------------------------------------
echo "[3/4] Pruefe Python-Umgebung..."
if [ ! -x "$VENV_PY" ] || [ "${RESET_VENV:-0}" = "1" ]; then
    if command -v python3 >/dev/null 2>&1; then PY_CMD="python3"
    elif command -v python >/dev/null 2>&1; then PY_CMD="python"
    else fail "Kein Python gefunden. Bitte installieren: brew install python"; fi

    [ "${RESET_VENV:-0}" = "1" ] && rm -rf "$VENV_DIR"
    echo "      Erstelle venv (einmalig)..."
    "$PY_CMD" -m venv "$VENV_DIR" || fail "venv konnte nicht erstellt werden."
    "$VENV_PY" -m pip install --quiet --upgrade pip
    "$VENV_PY" -m pip install --quiet "PySide6>=6.5.0" || fail "PySide6 konnte nicht installiert werden."
fi
echo "      [OK]"

# --- 4/4 Starten ------------------------------------------------------------
echo "[4/4] Starte Dashboard..."
echo
"$VENV_PY" "$CACHE_DIR/run_dashboard.py" --url "$SERVER"
echo
echo "Client beendet (Exit-Code $?)."
read -r -p "Enter zum Beenden..."
