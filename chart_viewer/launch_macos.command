#!/usr/bin/env bash

echo "========================================================"
echo "      TC2000-Style Desktop Chart Viewer Client (macOS)"
echo "========================================================"
echo ""

# Lokales Cache-Verzeichnis (lokale Platte, nicht die Netzlaufwerk-Freigabe)
CACHE_DIR="$HOME/Library/Application Support/ChartViewer"
mkdir -p "$CACHE_DIR"

# 1. Check for python3
if command -v python3 &>/dev/null; then
    PY_CMD="python3"
elif command -v python &>/dev/null; then
    PY_CMD="python"
else
    echo "========================================================"
    echo "FEHLER: Kein funktionierendes Python (python3 oder python) gefunden."
    echo "Bitte installiere Python (z.B. via Homebrew: brew install python)."
    echo "========================================================"
    read -p "Drücke Enter zum Beenden..."
    exit 1
fi

echo "Verwende Python: $(which $PY_CMD)"
echo ""

# 2. Auto-Sync from Linux Server (versioniert - bei unveraendertem Stand uebersprungen)
echo "[1/3] Pruefe Server-Version (http://10.20.0.23:8766)..."
SYNC_NEEDED=1
NEW_VER=""
if curl -s -f -m 5 "http://10.20.0.23:8766/api/sync_version" -o "$CACHE_DIR/sync_version.new"; then
    NEW_VER="$(cat "$CACHE_DIR/sync_version.new")"
    rm -f "$CACHE_DIR/sync_version.new"
fi

if [ -n "$NEW_VER" ]; then
    OLD_VER=""
    if [ -f "$CACHE_DIR/sync_version" ]; then
        OLD_VER="$(cat "$CACHE_DIR/sync_version")"
    fi
    if [ "$NEW_VER" = "$OLD_VER" ]; then
        echo "[OK] Code bereits aktuell - Sync uebersprungen."
        SYNC_NEEDED=0
    fi
fi

if [ "$SYNC_NEEDED" = "1" ]; then
    echo "[1/3] Lade neueste Version vom Server..."
    if curl -s -f -m 20 "http://10.20.0.23:8766/api/sync" -o "$CACHE_DIR/src_bundle.tar"; then
        tar -xf "$CACHE_DIR/src_bundle.tar" -C "$CACHE_DIR"
        rm -f "$CACHE_DIR/src_bundle.tar"
        if [ -n "$NEW_VER" ]; then
            echo "$NEW_VER" > "$CACHE_DIR/sync_version"
        fi
        echo "[OK] Code erfolgreich auf den neuesten Stand aktualisiert!"
    else
        rm -f "$CACHE_DIR/src_bundle.tar"
        echo "[INFO] Server-Sync uebersprungen - Server offline."
    fi
fi
echo ""

# 3. Check Dependencies (schnell via find_spec, ohne PySide6-Vollimport)
echo "[2/3] Pruefe Python-Abhaengigkeiten (PySide6, msgspec, websockets)..."
if ! $PY_CMD -c "import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ('PySide6','msgspec','websockets')) else 1)" &>/dev/null; then
    echo "Installiere erforderliche Pakete (PySide6, msgspec, websockets)..."
    $PY_CMD -m pip install PySide6 msgspec websockets
    if [ $? -ne 0 ]; then
        echo ""
        echo "Fehler bei der Installation der Abhaengigkeiten."
        read -p "Drücke Enter zum Beenden..."
        exit 1
    fi
fi

# 4. Run App (lokal aus dem Cache)
echo "[3/3] Starte Desktop Chart Viewer Client..."
echo "Verbinde mit ws://10.20.0.23:8765..."
echo ""

export PYTHONPATH="$CACHE_DIR/src:$PYTHONPATH"
$PY_CMD "$CACHE_DIR/src/chart_viewer/run_viewer.py" --ws ws://10.20.0.23:8765
EXIT_CODE=$?

echo ""
echo "Viewer-Prozess beendet (Exit-Code: $EXIT_CODE)."
read -p "Drücke Enter zum Beenden..."
