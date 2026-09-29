#!/usr/bin/env bash
# Claude Code Remote Control Server fuer das QJM-Projekt.
# Gestartet von systemd (claude-rc.service). Kein Terminal noetig.
set -euo pipefail

export HOME=/home/daniel
export PATH="/home/daniel/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
cd /home/daniel/QJM

# Remote Control braucht claude.ai-Abo-Auth. Ein API-Key/Auth-Token oder ein
# fremder Endpunkt im Environment wuerde die Session auf API-Auth umstellen
# und Remote Control abschalten.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL 2>/dev/null || true

# ---------------------------------------------------------------------------
# Einmalige Bestaetigung "Enable Remote Control? (y/n)" automatisch beantworten.
#
# Ohne TTY kann der Server die Frage nicht stellen und wartet endlos - es wird
# dann NIE eine Session angelegt (genau das passierte am 29.09.2026). Es gibt
# keinen --yes-Schalter (siehe: claude remote-control --help).
#
# Trick: FIFO mit beiden Enden offen halten. Das "y" liegt im Puffer bereit,
# sobald der Server liest, und stdin bekommt nie EOF.
# Die Zustimmung ist die des Betreibers dieses Servers (Weg A, siehe
# CLAUDE_CODE_SELFHOST_PLAN.md).
# ---------------------------------------------------------------------------
RUNDIR="${XDG_RUNTIME_DIR:-/tmp}"
FIFO="$RUNDIR/claude-rc-stdin"
rm -f "$FIFO"
mkfifo -m 600 "$FIFO"
exec 3<>"$FIFO"          # Lese- UND Schreibende offen halten
printf 'y\n' >&3

# KEIN --continue: laut --help bricht es ab, wenn in diesem Verzeichnis nichts
# innerhalb der letzten ~4 Stunden aufgezeichnet wurde (beim ersten Start also
# immer). Es unterdrueckt zusaetzlich --create-session-in-dir, das die erste
# Session anlegt. Sobald eine Session existiert, kann es fuer
# Reboot-Kontinuitaet wieder gesetzt werden.
exec claude remote-control \
  --name "QJM" \
  --permission-mode acceptEdits \
  --verbose \
  -d \
  --debug-file /home/daniel/QJM/dsh_playground/rc-setup/rc-debug.log \
  < "$FIFO"
