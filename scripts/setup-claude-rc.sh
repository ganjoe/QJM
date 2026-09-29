#!/usr/bin/env bash
# ==============================================================================
# setup-claude-rc.sh - Weg A aus CLAUDE_CODE_SELFHOST_PLAN.md
#
# Richtet Claude Code Remote Control fuer das QJM-Projekt ein:
#   1. Login pruefen / herstellen (claude.ai Abo - Remote Control braucht
#      einen Full-Scope-Login, KEINEN setup-token)
#   2. Workspace-Trust fuer das Projektverzeichnis setzen
#   3. remoteControlAtStartup aktivieren
#   4. systemd-User-Unit installieren, aktivieren, starten, verifizieren
#
# MUSS IN DEINER EIGENEN SHELL LAUFEN (nicht in der Agent-Sandbox): dort ist
# alles ausserhalb des Projektordners read-only, und der Login braucht deinen
# Browser. Idempotent - mehrfaches Ausfuehren ist unschaedlich.
# ==============================================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SRC_DIR/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
BACKUP_DIR="$PROJECT_DIR/dsh_playground/rc-setup"
TS="$(date +%F-%H%M%S)"
CLAUDE_JSON="$HOME/.claude.json"
SETTINGS_JSON="$HOME/.claude/settings.json"

say() { printf '\n=== %s ===\n' "$*"; }
die() { printf '\n[FEHLER] %s\n' "$*" >&2; exit 1; }

mkdir -p "$BACKUP_DIR" "$UNIT_DIR" "$HOME/.claude"

say "0. Voraussetzungen"
command -v claude >/dev/null || die "claude nicht im PATH"
command -v jq     >/dev/null || die "jq fehlt (sudo apt install jq)"
echo "claude:  $(command -v claude)  ($(claude --version 2>/dev/null || echo '?'))"
echo "Projekt: $PROJECT_DIR"

say "1. Backups"
if [ -f "$CLAUDE_JSON" ];   then cp -v "$CLAUDE_JSON"   "$BACKUP_DIR/claude.json.bak-$TS"; fi
if [ -f "$SETTINGS_JSON" ]; then cp -v "$SETTINGS_JSON" "$BACKUP_DIR/settings.json.bak-$TS"; fi

say "2. Login (claude.ai Abo) - Code kommt ueber die Datei, nicht ueber die Tastatur"
# Alte, noch wartende Login-Prozesse aufraeumen
pkill -f 'claude auth login' 2>/dev/null || true
sleep 1

if claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
  echo "[ok] Bereits eingeloggt."
else
  CODEFILE="$BACKUP_DIR/login-code.txt"
  LOGINLOG="$BACKUP_DIR/login.log"
  URLFILE="$BACKUP_DIR/login-url.txt"
  rm -f "$CODEFILE" "$LOGINLOG" "$URLFILE"
  : > "$CODEFILE"

  echo "Remote Control braucht einen Full-Scope-Login (setup-token funktioniert NICHT)."
  echo "Der Login liest den Code aus: $CODEFILE"
  echo

  # stdin des Logins ist eine Warteschlange auf $CODEFILE. Der Code wird von
  # aussen (DSH-Agent, per Chat) in diese Datei geschrieben - damit ist kein
  # Tippen und kein Einfuegen im Terminal noetig.
  ( tail -f -n0 "$CODEFILE" ) | claude auth login --claudeai > "$LOGINLOG" 2>&1 &
  LOGIN_PID=$!

  # URL einsammeln (kommt innerhalb weniger Sekunden) und zusaetzlich ablegen
  for _ in $(seq 1 40); do
    if grep -q 'oauth/authorize' "$LOGINLOG" 2>/dev/null; then break; fi
    sleep 1
  done
  LOGIN_URL="$(grep -oE 'https://claude\.com/cai/oauth/authorize[^[:space:]]*' "$LOGINLOG" | head -1 || true)"
  if [ -n "${LOGIN_URL:-}" ]; then
    printf '%s\n' "$LOGIN_URL" > "$URLFILE"
    echo "==================================================================="
    echo "LOGIN-URL (liegt auch in $URLFILE):"
    echo "$LOGIN_URL"
    echo "==================================================================="
  else
    echo "[!] Keine URL im Log gefunden - bitte melden. Log: $LOGINLOG"
  fi

  echo
  echo ">>> WICHTIG: Den Code NICHT hier ins Terminal tippen oder einfuegen."
  echo ">>> Schicke ihn im DSH-Chat an den Agenten. Er legt ihn in"
  echo ">>> $CODEFILE ab, dieser Login laeuft dann von allein weiter."
  echo
  echo "Warte auf den Code (max. 20 Minuten) ..."

  WAITED=0
  while [ ! -s "$CODEFILE" ] && [ "$WAITED" -lt 1200 ]; do
    sleep 2
    WAITED=$((WAITED + 2))
  done

  if [ ! -s "$CODEFILE" ]; then
    pkill -f "tail -f -n0 $CODEFILE" 2>/dev/null || true
    die "Kein Code angekommen - Skript einfach erneut starten"
  fi

  echo "[ok] Code empfangen, Login laeuft ..."
  for _ in $(seq 1 60); do
    if ! kill -0 "$LOGIN_PID" 2>/dev/null; then break; fi
    sleep 1
  done
  pkill -f "tail -f -n0 $CODEFILE" 2>/dev/null || true

  echo "--- Login-Log ---"
  cat "$LOGINLOG"
  claude auth status | grep -q '"loggedIn": *true' || die "Login nicht abgeschlossen (Log oben)"
  echo "[ok] Login erfolgreich."
