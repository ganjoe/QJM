#!/usr/bin/env bash
# ============================================================================
# wiq_dashboard/dashboard.sh — den Client direkt aus dem Repo starten.
#
#   ./dashboard.sh [--url http://10.20.0.23:8799]
#
# Gedacht fuer den QJM-Host selbst (Diagnose) und zum Entwickeln. Auf Windows
# und macOS laeuft der Client ueber launch/launch_windows.bat bzw.
# launch/launch_macos.command — die holen sich den Code vom Server.
#
# Braucht nur PySide6 (wiq_dashboard/.venv). Datenbank-Zugangsdaten gibt es
# hier nicht: der Client spricht ausschliesslich mit dem Dashboard-Server.
# ============================================================================
set -eo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/client/run_dashboard.py" "$@"
