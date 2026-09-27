#!/usr/bin/env bash
# =============================================================================
# dsh_apply_profile_patch.sh - Profil-Patchdatei von DSH ersetzen/zuruecksetzen
#
# Setzt eine vorbereitete Kandidatendatei als
#   ~/.dsh/profiles/web/cordis.patch.yml
# ein, sichert vorher die aktuelle Fassung und macht den Wechsel automatisch
# rueckgaengig, wenn die Komposition danach nicht mehr lesbar ist
# (Pruefung: dsh --profile web --dump-config).
#
# Aktionen:
#   show                      aktuelle Patchdatei + vorhandene Sicherungen
#   apply <datei>             Kandidat einsetzen (Default: boot1_standard)
#   revert [sicherung]        letzte Sicherung zurueckholen
#   check                     nur dump-config ausfuehren (keine Aenderung)
#
# Optionen:
#   --profile <name>   Default: web
#   --yes              ohne Rueckfrage
#   --no-reload        Hinweis auf Live-Reload unterdruecken (nur Text)
# =============================================================================
set -Eeuo pipefail

PROFILE="web"
ASSUME_YES=0
ACTION=""
SRC=""
DSH_HOME_DIR="/home/daniel/.dsh"
STAGE_DIR="/home/daniel/QJM/dsh_playground/dsh_update_0.1.7"
BACKUP_DIR="$STAGE_DIR/patch-backups"
TARGET=""
PRESET_HINT="preset-trader"

while [ $# -gt 0 ]; do
  case "$1" in
    show|apply|revert|check) ACTION="$1"; shift ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    --no-reload) shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) if [ -z "$SRC" ] && [ -f "$1" ]; then SRC="$1"; shift; else echo "unbekanntes Argument: $1" >&2; exit 2; fi ;;
  esac
done
[ -n "$ACTION" ] || { sed -n '2,26p' "$0"; exit 2; }

TARGET="$DSH_HOME_DIR/profiles/$PROFILE/cordis.patch.yml"
[ -z "$SRC" ] && SRC="$STAGE_DIR/boot1_standard/cordis.patch.candidate.yml"

log()  { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
die()  { printf '\nFEHLER: %s\n' "$*" >&2; exit 1; }
confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  printf '%s [j/N] ' "$1"; read -r a
  case "$a" in j|J|ja|Ja) return 0 ;; *) die "abgebrochen" ;; esac
}

mkdir -p "$BACKUP_DIR"

do_show() {
  echo "=== aktuelle Patchdatei: $TARGET ==="
  if [ -f "$TARGET" ]; then cat "$TARGET"; else echo "(fehlt)"; fi
  echo
  echo "=== Kandidaten ==="
  ls -l "$STAGE_DIR"/*/cordis.patch.candidate.yml 2>/dev/null || echo "(keine)"
  echo
  echo "=== Sicherungen ==="
  ls -lt "$BACKUP_DIR" 2>/dev/null | head -10 || echo "(keine)"
}

do_check() {
  log "Kompositionspruefung: dsh --profile $PROFILE --dump-config"
  if dsh --profile "$PROFILE" --dump-config > /tmp/dsh-dump-check.yml 2>&1; then
    log "  dump-config ok ($(wc -l < /tmp/dsh-dump-check.yml) Zeilen)"
    if grep -q "$PRESET_HINT" /tmp/dsh-dump-check.yml; then
      log "  Preset-Row gefunden: $PRESET_HINT"
    else
      log "  Hinweis: $PRESET_HINT erscheint nicht im Dump"
    fi
  else
    tail -20 /tmp/dsh-dump-check.yml >&2
    die "dump-config fehlgeschlagen"
  fi
}

do_apply() {
  [ -f "$SRC" ] || die "Kandidat nicht gefunden: $SRC"
  log "Quelle : $SRC"
  log "Ziel   : $TARGET"
  confirm "Profil-Patch '$PROFILE' jetzt ersetzen?"
  local stamp backup
  stamp="$(date '+%Y%m%d_%H%M%S')"
  backup="$BACKUP_DIR/cordis.patch.yml.$stamp"
  if [ -f "$TARGET" ]; then
    cp -a "$TARGET" "$backup"
    log "Sicherung: $backup"
  else
    log "keine vorhandene Patchdatei - lege nur die neue an"
  fi
  cp -a "$SRC" "$TARGET"
  log "eingesetzt."

  if ! do_check; then
    if [ -f "$backup" ]; then
      cp -a "$backup" "$TARGET"
      log "Automatischer Rollback: alte Patchdatei wiederhergestellt."
    else
      rm -f "$TARGET"
      log "Automatischer Rollback: Datei entfernt (es gab keine vorher)."
    fi
    die "Kandidat war unbrauchbar - nichts geaendert."
  fi

  echo
  log "Fertig. Der Web-Profile-Patch wird live nachgeladen (patchReload: live),"
  log "trotzdem ist ein Neustart die klarste Variante:"
  log "  systemctl --user restart dsh-native"
  log "Danach eine NEUE Session anlegen und pruefen, welches Preset greift."
}

do_revert() {
  local src="$1"
  if [ -z "$src" ]; then
    src="$(ls -t "$BACKUP_DIR"/cordis.patch.yml.* 2>/dev/null | head -1 || true)"
  fi
  [ -n "$src" ] && [ -f "$src" ] || die "keine Sicherung gefunden in $BACKUP_DIR"
  log "stelle wieder her: $src -> $TARGET"
  confirm "Patchdatei zuruecksetzen?"
  cp -a "$src" "$TARGET"
  do_check || die "Wiederhergestellte Datei ist ebenfalls fehlerhaft - bitte manuell pruefen"
  log "Rollback der Patchdatei abgeschlossen."
}

case "$ACTION" in
  show)   do_show ;;
  check)  do_check ;;
  apply)  do_apply ;;
  revert) do_revert "$SRC" ;;
esac
