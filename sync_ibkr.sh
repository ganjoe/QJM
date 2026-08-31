#!/bin/bash
# Hinweis: Dieses Skript leitet auf import_ibkr_csv.sh weiter.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/import_ibkr_csv.sh" "$@"
