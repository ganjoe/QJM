#!/usr/bin/env bash
# =============================================================================
# dsh_snapshot.sh - verifizierter Snapshot des DSH-Homes
#
# Sichert alles, was "meine Installation inkl. aller Chats und Konfigs" ausmacht:
# Sessions (Chats), Settings, Credentials, Home-/Profil-Patches, Storages,
# Attachments, Profile, Presets-Verweise - also ~/.dsh komplett.
#
# Zusaetzlich: Manifest (Datei + Groesse), SHA-256, Zustandsprotokoll und eine
# Restore-Anleitung. Der Snapshot wird nach dem Schreiben testweise entpackt
# und gegen das Manifest geprueft.
#
# Aufruf:
#   dsh_snapshot.sh [--dest-root DIR] [--keep N] [--no-verify] [--quiet]
# =============================================================================
set -Eeuo pipefail

DSH_HOME_DIR="/home/daniel/.dsh"
CHECKOUT="/home/daniel/deepseek-harness"
DEST_ROOT="/home/daniel/QJM/backups/dsh_state"
KEEP=5
VERIFY=1
QUIET=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dest-root) DEST_ROOT="$2"; shift 2 ;;
    --keep) KEEP="$2"; shift 2 ;;
    --no-verify) VERIFY=0; shift ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unbekanntes Argument: $1" >&2; exit 2 ;;
  esac
done

log() { [ "$QUIET" -eq 1 ] || printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf 'FEHLER: %s\n' "$*" >&2; exit 1; }

[ -d "$DSH_HOME_DIR" ] || die "$DSH_HOME_DIR fehlt"
umask 077

STAMP="$(date '+%Y%m%d_%H%M%S')"
DEST="$DEST_ROOT/$STAMP"
mkdir -p "$DEST"

log "Sichere $DSH_HOME_DIR -> $DEST/dsh_home.tar.gz"
tar -czf "$DEST/dsh_home.tar.gz" -C /home/daniel .dsh

# --- Manifest: jede Datei mit Groesse ---------------------------------------
( cd /home/daniel && find .dsh -type f -printf '%P\t%s\n' | sort ) > "$DEST/manifest.tsv"
FILES="$(wc -l < "$DEST/manifest.tsv")"
BYTES="$(awk -F'\t' '{s+=$2} END {print s+0}' "$DEST/manifest.tsv")"

# --- Zustandsprotokoll ------------------------------------------------------
{
  echo "# DSH-Zustand zum Snapshot-Zeitpunkt"
  echo "zeitpunkt: $(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "hostname: $(hostname)"
  echo "dsh_home: $DSH_HOME_DIR"
  echo "dsh_home_dateien: $FILES"
  echo "dsh_home_bytes: $BYTES"
  echo "checkout: $CHECKOUT"
  echo "checkout_rev: $(git -C "$CHECKOUT" rev-parse HEAD 2>/dev/null || echo '-')"
  echo "checkout_tag: $(git -C "$CHECKOUT" describe --tags --always 2>/dev/null || echo '-')"
  echo "dsh_version: $(node "$CHECKOUT/apps/cli/lib/bin.js" --version 2>/dev/null | tail -1 || echo '-')"
  echo "cli_symlink: $(readlink -f /home/daniel/.local/bin/dsh 2>/dev/null || echo '-')"
  echo "startscript_sha256: $(sha256sum /home/daniel/start-dsh-native.sh 2>/dev/null | cut -d' ' -f1 || echo '-')"
  echo "sessions_v3: $(find "$DSH_HOME_DIR/sessions" -name 'session.v3.jsonl.zstd' 2>/dev/null | wc -l)"
  echo "sessions_v4: $(find "$DSH_HOME_DIR/sessions" -name 'session.v4.jsonl.zstd' 2>/dev/null | wc -l)"
  echo "profiles: $(ls "$DSH_HOME_DIR/profiles" 2>/dev/null | tr '\n' ' ')"
  echo "settings_yaml: $(test -f "$DSH_HOME_DIR/settings.yaml" && echo vorhanden || echo FEHLT)"
  echo "settings_imported: $(test -f "$DSH_HOME_DIR/settings.yaml.imported" && echo vorhanden || echo '-')"
} > "$DEST/state.txt"

