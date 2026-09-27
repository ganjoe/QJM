#!/usr/bin/env bash
# =============================================================================
# update_dsh.sh - DSH (DeepSeek Harness) Quellcode-Installation aktualisieren
#
# Die Installation ist ein Git-Checkout mit Symlink-CLI:
#   ~/.local/bin/dsh -> ~/.npm-global/bin/dsh -> <CHECKOUT>/apps/cli/lib/bin.js
# Ein Update heisst daher: Checkout umstellen, Abhaengigkeiten installieren,
# bauen, Dienst neu starten. Dieses Skript kapselt das samt Backup + Rollback.
#
# WICHTIG: NICHT aus einer laufenden DSH-Session heraus starten.
# Der Dienst-Stopp beendet die Session (und damit den Agenten, der das Skript
# gerade ausfuehrt). Aufruf ueber SSH/Konsole, z. B.:
#     ssh daniel@10.20.0.23
#     /home/daniel/QJM/scripts/update_dsh.sh all --yes
#
# Aktionen:
#   status      Ist-Zustand (Revision, Version, Dienst, Ziel-Tag)
#   preflight   Nur lesende Vorabpruefungen
#   backup      DSH-Home + Ist-Zustand sichern (~/backups/dsh + Snapshot-Ordner)
#   stop        dsh-native (+ dsh-secretary) stoppen
#   update      fetch + checkout + pnpm install + build
#   start       Dienste starten
#   verify      Version, Build-Record, Port, Journal
#   rollback    auf die vorherige Revision zurueckbauen (optional Home-Restore)
#   all         preflight + backup + stop + update + start + verify
#
# Optionen:
#   --tag <tag>        Ziel-Tag (Default: dsh-v0.1.7-rc.2)
#   --checkout <pfad>  Git-Checkout (Default: /home/daniel/deepseek-harness)
#   --clean            vor dem Build 'pnpm run clean' ausfuehren
#   --no-frozen        'pnpm install' ohne --frozen-lockfile
#   --restore-home     bei rollback zusaetzlich ~/.dsh aus dem Backup zurueckholen
#   --yes              ohne Rueckfrage (Pflicht fuer 'all' und 'rollback')
#   --dry-run          nur zeigen, was passieren wuerde
#   --force            Schutzsperren (DSH-Session, laufender Dienst) uebergehen
#   -h | --help        Hilfe
# =============================================================================
set -Eeuo pipefail

# ---------------------------------------------------------------- Konfiguration
TARGET_TAG="dsh-v0.1.7-rc.2"
CHECKOUT="/home/daniel/deepseek-harness"
SERVICE="dsh-native"
SECONDARY_SERVICE="dsh-secretary"
DSH_HOME_DIR="/home/daniel/.dsh"
PORT="3090"
BACKUP_SCRIPT="/home/daniel/backup_dsh.sh"
BASE_DIR="/home/daniel/backups/dsh"
SNAPSHOT_DIR=""
STATE_DIR="/home/daniel/QJM/dsh_playground/dsh_update_0.1.7"
STATE_FILE="$STATE_DIR/update-state.env"
DRY_RUN=0
ASSUME_YES=0
FORCE=0
DO_CLEAN=0
FROZEN=1
RESTORE_HOME=0
ACTION=""

# ---------------------------------------------------------------------- Ausgabe
log()  { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
step() { printf '\n=== %s ===\n' "$*"; }
die()  { printf '\nFEHLER: %s\n' "$*" >&2; exit 1; }
warn() { printf 'WARNUNG: %s\n' "$*" >&2; }

usage() { sed -n '2,45p' "$0"; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  [dry-run] %s\n' "$*"
    return 0
  fi
  printf '  + %s\n' "$*"
  "$@"
}

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  printf '%s [j/N] ' "$1"
  read -r antwort
  case "$antwort" in j|J|ja|Ja) return 0 ;; *) die "vom Benutzer abgebrochen" ;; esac
}

