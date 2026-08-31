#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_DIR="$SCRIPT_DIR/ibkr_csv_importer"

echo "Starte IBKR CSV Import..."

cd "$SYNC_DIR"

# 1. Parst alle neuen CSVs (falls vorhanden), baut die trades.xml und verschiebt die CSVs in oldcsv/
echo "1. Prüfe auf neue CSV-Dateien..."
if [ -n "$1" ]; then
    python3 csv_parser.py "$1"
else
    shopt -s nullglob
    csv_files=( U16537315*.csv )
    if [ ${#csv_files[@]} -eq 0 ]; then
        echo "Keine neuen CSV-Dateien gefunden."
    else
        echo "Gefundene CSV-Dateien: ${#csv_files[@]}"
        IFS=$'\n' sorted_csvs=($(sort <<<"${csv_files[*]}"))
        unset IFS
        for f in "${sorted_csvs[@]}"; do
            echo "--- Verarbeite: $f ---"
            python3 csv_parser.py "$f"
        done
    fi
fi

# 2. Überträgt die (aktualisierte) trades.xml in die PostgreSQL/Supabase Datenbank
echo ""
echo "2. Übertrage Daten in die Datenbank..."
python3 migrate_xml_to_pg.py

echo ""
echo "CSV-Import erfolgreich abgeschlossen!"
