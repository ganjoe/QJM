#!/usr/bin/env bash
set -e

# Verzeichnis dieses Skripts ermitteln
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

# Prüfen, ob Deno installiert ist
if ! command -v deno &> /dev/null; then
    echo "❌ Fehler: 'deno' wurde nicht im PATH gefunden."
    echo "Bitte installiere Deno oder stelle sicher, dass es verfügbar ist."
    exit 1
fi

# Hilfe anzeigen, wenn keine Argumente übergeben wurden
if [ $# -eq 0 ]; then
    echo "========================================="
    echo "   🚀 X Alpha-Influencer Finder 🚀       "
    echo "========================================="
    echo "Verwendung:"
    echo "  ./run_alpha_finder.sh <x_username> [Optionen]"
    echo ""
    echo "Optionen:"
    echo "  --batch-pause <N>     Pausiert nach jeweils N Abfragen zur Bestätigung (Standard: 100)"
    echo "  --max-results <N>     Max. Followings pro Influencer abrufen (Standard: 1000)"
    echo "  --delay-ms <N>        Pause zwischen API-Requests in ms (Standard: 2500)"
    echo "  --top <N>             Anzahl der angezeigten Top-Accounts (Standard: 20)"
    echo "  --help                Vollständige Hilfe anzeigen"
    echo ""
    echo "Beispiel:"
    echo "  ./run_alpha_finder.sh elonmusk"
    echo "  ./run_alpha_finder.sh dein_account --batch-pause 50 --top 25"
    echo "========================================="
    exit 1
fi

# Skript ausführen und alle CLI-Parameter weiterleiten
deno run -A "$SCRIPT_DIR/scripts/find_alpha_influencer.ts" "$@"