# ------------------------------------------------------------------- Argumente
while [ $# -gt 0 ]; do
  case "$1" in
    status|preflight|backup|stop|update|start|verify|rollback|all) ACTION="$1"; shift ;;
    --tag) TARGET_TAG="$2"; shift 2 ;;
    --checkout) CHECKOUT="$2"; shift 2 ;;
    --clean) DO_CLEAN=1; shift ;;
    --no-frozen) FROZEN=0; shift ;;
    --restore-home) RESTORE_HOME=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unbekanntes Argument: $1 (siehe --help)" ;;
  esac
done
[ -n "$ACTION" ] || { usage; exit 2; }

TARGET_VERSION="$(printf '%s' "$TARGET_TAG" | sed 's/^dsh-v//')"
STAMP="$(date '+%Y%m%d_%H%M%S')"
mkdir -p "$STATE_DIR"
LOG_FILE="$STATE_DIR/update-$STAMP.log"
if [ "$DRY_RUN" -eq 0 ]; then
  exec > >(tee -a "$LOG_FILE") 2>&1
fi
log "update_dsh.sh Aktion=$ACTION Ziel=$TARGET_TAG Log=$LOG_FILE"

# ------------------------------------------------------------- Hilfsfunktionen
have_systemd() { command -v systemctl >/dev/null 2>&1 && systemctl --user show "$SERVICE" >/dev/null 2>&1; }

service_active() {
  have_systemd || return 1
  systemctl --user is-active --quiet "$SERVICE"
}

dsh_version() {
  node "$CHECKOUT/apps/cli/lib/bin.js" --version 2>/dev/null | tail -1
}

load_state() {
  [ -f "$STATE_FILE" ] || die "kein Zustandsfile $STATE_FILE - zuerst 'backup' ausfuehren"
  # shellcheck disable=SC1090
  . "$STATE_FILE"
}

save_state() {
  {
    printf '# Zustand des letzten Updates - von update_dsh.sh geschrieben\n'
    printf 'PREV_REV=%s\n' "$PREV_REV"
    printf 'PREV_VERSION=%s\n' "$PREV_VERSION"
    printf 'PREV_TAG=%s\n' "$PREV_TAG"
    printf 'TARGET_TAG=%s\n' "$TARGET_TAG"
    printf 'TARGET_REV=%s\n' "$TARGET_REV"
    printf 'SNAPSHOT_DIR=%s\n' "$SNAPSHOT_DIR"
    printf 'HOME_TARBALL=%s\n' "$HOME_TARBALL"
    printf 'STAMP=%s\n' "$STAMP"
  } > "$STATE_FILE"
  log "Zustand gespeichert: $STATE_FILE"
}

# ------------------------------------------------------------------ Aktionen
do_status() {
  step "Status"
  printf 'Checkout          : %s\n' "$CHECKOUT"
  printf 'Revision          : %s\n' "$(git -C "$CHECKOUT" rev-parse --short HEAD 2>/dev/null || echo '-')"
  printf 'Beschreibung      : %s\n' "$(git -C "$CHECKOUT" describe --tags --always 2>/dev/null || echo '-')"
  printf 'Aenderungen       : %s Datei(en) im Arbeitsverzeichnis\n' "$(git -C "$CHECKOUT" status --porcelain | wc -l)"
  printf 'Installierte CLI  : %s\n' "$(dsh_version || echo '-')"
  printf 'Ziel-Tag          : %s (Version %s)\n' "$TARGET_TAG" "$TARGET_VERSION"
  if have_systemd; then
    printf 'Dienst %-10s : %s\n' "$SERVICE" "$(systemctl --user is-active "$SERVICE" || true)"
    printf 'Dienst %-10s : %s\n' "$SECONDARY_SERVICE" "$(systemctl --user is-active "$SECONDARY_SERVICE" || true)"
  else
    printf 'Dienst            : systemd --user nicht erreichbar (falsche Shell? XDG_RUNTIME_DIR pruefen)\n'
  fi
  [ -f "$STATE_FILE" ] && printf 'Letztes Update    : %s\n' "$STATE_FILE"
}

