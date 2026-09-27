#!/usr/bin/env bash
# =============================================================================
# setup_dsh_parallel_017.sh - Parallelinstallation von DSH 0.1.7-rc.2
#
# Legt NEBEN der laufenden 0.1.5-Produktion an:
#   <WORKSPACE>  git worktree des Tags dsh-v0.1.7-rc.2, gebaut
#   <HOME_DIR>   eigenes DSH-Home (Settings, Patches, Credentials, optional Chats)
#   Startskript + systemd-Unit dsh-native-017 (Port 3092)
#
# Die Produktion (~/.dsh, /home/daniel/deepseek-harness, Port 3090) wird nur
# gelesen. Einzige Ausnahme: 'git fetch --tags' und die Worktree-Metadaten im
# gemeinsamen .git - der Arbeitsbaum der Produktion bleibt unveraendert.
#
# Aktionen:
#   preflight   nur lesende Vorabpruefung
#   fetch       Tags vom Origin holen
#   worktree    Worktree anlegen/aktualisieren
#   build       pnpm install + build im Worktree
#   seed        Home fuellen (Kopien aus ~/.dsh, Kandidaten-Patch)
#   service     Startskript + Unit schreiben (noch nicht starten)
#   start|stop|status
#   verify      Version, Build-Record, Linkfarm, Komposition, HTTP
#   reset       Testkanal komplett abbauen (Produktion bleibt unberuehrt)
#   all         preflight fetch worktree build seed service verify
#
# Optionen:
#   --workspace DIR   Default /home/daniel/deepseek-harness-017
#   --home DIR        Default /home/daniel/.dsh-017
#   --port N          Default 3092
#   --tag TAG         Default dsh-v0.1.7-rc.2
#   --patch FILE      Profil-Patch fuer das neue Home
#                     Default: $STAGE/boot1_standard/cordis.patch.candidate.yml
#   --with-sessions   Chats/Sessions und Attachments mitkopieren
#   --yes --dry-run --force
# =============================================================================
set -Eeuo pipefail

PROD_CHECKOUT="/home/daniel/deepseek-harness"
PROD_HOME="/home/daniel/.dsh"
WORKSPACE="/home/daniel/deepseek-harness-017"
HOME_DIR="/home/daniel/.dsh-017"
PORT="3092"
HTTP_PROXY_PORT="3093"
HTTPS_PROXY_PORT="3445"
TAG="dsh-v0.1.7-rc.2"
STAGE="/home/daniel/QJM/dsh_playground/dsh_update_0.1.7"
PATCH_FILE="$STAGE/boot1_standard/cordis.patch.candidate.yml"
START_TEMPLATE="/home/daniel/QJM/scripts/dsh-start/start-dsh-native-017.sh"
WITH_SESSIONS=0
ASSUME_YES=0
DRY_RUN=0
FORCE=0
ACTION=""

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

usage() { sed -n '2,40p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    preflight|fetch|worktree|build|seed|service|start|stop|status|verify|reset|all) ACTION="$1"; shift ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --home) HOME_DIR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --patch) PATCH_FILE="$2"; shift 2 ;;
    --with-sessions) WITH_SESSIONS=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unbekanntes Argument: $1" ;;
  esac
done
[ -n "$ACTION" ] || { usage; exit 2; }

UNIT="$HOME_DIR.service"
UNIT_FILE="/home/daniel/.config/systemd/user/dsh-native-017.service"
START_SCRIPT="/home/daniel/start-dsh-native-017.sh"
STATE_DIR_DUMP="/home/daniel/QJM/dsh_playground/dsh_update_0.1.7/dump-config-017.yml"
TARGET_VERSION="$(printf '%s' "$TAG" | sed 's/^dsh-v//')"

have_systemd() { command -v systemctl >/dev/null 2>&1 && systemctl --user show dsh-native.service >/dev/null 2>&1; }
systemd_hint() { warn "systemctl --user nicht erreichbar - Dienstschritte bitte in einer Login-Shell ausfuehren:"; }

