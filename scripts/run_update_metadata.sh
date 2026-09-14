#!/usr/bin/env bash
# Wrapper zum einfachen Ausführen des Metadaten-Update-Scripts
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QJM_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON_VENV="/home/daniel/stock-data-node/.venv/bin/python3"

cd "$QJM_DIR"
exec "$PYTHON_VENV" "$SCRIPT_DIR/update_ticker_metadata.py" "$@"
