#!/usr/bin/env bash
# rs_lab/run_lab.sh — führt ein Lab-Skript in einem wegwerf pca-service-Container aus.
# Isoliertes Testlab: Produktionscode wird NICHT verändert.
#   Nutzung: ./run_lab.sh 01_build_highcap.py [args...]
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMG="${LAB_IMG:-llm-gw-pca-service:latest}"
SCRIPT="$1"; shift || true

SUPABASE_URL="$(docker exec qjm-pca-service printenv SUPABASE_URL)"
SUPABASE_KEY="$(docker exec qjm-pca-service printenv SUPABASE_SERVICE_ROLE_KEY)"

exec docker run --rm \
  --user "$(id -u):$(id -g)" \
  --add-host=host.docker.internal:host-gateway \
  -v "$LAB_DIR":/lab \
  -v /home/daniel/stock-data-node/data/parquet:/parquet:ro \
  -e SUPABASE_URL="$SUPABASE_URL" \
  -e SUPABASE_SERVICE_ROLE_KEY="$SUPABASE_KEY" \
  -e PARQUET_BASE_PATH=/parquet \
  -w /lab \
  "$IMG" python3 "/lab/$SCRIPT" "$@"