# ------------------------------------------------------------------ Aktionen
do_preflight() {
  step "Preflight"
  local fehler=0
  [ "$WORKSPACE" != "$PROD_CHECKOUT" ] || { warn "WORKSPACE darf nicht der Produktions-Checkout sein"; fehler=1; }
  [ "$HOME_DIR" != "$PROD_HOME" ] || { warn "HOME darf nicht das Produktions-Home sein"; fehler=1; }
  case "$HOME_DIR" in /home/daniel/.dsh*) : ;; *) warn "HOME liegt unerwartet ausserhalb /home/daniel/.dsh*" ;; esac

  [ -d "$PROD_CHECKOUT/.git" ] || { warn "Produktions-Checkout fehlt"; fehler=1; }
  [ -d "$PROD_HOME" ] || { warn "Produktions-Home fehlt"; fehler=1; }
  [ -f "$START_TEMPLATE" ] || { warn "Start-Template fehlt: $START_TEMPLATE"; fehler=1; }
  [ -f "$PATCH_FILE" ] || { warn "Kandidaten-Patch fehlt: $PATCH_FILE"; fehler=1; }

  local dirty; dirty="$(git -C "$PROD_CHECKOUT" status --porcelain | wc -l)"
  [ "$dirty" -eq 0 ] || { warn "$dirty lokale Aenderungen im Produktions-Checkout"; fehler=1; }

  if git -C "$PROD_CHECKOUT" ls-remote --tags origin "refs/tags/$TAG" 2>/dev/null | grep -q "refs/tags/$TAG"; then
    log "  Tag $TAG auf origin vorhanden"
  else
    warn "Tag $TAG auf origin nicht gefunden"; fehler=1
  fi

  local frei; frei="$(df -Pk /home/daniel | awk 'NR==2 {print $4}')"
  [ "$frei" -ge 31457280 ] || { warn "weniger als 30 GB frei"; fehler=1; }
  log "  Speicher frei: $(( frei / 1024 / 1024 )) GB"

  command -v pnpm >/dev/null 2>&1 || { warn "pnpm fehlt"; fehler=1; }
  command -v node >/dev/null 2>&1 || { warn "node fehlt"; fehler=1; }

  local latest; latest="$(cat /home/daniel/QJM/backups/dsh_state/LATEST 2>/dev/null || true)"
  if [ -n "$latest" ] && [ -d "$latest" ]; then
    log "  Snapshot vorhanden: $latest"
  else
    warn "kein Snapshot gefunden - vorher scripts/dsh_snapshot.sh laufen lassen"
  fi

  if have_systemd; then log "  systemd --user erreichbar"; else warn "systemd --user nicht erreichbar (Dienstschritte spaeter manuell)"; fi
  if [ "$fehler" -eq 0 ]; then log "Preflight ok"; else die "Preflight hat Probleme gefunden"; fi
}

do_fetch() {
  step "Tags holen"
  run env GIT_TERMINAL_PROMPT=0 git -C "$PROD_CHECKOUT" fetch --tags --prune origin
}

do_worktree() {
  step "Worktree $WORKSPACE @ $TAG"
  if [ -d "$WORKSPACE/.git" ] || [ -f "$WORKSPACE/.git" ]; then
    log "Worktree existiert bereits - nur Revision pruefen"
    local ist; ist="$(git -C "$WORKSPACE" describe --tags --always)"
    log "  aktuell: $ist"
  else
    [ -e "$WORKSPACE" ] && die "$WORKSPACE existiert und ist kein Worktree"
    run git -C "$PROD_CHECKOUT" worktree prune
    run git -C "$PROD_CHECKOUT" worktree add "$WORKSPACE" "$TAG"
  fi
  if [ "$DRY_RUN" -eq 0 ]; then
    log "  Revision: $(git -C "$WORKSPACE" rev-parse --short HEAD)"
    log "  Tag-Info: $(git -C "$WORKSPACE" describe --tags --always)"
  fi
}

do_build() {
  step "Bauen in $WORKSPACE (10-30 Min)"
  run pnpm --dir "$WORKSPACE" install --frozen-lockfile
  run pnpm --dir "$WORKSPACE" run build
  if [ "$DRY_RUN" -eq 0 ]; then
    local v; v="$(node "$WORKSPACE/apps/cli/lib/bin.js" --version 2>/dev/null | tail -1)"
    log "  gebaut: $v (erwartet $TARGET_VERSION)"
    [ "$v" = "$TARGET_VERSION" ] || die "Version $v != $TARGET_VERSION"
  fi
}