do_preflight() {
  step "Preflight (nur lesend)"
  local fehler=0
  local session_id
  session_id="$(printenv DSH_SESSION_ID 2>/dev/null || true)"

  if [ -n "$session_id" ] && [ "$FORCE" -eq 0 ]; then
    warn "Diese Shell laeuft INNERHALB einer DSH-Session (DSH_SESSION_ID=$session_id)."
    warn "Der Dienst-Stopp wuerde die eigene Session beenden. Aufruf ueber SSH/Konsole."
    fehler=1
  fi

  [ -d "$CHECKOUT/.git" ] || { warn "kein Git-Checkout: $CHECKOUT"; fehler=1; }

  local dirty
  dirty="$(git -C "$CHECKOUT" status --porcelain | wc -l)"
  if [ "$dirty" -ne 0 ]; then
    warn "$dirty lokale Aenderungen im Checkout - erst committen oder verwerfen."
    git -C "$CHECKOUT" status --short | head -20
    fehler=1
  fi

  log "Pruefe Ziel-Tag auf origin ..."
  if git -C "$CHECKOUT" ls-remote --tags origin "refs/tags/$TARGET_TAG" 2>/dev/null | grep -q "refs/tags/$TARGET_TAG"; then
    log "  Tag vorhanden: $TARGET_TAG"
  else
    warn "Tag $TARGET_TAG auf origin nicht gefunden (Netz? Name?)"
    fehler=1
  fi

  local node_major node_minor pnpm_v
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  node_minor="$(node -p 'process.versions.node.split(".")[1]')"
  if [ "$node_major" -lt 22 ] || { [ "$node_major" -eq 22 ] && [ "$node_minor" -lt 19 ]; }; then
    warn "Node $node_major.$node_minor zu alt - DSH 0.1.7 verlangt ^22.19.0 || >=24.0.0"
    fehler=1
  else
    log "  node $(node -v) ok"
  fi
  pnpm_v="$(pnpm --version 2>/dev/null || echo '-')"
  case "$pnpm_v" in
    11.7.*) log "  pnpm $pnpm_v ok" ;;
    -) warn "pnpm nicht gefunden"; fehler=1 ;;
    *) warn "pnpm $pnpm_v weicht vom gepinnten packageManager pnpm@11.7.0 ab (meist tolerierbar)" ;;
  esac

  command -v gcc >/dev/null 2>&1 || warn "gcc fehlt (native-system Build)"
  command -v python3 >/dev/null 2>&1 || warn "python3 fehlt (native-system Build)"
  command -v curl >/dev/null 2>&1 || warn "curl fehlt (Port-Check)"

  local frei
  frei="$(df -Pk "$CHECKOUT" | awk 'NR==2 {print $4}')"
  if [ "$frei" -lt 26214400 ]; then
    warn "weniger als 25 GB frei unter $CHECKOUT ($frei KB)"
    fehler=1
  else
    log "  Speicher frei: $(( frei / 1024 / 1024 )) GB"
  fi

  [ -d "$DSH_HOME_DIR" ] || { warn "$DSH_HOME_DIR fehlt"; fehler=1; }
  [ -x "$BACKUP_SCRIPT" ] || warn "$BACKUP_SCRIPT nicht ausfuehrbar - Backup faellt auf tar zurueck"

  if have_systemd; then
    log "  Dienststatus $SERVICE: $(systemctl --user is-active "$SERVICE" || true)"
  else
    warn "systemd --user nicht erreichbar - stop/start/verify brauchen eine Login-Shell"
  fi

  if [ "$fehler" -eq 0 ]; then
    log "Preflight ok."
  else
    die "Preflight hat Probleme gefunden (siehe Warnungen)."
  fi
}