# --- Checksummen ------------------------------------------------------------
sha256sum "$DEST/dsh_home.tar.gz" > "$DEST/dsh_home.tar.gz.sha256"

# --- Restore-Anleitung ------------------------------------------------------
cat > "$DEST/RESTORE.md" <<'RESTORE'
# Wiederherstellung dieses DSH-Stands

Alles, was die Installation ausmacht (Chats/Sessions, Settings, Credentials,
Patches, Storages, Attachments), liegt in ~/.dsh. Der Checkout ist Quellcode
und liegt auf GitHub (Tag siehe state.txt → checkout_tag).

## Nur den DSH-Zustand zurueckholen (empfohlen, nicht destruktiv)

    systemctl --user stop dsh-native dsh-secretary
    mv ~/.dsh ~/.dsh.vor-restore-$(date +%Y%m%d_%H%M%S)
    tar -xzf <dieser-ordner>/dsh_home.tar.gz -C /home/daniel .dsh
    systemctl --user start dsh-native dsh-secretary

Das alte Home wird dabei nicht geloescht, sondern umbenannt - ein zweiter
Versuch bleibt jederzeit moeglich.

## Pruefen, ob das Archiv vollstaendig ist

    sha256sum -c <dieser-ordner>/dsh_home.tar.gz.sha256
    tar -tzf <dieser-ordner>/dsh_home.tar.gz | grep -c 'session.v3.jsonl.zstd'
    grep -c . <dieser-ordner>/manifest.tsv

## Wenn auch der Quellcode zurueck muss

    git -C /home/daniel/deepseek-harness checkout --detach <checkout_tag aus state.txt>
    pnpm --dir /home/daniel/deepseek-harness install --frozen-lockfile
    pnpm --dir /home/daniel/deepseek-harness run build
RESTORE

# --- Verifikation: entpacken und gegen Manifest zaehlen ---------------------
if [ "$VERIFY" -eq 1 ]; then
  log "Verifiziere Archiv (Testentpackung)"
  VERIFY_DIR="$DEST/verify"
  mkdir -p "$VERIFY_DIR"
  tar -xzf "$DEST/dsh_home.tar.gz" -C "$VERIFY_DIR"
  VFILES="$(find "$VERIFY_DIR/.dsh" -type f | wc -l)"
  VBYTES="$(find "$VERIFY_DIR/.dsh" -type f -printf '%s\n' | awk '{s+=$1} END {print s+0}')"
  if [ "$VFILES" != "$FILES" ] || [ "$VBYTES" != "$BYTES" ]; then
    printf 'FEHLER: Verifikation fehlgeschlagen - Manifest: %s Dateien / %s Bytes, Archiv: %s / %s\n' \
      "$FILES" "$BYTES" "$VFILES" "$VBYTES" >&2
    exit 1
  fi
  rm -rf "$VERIFY_DIR"
  log "Verifikation ok: $VFILES Dateien, $(( VBYTES / 1024 / 1024 )) MB"
fi

# --- Rotation ---------------------------------------------------------------
if [ "$KEEP" -gt 0 ]; then
  ls -1dt "$DEST_ROOT"/*/ 2>/dev/null | tail -n +$(( KEEP + 1 )) | while read -r old; do
    log "entferne alten Snapshot $old"
    rm -rf "$old"
  done
fi

SIZE="$(du -h "$DEST/dsh_home.tar.gz" | cut -f1)"
log "Snapshot fertig: $DEST (Archiv $SIZE, $FILES Dateien)"
printf '%s\n' "$DEST" > "$DEST_ROOT/LATEST"