do_seed() {
  step "Home fuellen: $HOME_DIR"
  [ -d "$PROD_HOME" ] || die "$PROD_HOME fehlt"
  run mkdir -p "$HOME_DIR/profiles/web"

  # Konfigurationen 1:1
  for f in settings.yaml .credentials.yaml cordis.patch.yml .anonymous-user-id; do
    if [ -e "$PROD_HOME/$f" ]; then run cp -a "$PROD_HOME/$f" "$HOME_DIR/$f"; fi
  done
  [ -d "$PROD_HOME/ssl" ] && run cp -a "$PROD_HOME/ssl" "$HOME_DIR/ssl"
  [ -f "$PROD_HOME/storages/workspace.json" ] && { run mkdir -p "$HOME_DIR/storages"; run cp -a "$PROD_HOME/storages/workspace.json" "$HOME_DIR/storages/workspace.json"; }

  # Profil-Konfiguration (NICHT die Linkfarm: die muss auf den neuen Checkout zeigen)
  for f in package.json cordis.yml pnpm-workspace.yaml; do
    [ -e "$PROD_HOME/profiles/web/$f" ] && run cp -a "$PROD_HOME/profiles/web/$f" "$HOME_DIR/profiles/web/$f"
  done
  run cp -a "$PATCH_FILE" "$HOME_DIR/profiles/web/cordis.patch.yml"
  log "  Profil-Patch: $PATCH_FILE"

  if [ "$WITH_SESSIONS" -eq 1 ]; then
    step "Chats/Sessions mitkopieren (Momentaufnahme)"
    run mkdir -p "$HOME_DIR/sessions" "$HOME_DIR/attachments"
    run rsync -a "$PROD_HOME/sessions/" "$HOME_DIR/sessions/"
    [ -d "$PROD_HOME/attachments" ] && run rsync -a "$PROD_HOME/attachments/" "$HOME_DIR/attachments/"
    if [ "$DRY_RUN" -eq 0 ]; then
      log "  Sessions kopiert: $(find "$HOME_DIR/sessions" -name 'session.v*.jsonl.zstd' | wc -l)"
    fi
  else
    log "  Sessions NICHT kopiert (--with-sessions fuer eine Momentaufnahme)"
  fi
}

do_service() {
  step "Startskript und Unit"
  run cp -a "$START_TEMPLATE" "$START_SCRIPT"
  run chmod +x "$START_SCRIPT"
  if [ "$DRY_RUN" -eq 1 ]; then printf '  [dry-run] Unit nach %s schreiben\n' "$UNIT_FILE"; return 0; fi
  mkdir -p /home/daniel/.config/systemd/user
  cat > "$UNIT_FILE" <<UNIT_EOF
# Testkanal DSH 0.1.7-rc.2 - erzeugt von setup_dsh_parallel_017.sh
[Unit]
Description=DeepSeek Harness 0.1.7-rc.2 (Parallel-Instanz, Port $PORT)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/daniel
ExecStart=$START_SCRIPT
Restart=always
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=HOME=/home/daniel
Environment=DSH_CHECKOUT=$WORKSPACE
Environment=DSH_HOME=$HOME_DIR
Environment=DSH_PORT=$PORT
Environment=HTTP_PROXY_PORT=$HTTP_PROXY_PORT
Environment=HTTPS_PROXY_PORT=$HTTPS_PROXY_PORT

[Install]
WantedBy=default.target
UNIT_EOF
  log "  Unit: $UNIT_FILE"
  if have_systemd; then
    run systemctl --user daemon-reload
    run systemctl --user enable dsh-native-017
  else
    systemd_hint
    printf '    systemctl --user daemon-reload\n    systemctl --user enable --now dsh-native-017\n'
  fi
}

do_start() {
  step "Dienst dsh-native-017 starten"
  if have_systemd; then run systemctl --user start dsh-native-017; else systemd_hint; printf '    systemctl --user start dsh-native-017\n'; fi
}
do_stop() {
  step "Dienst dsh-native-017 stoppen"
  if have_systemd; then run systemctl --user stop dsh-native-017 || true; else systemd_hint; printf '    systemctl --user stop dsh-native-017\n'; fi
}
do_status() {
  step "Status Testkanal"
  printf 'Worktree      : %s %s\n' "$WORKSPACE" "$(git -C "$WORKSPACE" describe --tags --always 2>/dev/null || echo '-')"
  printf 'Home          : %s (%s Dateien)\n' "$HOME_DIR" "$(find "$HOME_DIR" -type f 2>/dev/null | wc -l)"
  printf 'Version       : %s\n' "$(node "$WORKSPACE/apps/cli/lib/bin.js" --version 2>/dev/null | tail -1 || echo '-')"
  printf 'Startskript   : %s\n' "$START_SCRIPT"
  printf 'Unit          : %s\n' "$UNIT_FILE"
  if have_systemd; then printf 'Dienst        : %s\n' "$(systemctl --user is-active dsh-native-017 || true)"; fi
  printf 'Port %s intern: %s\n' "$PORT" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:$PORT/" 2>/dev/null || echo '-')"
  printf 'Port %s extern: %s   (App lauscht nur auf 127.0.0.1, nach aussen via socat)\n' "$HTTP_PROXY_PORT" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:$HTTP_PROXY_PORT/" 2>/dev/null || echo '-')"
  printf 'URL           : scripts/dsh_url.sh 017\n'
}

