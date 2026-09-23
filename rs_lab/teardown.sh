#!/usr/bin/env bash
# Kompletter Teardown des RS-Labs (reversibel, kein Produktionscode bleibt).
set -euo pipefail
LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "1) Supabase (Watchlists + Features + Preset-Member) ..."
"$LAB_DIR/run_lab.sh" teardown.py

echo "2) 1D_lab_rs.parquet aus dem Parquet-Basis-Ordner entfernen ..."
docker run --rm -v /home/daniel/stock-data-node/data/parquet:/parquet \
  llm-gw-pca-service:latest bash -c 'find /parquet -maxdepth 2 -name "1D_lab_rs.parquet" -delete; echo "  parquet-cleaned"'

echo "3) chart_data.py: den markierten 'Lab-RS (Testlab...)' Merge-Block manuell entfernen."
echo "4) Optional: rm -rf $LAB_DIR"
echo "Fertig."
