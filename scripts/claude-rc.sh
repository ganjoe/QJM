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

exec claude remote-control \
  --continue \
  --name "QJM" \
  --permission-mode acceptEdits \
  --verbose