do_verify() {
  step "Verifikation"
  printf 'Version       : %s (erwartet %s)\n' "$(node "$WORKSPACE/apps/cli/lib/bin.js" --version 2>/dev/null | tail -1 || echo '-')" "$TARGET_VERSION"
  printf 'Build-Record  : %s\n' "$(jq -c .environment "$WORKSPACE/.dsh-build/client-build-environment.json" 2>/dev/null || echo '-')"
  # 0.1.7 loest Plugins zur Laufzeit auf; die Linkfarm aus 0.1.5 wird nicht mehr benoetigt.
  # Entscheidend: im neuen Home darf NICHTS auf den alten Checkout zeigen.
  local leaks refs
  leaks="$(find "$HOME_DIR" -type l -lname "*deepseek-harness/apps*" 2>/dev/null | wc -l)"
  refs="$(grep -rl "deepseek-harness/apps" "$HOME_DIR" 2>/dev/null | wc -l)"
  printf 'Verweise auf 0.1.5-Checkout : %s Links, %s Dateien (erwartet 0/0)\n' "$leaks" "$refs"
  if [ "$leaks" -eq 0 ] && [ "$refs" -eq 0 ]; then
    log "  neues Home ist self-contained - laedt keinen 0.1.5-Plugin-Code"
  else
    warn "  Verweise auf den alten Checkout gefunden - bitte pruefen"
  fi
  if DSH_HOME="$HOME_DIR" node "$WORKSPACE/apps/cli/lib/bin.js" --profile web --dump-config > "$STATE_DIR_DUMP" 2>&1; then
    printf 'Komposition   : ok (%s Zeilen), preset-trader: %s\n' "$(wc -l < "$STATE_DIR_DUMP")" "$(grep -c preset-trader "$STATE_DIR_DUMP" || true)"
  elif grep -q -E "EROFS|EACCES" "$STATE_DIR_DUMP"; then
    log "  dump-config braucht ein schreibbares DSH_HOME (Sandbox) - in der Login-Shell erneut pruefen"
  else
    warn "dump-config fehlgeschlagen:"; tail -5 "$STATE_DIR_DUMP" >&2
  fi
  printf 'HTTP %s      : %s\n' "$PORT" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:$PORT/" 2>/dev/null || echo '-')"
  step "Produktion unberuehrt?"
  printf 'Produktions-Home : %s (%s Dateien)\n' "$PROD_HOME" "$(find "$PROD_HOME" -type f 2>/dev/null | wc -l)"
  printf 'Prod.-Checkout   : %s\n' "$(git -C "$PROD_CHECKOUT" describe --tags --always)"
  printf 'HTTP 3090        : %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:3090/ 2>/dev/null || echo '-')"
}

do_reset() {
  step "Testkanal abbauen (Produktion bleibt unberuehrt)"
  confirm "Worktree $WORKSPACE, Home $HOME_DIR, Unit und Startskript entfernen?"
  if have_systemd; then run systemctl --user disable --now dsh-native-017 || true; fi
  run rm -f "$UNIT_FILE" "$START_SCRIPT"
  if have_systemd; then run systemctl --user daemon-reload; fi
  if [ -d "$WORKSPACE" ]; then run git -C "$PROD_CHECKOUT" worktree remove --force "$WORKSPACE"; fi
  run rm -rf "$HOME_DIR"
  log "Testkanal entfernt. Die Produktion wurde nicht angefasst."
}

case "$ACTION" in
  preflight) do_preflight ;;
  fetch)     do_fetch ;;
  worktree)  do_worktree ;;
  build)     do_build ;;
  seed)      do_seed ;;
  service)   do_service ;;
  start)     do_start ;;
  stop)      do_stop ;;
  status)    do_status ;;
  verify)    do_verify ;;
  reset)     do_reset ;;
  all)
    do_preflight
    do_fetch
    do_worktree
    do_build
    do_seed
    do_service
    if have_systemd; then
      do_start
      log "kurz warten, dann Verifikation"
      sleep 10
    fi
    do_verify
    log "FERTIG. Testkanal: http://127.0.0.1:$PORT/ (Produktion unveraendert auf 3090)"
    ;;
esac