do_backup() {
  step "Backup"
  local tarball
  if [ -x "$BACKUP_SCRIPT" ]; then
    log "Vorhandenes Backup-Skript: $BACKUP_SCRIPT"
    run "$BACKUP_SCRIPT"
  else
    warn "kein Backup-Skript - eigenes tar"
    run mkdir -p "$BASE_DIR"
    run tar --exclude='*.log' --exclude='*/cache/*' -czf "$BASE_DIR/dsh_backup_$STAMP.tar.gz" -C /home/daniel .dsh
  fi
  tarball="$(ls -t "$BASE_DIR"/dsh_backup_*.tar.gz 2>/dev/null | head -1 || true)"

  SNAPSHOT_DIR="$BASE_DIR/update-$STAMP"
  run mkdir -p "$SNAPSHOT_DIR"
  PREV_REV="$(git -C "$CHECKOUT" rev-parse HEAD)"
  PREV_TAG="$(git -C "$CHECKOUT" describe --tags --always 2>/dev/null || echo unknown)"
  PREV_VERSION="$(dsh_version || echo unknown)"
  TARGET_REV="$(git -C "$CHECKOUT" ls-remote --tags origin "refs/tags/$TARGET_TAG" | awk '{print $1}')"
  HOME_TARBALL="$tarball"

  log "Ist-Zustand: rev=$PREV_REV tag=$PREV_TAG version=$PREV_VERSION"
  log "Ziel        : tag=$TARGET_TAG rev=$TARGET_REV"

  run cp -a "$DSH_HOME_DIR/settings.yaml" "$SNAPSHOT_DIR/settings.yaml"
  run cp -a "$DSH_HOME_DIR/cordis.patch.yml" "$SNAPSHOT_DIR/home-cordis.patch.yml"
  run cp -a "$DSH_HOME_DIR/profiles/web/cordis.patch.yml" "$SNAPSHOT_DIR/web-cordis.patch.yml"
  if [ "$DRY_RUN" -eq 0 ]; then
    git -C "$CHECKOUT" rev-parse HEAD > "$SNAPSHOT_DIR/git-rev-before.txt"
    printf '%s\n' "$PREV_VERSION" > "$SNAPSHOT_DIR/dsh-version-before.txt"
    dsh --profile web --dump-config > "$SNAPSHOT_DIR/dump-config-before.yml" 2>&1 || warn "dump-config vorher fehlgeschlagen (nicht kritisch)"
    find "$DSH_HOME_DIR/sessions" -maxdepth 2 -name 'session.v*.jsonl.zstd' 2>/dev/null | wc -l > "$SNAPSHOT_DIR/session-count-before.txt"
    du -sh "$DSH_HOME_DIR" > "$SNAPSHOT_DIR/dsh-home-size-before.txt" 2>/dev/null || true
  else
    printf '  [dry-run] Snapshot-Metadaten schreiben\n'
  fi

  if [ "$DRY_RUN" -eq 0 ]; then
    save_state
  else
    printf '  [dry-run] Zustandsfile %s nicht geschrieben\n' "$STATE_FILE"
  fi
  log "Backup abgeschlossen: $tarball"
}

do_stop() {
  step "Dienste stoppen"
  if ! have_systemd; then
    if [ "$DRY_RUN" -eq 1 ]; then
      printf '  [dry-run] systemctl --user stop %s %s (systemd hier nicht erreichbar)\n' "$SECONDARY_SERVICE" "$SERVICE"
      return 0
    fi
    die "systemctl --user nicht erreichbar - in einer Login-Shell ausfuehren"
  fi
  run systemctl --user stop "$SECONDARY_SERVICE" || true
  run systemctl --user stop "$SERVICE"
  if [ "$DRY_RUN" -eq 0 ]; then
    local i=0
    while service_active && [ "$i" -lt 30 ]; do sleep 1; i=$(( i + 1 )); done
    service_active && die "Dienst laeuft noch - bitte manuell pruefen: systemctl --user status $SERVICE"
    log "Dienst gestoppt."
  fi
}

