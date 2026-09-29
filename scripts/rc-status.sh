#!/usr/bin/env bash
# Schreibt den Zustand des Claude-RC-Dienstes in eine Datei, die der DSH-Agent
# lesen kann (der Agent hat keinen Zugriff auf die systemd-User-Bus).
OUT="/home/daniel/QJM/dsh_playground/rc-setup/status.txt"
{
  echo "=== Zeit ==="; date -Is
  echo; echo "=== is-active ==="; systemctl --user is-active claude-rc 2>&1
  echo; echo "=== is-enabled ==="; systemctl --user is-enabled claude-rc 2>&1
  echo; echo "=== status ==="; systemctl --user --no-pager status claude-rc 2>&1
  echo; echo "=== journal (letzte 60) ==="; journalctl --user -u claude-rc -n 60 --no-pager 2>&1
  echo; echo "=== Session-URL ==="
  journalctl --user -u claude-rc -n 300 --no-pager 2>/dev/null | grep -oE 'https://claude\.ai/code/session_[A-Za-z0-9_-]+' | tail -1 || echo "(keine URL gefunden)"
  echo; echo "=== tmux ==="; tmux ls 2>&1 || true
} > "$OUT" 2>&1
echo "Zustand geschrieben nach: $OUT"
