#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"

if ! command -v deno &> /dev/null; then
    echo "❌ Fehler: 'deno' wurde nicht im PATH gefunden."
    exit 1
fi

deno run -A "$SCRIPT_DIR/scripts/visualize_alpha.ts" "$@"
