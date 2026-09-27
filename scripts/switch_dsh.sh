#!/usr/bin/env bash
# =============================================================================
# switch_dsh.sh - zwischen der aktuellen 0.1.5-Installation und 0.1.7-rc.2 umschalten
#
# Prinzip:
#   * Es laeuft immer genau EINE Instanz je Home (sonst streiten sich zwei
#     Prozesse um Sessions/Profil-Links).
#   * ~/.dsh (alle heutigen Chats + Konfigs) wird von diesem Skript NIE
#     veraendert. Es bleibt der eingefrorene Stand, zu dem du jederzeit
#     zurueckkehren kannst.
#   * Umgeschaltet wird nur die Datei ~/start-dsh-native.sh, die die
#     systemd-Unit dsh-native.service startet. Die Unit selbst bleibt gleich,
#     der Port bleibt 3090 - die URLs aendern sich nicht.
#
# Aktionen:
#   status          aktueller Stand + Verfuegbarkeit beider Varianten
#   to-017          DSH 0.1.7 auf 3090 mit ~/.dsh-017 (Chats werden vorher gesynct)
#   to-015          zurueck auf 0.1.5 mit ~/.dsh (Original-Startskript)
#   sync-sessions   015 -> 017 synchronisieren (nur bei gestoppter 015)
#
# Optionen: --yes  --dry-run  --no-sync  --force
# =============================================================================
set -Eeuo pipefail

PROD_HOME="/home/daniel/.dsh"
NEW_HOME="/home/daniel/.dsh-017"
NEW_CHECKOUT="/home/daniel/deepseek-harness-017"
START_LIVE="/home/daniel/start-dsh-native.sh"
START_015="/home/daniel/start-dsh-native-015.sh"
START_017="/home/daniel/start-dsh-native-017.sh"
DIR_015="/home/daniel/QJM/scripts/dsh-start/start-dsh-native-015.sh"
SNAPSHOT_SCRIPT="/home/daniel/QJM/scripts/dsh_snapshot.sh"
SERVICE="dsh-native"
TEST_SERVICE="dsh-native-017"
ASSUME_YES=0
DRY_RUN=0
NO_SYNC=0
FORCE=0
ACTION=""
DEFAULT_ID=""
PATCH_017="$NEW_HOME/profiles/web/cordis.patch.yml"
BACKUP_DIR="/home/daniel/QJM/dsh_playground/dsh_update_0.1.7/patch-backups"

log()  { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
step() { printf '\n=== %s ===\n' "$*"; }
die()  { printf '\nFEHLER: %s\n' "$*" >&2; exit 1; }
warn() { printf 'WARNUNG: %s\n' "$*" >&2; }
run()  {
  if [ "$DRY_RUN" -eq 1 ]; then printf '  [dry-run] %s\n' "$*"; return 0; fi
  printf '  + %s\n' "$*"; "$@"
}
confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  printf '%s [j/N] ' "$1"; read -r a
  case "$a" in j|J|ja|Ja) return 0 ;; *) die "abgebrochen" ;; esac
}
have_systemd() { command -v systemctl >/dev/null 2>&1 && systemctl --user show "$SERVICE" >/dev/null 2>&1; }
usage() { sed -n '2,24p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    status|to-017|to-015|sync-sessions) ACTION="$1"; shift ;;
    set-default) ACTION="set-default"; DEFAULT_ID="$2"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-sync) NO_SYNC=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unbekanntes Argument: $1" ;;
  esac
done
[ -n "$ACTION" ] || { usage; exit 2; }

live_target() {
  if grep -q "deepseek-harness-017" "$START_LIVE" 2>/dev/null; then echo "017"; else echo "015"; fi
}

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:$1/" 2>/dev/null || echo "-"; }