do_update() {
  step "Checkout auf $TARGET_TAG"
  if service_active && [ "$FORCE" -eq 0 ]; then
    die "Dienst $SERVICE laeuft noch. Erst 'stop' ausfuehren (oder --force)."
  fi
  local dirty
  dirty="$(git -C "$CHECKOUT" status --porcelain | wc -l)"
  [ "$dirty" -eq 0 ] || die "$dirty lokale Aenderungen im Checkout - Abbruch."

  run env GIT_TERMINAL_PROMPT=0 git -C "$CHECKOUT" fetch --tags --prune origin
  run git -C "$CHECKOUT" checkout --detach "$TARGET_TAG"
  if [ "$DRY_RUN" -eq 0 ]; then
    local ist
    ist="$(git -C "$CHECKOUT" describe --tags --exact-match 2>/dev/null || git -C "$CHECKOUT" describe --tags --always)"
    [ "$ist" = "$TARGET_TAG" ] || die "Checkout steht auf $ist, erwartet $TARGET_TAG"
    log "Checkout: $ist"
  fi

  step "Abhaengigkeiten installieren"
  if [ "$FROZEN" -eq 1 ]; then
    run pnpm --dir "$CHECKOUT" install --frozen-lockfile
  else
    run pnpm --dir "$CHECKOUT" install
  fi

  if [ "$DO_CLEAN" -eq 1 ]; then
    step "Build-Ausgabe aufraeumen"
    run pnpm --dir "$CHECKOUT" run clean
  fi

  step "Bauen (native-system + lib host/client + web)"
  log "Das dauert typischerweise 10-30 Minuten; Log: $LOG_FILE"
  run pnpm --dir "$CHECKOUT" run build

  step "Build verifizieren"
  if [ "$DRY_RUN" -eq 0 ]; then
    local v
    v="$(dsh_version || echo '-')"
    log "gebaut: dsh --version -> $v"
    [ "$v" = "$TARGET_VERSION" ] || die "Version $v != erwartet $TARGET_VERSION"
    node -e '
      const fs = require("node:fs");
      const p = process.argv[1];
      const r = JSON.parse(fs.readFileSync(p, "utf8"));
      console.log("  client-build-environment:", JSON.stringify(r.environment));
      if (r.environment.DSH_CLIENT_VERSION !== process.argv[2]) {
        console.error("FEHLER: Client-Build-Record zeigt " + r.environment.DSH_CLIENT_VERSION);
        process.exit(1);
      }
    ' "$CHECKOUT/.dsh-build/client-build-environment.json" "$TARGET_VERSION"
  fi
}

do_start() {
  step "Dienste starten"
  if ! have_systemd; then
    if [ "$DRY_RUN" -eq 1 ]; then
      printf '  [dry-run] systemctl --user start %s %s (systemd hier nicht erreichbar)\n' "$SERVICE" "$SECONDARY_SERVICE"
      return 0
    fi
    die "systemctl --user nicht erreichbar - in einer Login-Shell ausfuehren"
  fi
  run systemctl --user start "$SERVICE"
  run systemctl --user start "$SECONDARY_SERVICE" || true
  log "gestartet - Verifikation mit: $0 verify"
}

