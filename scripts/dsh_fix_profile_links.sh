#!/usr/bin/env bash
# =============================================================================
# dsh_fix_profile_links.sh - Interception-Ebene (profiles/node_modules) aufbauen
#
# WARUM: Plugin-Namen in einem Profil-Patch werden relativ zum Profilverzeichnis
# aufgeloest (Node-Suche: profiles/web/node_modules -> profiles/node_modules ->
# ...). Im 0.1.5-Home existiert dafuer ~/.dsh/profiles/node_modules mit ~240
# Symlinks in den Checkout. Ein frisches 0.1.7-Home hat diese Ebene nicht,
# deshalb koennen Rows aus dem Profil-Patch (z. B. preset-trader) ihre Pakete
# nicht aufloesen und erscheinen nicht in der UI.
#
# Aufruf:
#   dsh_fix_profile_links.sh [--home DIR] [--target-checkout DIR] [--source DIR]
#                            [--check] [--dry-run]
#   --check   nur aufloesen/pruefen, nichts schreiben
# =============================================================================
set -Eeuo pipefail

HOME_DIR="/home/daniel/.dsh-017"
TARGET_CHECKOUT="/home/daniel/deepseek-harness-017"
SOURCE="/home/daniel/.dsh/profiles/node_modules"
CHECK_ONLY=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --home) HOME_DIR="$2"; shift 2 ;;
    --target-checkout) TARGET_CHECKOUT="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --check) CHECK_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unbekanntes Argument: $1" >&2; exit 2 ;;
  esac
done

log() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf 'FEHLER: %s\n' "$*" >&2; exit 1; }
run() {
  if [ "$DRY_RUN" -eq 1 ]; then printf '  [dry-run] %s\n' "$*"; return 0; fi
  printf '  + %s\n' "$*"; "$@"
}

PROFILE_DIR="$HOME_DIR/profiles/web"
PATCH="$PROFILE_DIR/cordis.patch.yml"
DST="$HOME_DIR/profiles/node_modules"

[ -d "$PROFILE_DIR" ] || die "$PROFILE_DIR fehlt"
[ -f "$PATCH" ] || die "$PATCH fehlt"

# --- Referenzierte Paketnamen aus dem Profil-Patch --------------------------
NAMES="$(grep -o "name: '@deepseek-ai/[^']*'" "$PATCH" | sed "s/name: '//; s/'//; s|/list-agents$||" | sort -u)"
COUNT="$(printf '%s\n' "$NAMES" | grep -c . || true)"
log "Pakete im Profil-Patch: $COUNT"

resolve_check() {
  # Aufloesung genau so, wie Node sie aus dem Profilverzeichnis sieht
  printf '%s\n' "$NAMES" | node -e '
    const { createRequire } = require("node:module");
    const req = createRequire(process.argv[1] + "/cordis.yml");
    let ok = 0, bad = 0;
    const lines = require("node:fs").readFileSync(0, "utf8").split("\n").filter(Boolean);
    for (const name of lines) {
      try { req.resolve(name); ok++ }
      catch { console.log("  FEHLT  " + name); bad++ }
    }
    console.log("  aufloesbar: " + ok + " / " + (ok + bad));
    process.exit(bad === 0 ? 0 : 1);
  ' "$PROFILE_DIR"
}

if [ "$CHECK_ONLY" -eq 1 ]; then
  log "Pruefe Aufloesung aus $PROFILE_DIR"
  resolve_check && log "alles aufloesbar" || log "nicht alle Pakete aufloesbar (siehe oben)"
  exit 0
fi

# --- Interception-Layer aufbauen -------------------------------------------
[ -d "$SOURCE" ] || die "Quell-Ebene $SOURCE fehlt (kein 0.1.5-Home?)"
step=0
run mkdir -p "$DST" "$HOME_DIR/profiles/web/node_modules"

if [ "$DRY_RUN" -eq 0 ]; then
  projiziert=0
  kaputt=0
  while IFS= read -r link; do
    rel="$(printf '%s' "$link" | sed "s|^$SOURCE/||")"
    ziel="$(readlink "$link")"
    neu="$(printf '%s' "$ziel" | sed "s|/deepseek-harness/|/deepseek-harness-017/|")"
    mkdir -p "$DST/$(dirname "$rel")"
    ln -sfn "$neu" "$DST/$rel"
    projiziert=$(( projiziert + 1 ))
    [ -e "$neu" ] || kaputt=$(( kaputt + 1 ))
  done < <(find "$SOURCE" -type l)
  log "Links projiziert: $projiziert (davon $kaputt mit fehlendem Ziel im neuen Checkout)"
  # Zusaetzlich: alle im Patch genannten Pakete direkt aus dem neuen Checkout verlinken,
  # falls die Projektion sie nicht erwischt (Layout-Unterschiede zwischen den Versionen).
  fehlend=0
  for name in $NAMES; do
    pfad="$DST/$name"
    if [ ! -e "$pfad" ]; then
      # Reihenfolge: direktes pnpm-Paket der CLI, dann Node-Aufloesung aus apps/cli,
      # dann Workspace-Quellpaket unter packages/<gruppe>/<name>.
      echt=""
      for kandidat in "$TARGET_CHECKOUT/apps/cli/node_modules/$name" "$TARGET_CHECKOUT/apps/cli/node_modules/@deepseek-ai/$name"; do
        if [ -e "$kandidat" ]; then echt="$kandidat"; break; fi
      done
      if [ -z "$echt" ]; then
        echt="$(cd "$TARGET_CHECKOUT/apps/cli" && node -e "try{console.log(require.resolve(process.argv[1]))}catch(e){process.exit(1)}" "$name" 2>/dev/null || true)"
      fi
      if [ -z "$echt" ]; then
        kurz="$(printf '%s' "$name" | sed 's|^@deepseek-ai/||')"
        quelle="$(grep -rl "\"name\": \"$name\"" "$TARGET_CHECKOUT/packages" --include=package.json 2>/dev/null | head -1)"
        [ -n "$quelle" ] && echt="$(dirname "$quelle")"
      fi
      if [ -n "$echt" ]; then
        mkdir -p "$(dirname "$pfad")"
        ln -sfn "$echt" "$pfad"
        log "  ergaenzt: $name -> $echt"
      else
        printf '  WARNUNG: %s im neuen Checkout nicht gefunden\n' "$name"
        fehlend=$(( fehlend + 1 ))
      fi
    fi
  done
  log "nicht aufloesbare Pakete: $fehlend"
fi

log "Kontrolle:"
resolve_check && log "alles aufloesbar" || log "weiterhin Luecken (siehe oben)"
log "Fertig. Dienst neu starten: systemctl --user restart dsh-native-017"