do_status() {
  step "Umschaltstatus"
  printf 'Aktives Startskript   : %s -> DSH %s\n' "$START_LIVE" "$(live_target)"
  printf 'Produktions-Home      : %s (%s Dateien, %s Sessions)\n' "$PROD_HOME" "$(find "$PROD_HOME" -type f 2>/dev/null | wc -l)" "$(find "$PROD_HOME/sessions" -name 'session.v*.jsonl.zstd' 2>/dev/null | wc -l)"
  printf 'Neues Home 0.1.7      : %s (%s Dateien, %s Sessions)\n' "$NEW_HOME" "$(find "$NEW_HOME" -type f 2>/dev/null | wc -l)" "$(find "$NEW_HOME/sessions" -name 'session.v*.jsonl.zstd' 2>/dev/null | wc -l)"
  printf 'Checkout 0.1.7        : %s %s\n' "$NEW_CHECKOUT" "$(git -C "$NEW_CHECKOUT" describe --tags --always 2>/dev/null || echo 'FEHLT')"
  printf 'Original 0.1.5-Skript : %s %s\n' "$START_015" "$(sha256sum "$START_015" 2>/dev/null | cut -c1-12 || echo FEHLT)"
  printf 'HTTP 3091 extern 015  : %s (intern 3090: %s)\n' "$(http_code 3091)" "$(http_code 3090)"
  printf 'HTTP 3093 extern 017  : %s (intern 3092: %s)\n' "$(http_code 3093)" "$(http_code 3092)"
  printf 'URL holen             : scripts/dsh_url.sh 015|017   (App lauscht nur auf 127.0.0.1)\n'
  if have_systemd; then
    printf 'Dienst %-14s: %s\n' "$SERVICE" "$(systemctl --user is-active "$SERVICE" || true)"
    printf 'Dienst %-14s: %s\n' "$TEST_SERVICE" "$(systemctl --user is-active "$TEST_SERVICE" || true)"
  fi
  local latest; latest="$(cat /home/daniel/QJM/backups/dsh_state/LATEST 2>/dev/null || true)"
  [ -n "$latest" ] && printf 'Letzter Snapshot      : %s\n' "$latest"
}

do_sync_sessions() {
  [ -d "$NEW_HOME" ] || die "$NEW_HOME fehlt - erst setup_dsh_parallel_017.sh ausfuehren"
  step "Chats 015 -> 017 synchronisieren"
  if [ "$DRY_RUN" -eq 0 ] && have_systemd && systemctl --user is-active --quiet "$SERVICE" && [ "$FORCE" -eq 0 ]; then
    die "Dienst $SERVICE laeuft - fuer einen konsistenten Sync erst stoppen (oder --force)"
  fi
  run mkdir -p "$NEW_HOME/sessions" "$NEW_HOME/attachments"
  run rsync -a --delete "$PROD_HOME/sessions/" "$NEW_HOME/sessions/"
  [ -d "$PROD_HOME/attachments" ] && run rsync -a "$PROD_HOME/attachments/" "$NEW_HOME/attachments/"
  if [ "$DRY_RUN" -eq 0 ]; then
    log "  Sessions in 017: $(find "$NEW_HOME/sessions" -name 'session.v*.jsonl.zstd' | wc -l)"
  fi
}

apply_registry_default() {
  # Setzt den Deployment-Default der Preset-Registry im 0.1.7-Profil-Patch.
  # Der Patch enthaelt bereits die importierten Settings - deshalb wird hier nur
  # die eine Zeile geaendert, nicht der ganze Patch ersetzt.
  local id="$1"
  [ -f "$PATCH_017" ] || die "0.1.7-Profil-Patch fehlt: $PATCH_017"
  mkdir -p "$BACKUP_DIR"
  run cp -a "$PATCH_017" "$BACKUP_DIR/cordis.patch.yml.017-vor-default-$(date '+%Y%m%d_%H%M%S')"
  run sed -i "/^- id: agent-preset-registry$/,/^    default: / s/^    default: .*/    default: $id/" "$PATCH_017"
  if [ "$DRY_RUN" -eq 0 ]; then
    log "  Registry-Default jetzt:"
    grep -A3 "^- id: agent-preset-registry" "$PATCH_017" | sed 's/^/    /'
  fi
}

