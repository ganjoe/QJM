#!/usr/bin/env bash
# ==============================================================================
# Shelly PowerMeter & Control Hub Launcher
# ==============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Python-Interpreter: lokale venv, sonst die bekannte venv, sonst System-Python
if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
elif [ -x "/home/daniel/powermeter/.venv/bin/python" ]; then
    PYTHON="/home/daniel/powermeter/.venv/bin/python"
else
    PYTHON="python3"
fi

# Port aus .env lesen (Fallback 8800)
PORT="$(grep -E '^POWERMETER_PORT=' "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-8800}"

if command -v ss >/dev/null 2>&1 && ss -ltn | grep -q ":${PORT} "; then
    echo "❌ Port ${PORT} ist bereits belegt – läuft der Dienst schon? Stoppen mit: fuser -k ${PORT}/tcp" >&2
    exit 1
fi

echo "⚡ Starte Shelly PowerMeter Service & Dashboard auf http://0.0.0.0:${PORT} (Ctrl+C beendet)"
exec "$PYTHON" main.py
