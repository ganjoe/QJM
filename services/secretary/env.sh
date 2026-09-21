#!/usr/bin/env bash
# ============================================================================
# services/secretary/env.sh — Umgebung fuer die Sekretaerin.
#
#   source services/secretary/env.sh
#
# Das DB-Passwort wird zur Laufzeit aus dem Container gelesen und NUR in die
# Umgebung exportiert — es landet nie in einer Datei im Repo.
# ============================================================================

export PGHOST="${PGHOST:-127.0.0.1}"
export PGPORT="${PGPORT:-5433}"
export PGUSER="${PGUSER:-postgres}"
export PGDATABASE="${PGDATABASE:-postgres}"
export PGPASSWORD="$(docker exec openbrain-db printenv POSTGRES_PASSWORD 2>/dev/null)"

# DSH-Anbindung: derselbe Build und dasselbe Zuhause wie die Web-GUI,
# damit die Laeufe dort sichtbar sind.
export SECRETARY_DSH_BIN="${SECRETARY_DSH_BIN:-/home/daniel/.local/bin/dsh}"
export SECRETARY_DSH_HOME="${SECRETARY_DSH_HOME:-/home/daniel/.dsh}"
export SECRETARY_WORKSPACE="${SECRETARY_WORKSPACE:-/home/daniel/QJM}"
export SECRETARY_ROLES_DIR="${SECRETARY_ROLES_DIR:-/home/daniel/QJM/roles}"

if [ -z "$PGPASSWORD" ]; then
  echo "env.sh: konnte das DB-Passwort nicht aus openbrain-db lesen" >&2
  return 1 2>/dev/null || exit 1
fi