do_set_default() {
  [ -n "$DEFAULT_ID" ] || die "Aufruf: switch_dsh.sh set-default <preset-id>"
  step "Preset-Default im 0.1.7-Home auf '$DEFAULT_ID' setzen"
  confirm "Default fuer neue Sessions in $NEW_HOME auf '$DEFAULT_ID' setzen?"
  apply_registry_default "$DEFAULT_ID"
  log "Wirkt auf neue Sessions; laufende Sessions behalten ihre Komposition."
  log "Falls 0.1.7 als Dienst laeuft: systemctl --user restart dsh-native-017"
}

write_wrapper_017() {
  if [ "$DRY_RUN" -eq 1 ]; then printf '  [dry-run] %s als Produktionsstart schreiben (0.1.7, Port 3090)\n' "$START_LIVE"; return 0; fi
  cat > "$START_LIVE" <<'WRAP_EOF'
#!/bin/bash
# =============================================================================
# Produktionsstart DSH 0.1.7-rc.2 - erzeugt von switch_dsh.sh --to 017
# Zurueck zu 0.1.5:  switch_dsh.sh to-015 --yes   (holt start-dsh-native-015.sh zurueck)
# =============================================================================
export DSH_CHECKOUT="/home/daniel/deepseek-harness-017"
export DSH_HOME="/home/daniel/.dsh-017"
export DSH_PORT="3090"
export HTTP_PROXY_PORT="3091"
export HTTPS_PROXY_PORT="3444"
exec /home/daniel/start-dsh-native-017.sh
WRAP_EOF
  chmod +x "$START_LIVE"
  log "  $START_LIVE zeigt jetzt auf 0.1.7 mit $NEW_HOME (Port 3090)"
}

do_to_017() {
  step "Umschalten auf DSH 0.1.7-rc.2"
  [ -f "$START_017" ] || die "$START_017 fehlt - erst setup_dsh_parallel_017.sh ausfuehren"
  [ -x "$NEW_CHECKOUT/apps/cli/lib/bin.js" ] || [ -f "$NEW_CHECKOUT/apps/cli/lib/bin.js" ] || die "0.1.7 ist nicht gebaut ($NEW_CHECKOUT)"
  [ -f "$START_015" ] || { warn "$START_015 fehlt - hole Original aus dem QJM-Repo"; run cp -a "$DIR_015" "$START_015"; }
  confirm "Jetzt auf 0.1.7 umschalten? (Dienst wird kurz gestoppt, Chats werden synchronisiert)"

  step "1/6 Sicherheits-Snapshot des aktuellen Stands"
  run bash "$SNAPSHOT_SCRIPT" --quiet

  step "2/6 Testkanal-Dienst stoppen (verhindert zwei Instanzen auf demselben Home)"
  if have_systemd; then run systemctl --user stop "$TEST_SERVICE" || true; fi

  step "3/6 Produktionsdienst stoppen (Downtime beginnt)"
  if have_systemd; then run systemctl --user stop "$SERVICE"; else die "systemctl --user nicht erreichbar - in einer Login-Shell ausfuehren"; fi

  if [ "$NO_SYNC" -eq 0 ]; then
    step "4/6 Chats und Attachments nach 017 uebernehmen"
    do_sync_sessions
  else
    log "4/6 Sync uebersprungen (--no-sync)"
  fi

  step "4b/6 Preset-Pakete im 0.1.7-Home pruefen"
  if bash /home/daniel/QJM/scripts/dsh_fix_profile_links.sh --check 2>&1 | grep -q "alles aufloesbar"; then
    log "  alle Pakete der Preset-Komposition aufloesbar"
  else
    warn "  nicht alle Pakete aufloesbar - sonst startet 'trader' mit 'Failed to load'."
    warn "  Fix: bash /home/daniel/QJM/scripts/dsh_fix_profile_links.sh   (danach Dienst neu starten)"
  fi

  step "4c/6 Preset-Default fuer neue Sessions"
  if grep -q "^    default: standard$" "$PATCH_017" 2>/dev/null; then
    log "  Default steht noch auf 'standard' (sichere Erstinstallation)"
    apply_registry_default trader
  else
    log "  Default bereits gesetzt - keine Aenderung"
  fi

  step "5/6 Startskript der Produktion umstellen"
  if [ "$DRY_RUN" -eq 0 ]; then
    cp -a "$START_LIVE" "/home/daniel/start-dsh-native.015-$(date '+%Y%m%d_%H%M%S').bak"
  fi
  write_wrapper_017

  step "6/6 0.1.7 starten und pruefen"
  run systemctl --user start "$SERVICE"
  if [ "$DRY_RUN" -eq 0 ]; then
    local i=0 code="-"
    while [ "$i" -lt 60 ]; do
      code="$(http_code 3090)"
      [ "$code" != "-" ] && [ "$code" != "000" ] && break
      sleep 2; i=$(( i + 2 ))
    done
    log "HTTP 3090: $code"
    log "Version: $(node "$NEW_CHECKOUT/apps/cli/lib/bin.js" --version 2>/dev/null | tail -1)"
  fi
  log "FERTIG. Rueckweg jederzeit: switch_dsh.sh to-015 --yes"
}

