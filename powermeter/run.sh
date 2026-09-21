#!/usr/bin/env bash
# ==============================================================================
# Shelly PowerMeter & Control Hub Launcher
# ==============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Check if venv exists
if [ -d "$SCRIPT_DIR/.venv" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
elif [ -d "/home/daniel/powermeter/.venv" ]; then
    PYTHON="/home/daniel/powermeter/.venv/bin/python"
else
    PYTHON="python3"
fi

echo "⚡ Starte Shelly PowerMeter Service & Dashboard auf Port 8798..."
exec "$PYTHON" main.py