fi

say "3. Workspace-Trust fuer $PROJECT_DIR"
if [ ! -f "$CLAUDE_JSON" ]; then echo '{}' > "$CLAUDE_JSON"; fi
jq --arg p "$PROJECT_DIR" \
   '.projects = (.projects // {}) | .projects[$p] = ((.projects[$p] // {}) + {hasTrustDialogAccepted: true})' \
   "$CLAUDE_JSON" > "$BACKUP_DIR/claude.json.new"
install -m 600 "$BACKUP_DIR/claude.json.new" "$CLAUDE_JSON"
jq -r --arg p "$PROJECT_DIR" '.projects[$p]' "$CLAUDE_JSON"

say "4. settings.json (remoteControlAtStartup)"
if [ ! -f "$SETTINGS_JSON" ]; then echo '{}' > "$SETTINGS_JSON"; fi
jq '. + {remoteControlAtStartup: true}' "$SETTINGS_JSON" > "$BACKUP_DIR/settings.json.new"
install -m 664 "$BACKUP_DIR/settings.json.new" "$SETTINGS_JSON"
cat "$SETTINGS_JSON"

say "5. Dienst installieren"
chmod +x "$SRC_DIR/claude-rc.sh"
install -m 644 "$SRC_DIR/claude-rc.service" "$UNIT_DIR/claude-rc.service"
systemctl --user daemon-reload
systemctl --user enable --now claude-rc
sleep 8

say "6. Verifikation"
if systemctl --user is-active --quiet claude-rc; then
  echo "[ok] claude-rc laeuft."
else
  echo "[!] claude-rc ist NICHT aktiv. Letzte Logzeilen:"
  journalctl --user -u claude-rc -n 40 --no-pager || true
  if journalctl --user -u claude-rc -n 80 --no-pager 2>/dev/null | grep -qi 'workspace not trusted'; then
    echo
    echo "[HINWEIS] Der Workspace-Trust hat nicht gegriffen. Einmalig beheben mit:"
    echo "    cd $PROJECT_DIR && claude     # 'Yes, I trust this folder' bestaetigen, dann beenden"
    echo "  (Niemals in \$HOME selbst starten - dort wird der Trust nie gespeichert.)"
    echo "  Danach: systemctl --user restart claude-rc"
  fi
  die "Dienst startet nicht - Log oben pruefen"
fi
systemctl --user --no-pager status claude-rc | head -15 || true

say "7. Session-URL"
for i in 1 2 3 4 5 6; do
  URL="$(journalctl --user -u claude-rc -n 60 --no-pager 2>/dev/null \
        | grep -oE 'https://claude\.ai/code/session_[A-Za-z0-9_-]+' | tail -1 || true)"
  if [ -n "${URL:-}" ]; then break; fi
  sleep 5
done
if [ -n "${URL:-}" ]; then
  echo "Session: $URL"
else
  echo "(noch keine URL im Log - siehe: journalctl --user -u claude-rc -f)"
fi

cat <<'EOT'

------------------------------------------------------------------
Fertig. Ab jetzt:

  Bedienung:   https://claude.ai/code  ->  Session "QJM"
  Logs:        journalctl --user -u claude-rc -f
  Neustart:    systemctl --user restart claude-rc
  Stoppen:     systemctl --user stop claude-rc
  Rollback:    systemctl --user disable --now claude-rc

Optional, damit der Dienst auch NACH EINEM REBOOT OHNE LOGIN startet:
  sudo loginctl enable-linger daniel

Hinweis: der alte scripts/start_claude_remote.sh (tmux-Variante) wird nicht
mehr gebraucht; dieser Dienst ersetzt ihn.
------------------------------------------------------------------
EOT