do_to_015() {
  step "Zurueck auf 0.1.5-rc.2"
  [ -f "$START_015" ] || die "$START_015 fehlt - Original verloren? Kopie im QJM-Repo: $DIR_015"
  confirm "Zurueck auf 0.1.5 mit ~/.dsh umschalten?"
  if have_systemd; then
    step "1/3 Dienst stoppen"
    run systemctl --user stop "$SERVICE"
  else
    die "systemctl --user nicht erreichbar - in einer Login-Shell ausfuehren"
  fi

  step "2/3 Original-Startskript zurueckholen und pruefen"
  run cp -a "$START_015" "$START_LIVE"
  run chmod +x "$START_LIVE"
  if [ "$DRY_RUN" -eq 0 ]; then
    log "  sha256: $(sha256sum "$START_LIVE" | cut -c1-16)"
  fi

  step "3/3 0.1.5 starten"
  run systemctl --user start "$SERVICE"
  if [ "$DRY_RUN" -eq 0 ]; then
    local i=0 code="-"
    while [ "$i" -lt 60 ]; do
      code="$(http_code 3090)"
      [ "$code" != "-" ] && [ "$code" != "000" ] && break
      sleep 2; i=$(( i + 2 ))
    done
    log "HTTP 3090: $code"
    log "Version: $(node /home/daniel/deepseek-harness/apps/cli/lib/bin.js --version 2>/dev/null | tail -1)"
  fi
  cat <<'HINWEIS'

Hinweis zum Rueckweg:
  * ~/.dsh wurde nie veraendert - alle Chats und Konfigurationen sind genau wie
    vor dem Umschalten.
  * Chats, die DU NACH dem Umschalten auf 0.1.7 geschrieben hast, liegen im
    V4-Format in ~/.dsh-017 und sind mit 0.1.5 nicht lesbar. Sie sind nicht
    verloren: sie bleiben in ~/.dsh-017 und werden sichtbar, sobald du wieder
    auf 0.1.7 gehst.
  * Der Testkanal auf 3092 laesst sich jederzeit wieder starten:
    systemctl --user start dsh-native-017
HINWEIS
}

case "$ACTION" in
  status)        do_status ;;
  sync-sessions) do_sync_sessions ;;
  set-default)   do_set_default ;;
  to-017)        do_to_017 ;;
  to-015)        do_to_015 ;;
esac
