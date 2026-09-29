#!/usr/bin/env bash
# Installiert/aktualisiert den Claude-Code-Remote-Control-Dienst als
# systemd-USER-Unit (kein root noetig - passend zu den DSH-Units).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

chmod +x "$SRC_DIR/claude-rc.sh"
mkdir -p "$UNIT_DIR"
install -m 644 "$SRC_DIR/claude-rc.service" "$UNIT_DIR/claude-rc.service"

systemctl --user daemon-reload
systemctl --user enable --now claude-rc
sleep 3
systemctl --user --no-pager status claude-rc || true

echo
echo "Naechste Schritte:"
echo "  Logs ansehen:            journalctl --user -u claude-rc -f"
echo "  Neustart:                systemctl --user restart claude-rc"
echo "  Reboot ohne Login:       sudo loginctl enable-linger $USER"