do_verify() {
  step "Verifikation"
  printf 'Installierte Version : %s\n' "$(dsh_version || echo '-')"
  printf 'Checkout             : %s\n' "$(git -C "$CHECKOUT" describe --tags --always)"
  if have_systemd; then
    printf 'Dienst %s        : %s\n' "$SERVICE" "$(systemctl --user is-active "$SERVICE" || true)"
  fi
  if command -v curl >/dev/null 2>&1; then
    local code i=0
    code="000"
    while [ "$i" -lt 60 ]; do
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/" || true)"
      [ "$code" != "000" ] && break
      sleep 2; i=$(( i + 2 ))
    done
    if [ "$code" = "000" ]; then
      warn "Port $PORT antwortet nicht - Journal pruefen: journalctl --user -u $SERVICE -n 100"
    else
      log "HTTP $code auf http://127.0.0.1:$PORT/ (jede Antwort ungleich 000 beweist den Listener)"
    fi
  fi
  if have_systemd; then
    step "Journal (letzte Zeilen, Fehlerfilter)"
    journalctl --user -u "$SERVICE" -n 200 --no-pager 2>/dev/null | grep -i -E 'error|fail|warn' | tail -20 || log "  keine Fehlerzeilen gefunden"
  fi
  step "Naechste manuelle Schritte"
  cat <<'NAECHSTE'

  1. Web-UI oeffnen und pruefen: Sessions-Liste, MCP-Tools (openbrain-*), Settings.
  2. Einstellungen: ~/.dsh/settings.yaml wurde beim ersten Start einmalig importiert
     und nach ~/.dsh/settings.yaml.imported umbenannt. Abschnitt 'agent-presets' hat in
     0.1.7 kein Ziel-Entry und bleibt nur in der .imported-Datei.
  3. Preset-Migration (falls noch nicht geschehen): Kandidat aus
     dsh_playground/dsh_update_0.1.7/cordis.patch.candidate.yml reviewen und nach
     ~/.dsh/profiles/web/cordis.patch.yml kopieren (vorher Sicherung anlegen).
  4. Alte Sessions (V3) oeffnen: sie werden beim Oeffnen gelesen und beim Schreiben als
     V4-Nachfolger neben der V3-Datei abgelegt. Die V3-Dateien bleiben unveraendert.
  5. Rollback, falls noetig: scripts/update_dsh.sh rollback --yes [--restore-home]

NAECHSTE
}

do_rollback() {
  step "Rollback"
  load_state
  [ "$ASSUME_YES" -eq 1 ] || confirm "Rollback auf $PREV_TAG ($PREV_REV) durchfuehren?"
  log "Ziel: $PREV_TAG ($PREV_REV) - Backup: $HOME_TARBALL"
  have_systemd && { run systemctl --user stop "$SECONDARY_SERVICE" || true; run systemctl --user stop "$SERVICE"; }
  run git -C "$CHECKOUT" checkout --detach "$PREV_REV"
  run pnpm --dir "$CHECKOUT" install
  run pnpm --dir "$CHECKOUT" run build
  if [ "$RESTORE_HOME" -eq 1 ]; then
    warn "Home-Restore: ~/.dsh wird durch den Stand von vor dem Update ersetzt."
    run mv "$DSH_HOME_DIR" "$DSH_HOME_DIR.vor-rollback-$STAMP"
    run mkdir -p "$DSH_HOME_DIR"
    run tar -xzf "$HOME_TARBALL" -C /home/daniel .dsh
    log "Home zurueckgeholt - Sessions/Settings sind auf dem Stand vor dem Update."
  else
    log "Home bleibt unveraendert (ohne --restore-home). Hinweis: 0.1.5 kann V4-Sessions"
    log "nicht lesen; betroffene Nachfolgerdateien ggf. aus ~/.dsh/sessions entfernen."
  fi
  have_systemd && { run systemctl --user start "$SERVICE"; run systemctl --user start "$SECONDARY_SERVICE" || true; }
  log "Rollback abgeschlossen - Version pruefen mit: $0 verify"
}

# ---------------------------------------------------------------------- Ablauf
case "$ACTION" in
  status)    do_status ;;
  preflight) do_preflight ;;
  backup)    do_preflight; do_backup ;;
  stop)      do_stop ;;
  update)    do_update ;;
  start)     do_start ;;
  verify)    do_verify ;;
  rollback)  do_rollback ;;
  all)
    [ "$ASSUME_YES" -eq 1 ] || die "Aktion 'all' braucht --yes (stoppt den laufenden Dienst)."
    do_preflight
    do_backup
    do_stop
    do_update
    do_start
    do_verify
    log "FERTIG. Log: $LOG_FILE"
    ;;
esac
