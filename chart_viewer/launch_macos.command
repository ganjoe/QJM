#!/usr/bin/env bash

echo "========================================================"
echo "      TC2000-Style Desktop Chart Viewer Client (macOS)"
echo "========================================================"
echo ""

# Wechsel in das Verzeichnis des Skripts
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

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

# 2. Auto-Sync from Linux Server
echo "[1/3] Synchronisiere neueste Version vom Server (http://10.20.0.23:8766)..."
if curl -s -f -m 3 "http://10.20.0.23:8766/api/sync" -o "src_bundle.tar"; then
    tar -xf "src_bundle.tar"
    rm -f "src_bundle.tar"
    echo "[OK] Code erfolgreich auf den neuesten Stand aktualisiert!"
else
    echo "[INFO] Server-Sync uebersprungen - Server offline oder unveraendert."
fi
echo ""

# 3. Check Dependencies
echo "[2/3] Pruefe Python-Abhaengigkeiten (PySide6, msgspec, websockets)..."
if ! $PY_CMD -c "import PySide6, msgspec, websockets" &>/dev/null; then
    echo "Installiere erforderliche Pakete (PySide6, msgspec, websockets)..."
    $PY_CMD -m pip install PySide6 msgspec websockets
    if [ $? -ne 0 ]; then
        echo ""
        echo "Fehler bei der Installation der Abhaengigkeiten."
        read -p "Drücke Enter zum Beenden..."
        exit 1
    fi
fi

# 4. Run App
echo "[3/3] Starte Desktop Chart Viewer Client..."
echo "Verbinde mit ws://10.20.0.23:8765..."
echo ""

export PYTHONPATH="$DIR/src:$PYTHONPATH"
$PY_CMD "$DIR/src/chart_viewer/run_viewer.py" --ws ws://10.20.0.23:8765
EXIT_CODE=$?

echo ""
echo "Viewer-Prozess beendet (Exit-Code: $EXIT_CODE)."
read -p "Drücke Enter zum Beenden..."
