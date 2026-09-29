#!/usr/bin/env bash
set -e

# ==============================================================================
# start_claude_remote.sh
# Startet Claude Code mit Remote Control in einer persistenten tmux-Sitzung.
# ==============================================================================

SESSION_NAME="claude-remote"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Claude Code Remote Control Starter ==="
echo "Projektverzeichnis: $PROJECT_DIR"
echo ""

# 1. tmux prüfen
if ! command -v tmux >/dev/null 2>&1; then
    echo "[-] tmux ist noch nicht installiert. Bitte installieren mit:"
    echo "    sudo apt update && sudo apt install -y tmux"
    exit 1
fi

# 2. Node & npm prüfen
if ! command -v npm >/dev/null 2>&1; then
    echo "[-] npm wurde nicht im Pfad gefunden. Bitte stelle sicher, dass Node.js installiert ist."
    exit 1
fi

# 3. claude CLI prüfen
if ! command -v claude >/dev/null 2>&1; then
    echo "[*] Claude Code CLI nicht gefunden. Installiere @anthropic-ai/claude-code..."
    npm install -g @anthropic-ai/claude-code || {
        echo "[-] Installation mit globalen Rechten fehlgeschlagen. Bitte versuche:"
        echo "    sudo npm install -g @anthropic-ai/claude-code"
        exit 1
    }
fi

# 4. Prüfen, ob Session bereits läuft
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "[+] Bestehende tmux-Session '$SESSION_NAME' gefunden."
    echo "[*] Verbinde zur Session... (Zum Verlassen im Hintergrund: Strg+B, danach D)"
    sleep 1
    tmux attach -t "$SESSION_NAME"
    exit 0
fi

# 5. Neue Session anlegen und Claude Code starten (mit Shell-Fallback, damit Session nicht stirbt)
echo "[+] Erstelle neue tmux-Session '$SESSION_NAME'..."
tmux new-session -d -s "$SESSION_NAME" -c "$PROJECT_DIR" "bash -l -c 'claude remote-control || claude; exec bash'"

echo ""
echo "=================================================================="
echo "  Claude Code Remote Control läuft in tmux-Session '$SESSION_NAME'!"
echo "=================================================================="
echo ""
echo "Wichtige Hinweise:"
echo "1. Verbinde dich jetzt mit der Session, um den Kopplungs-Link / QR-Code zu sehen:"
echo "   tmux attach -t $SESSION_NAME"
echo ""
echo "2. Öffne den angezeigten Link in deinem Browser am Client-PC."
echo ""
echo "3. Session im Hintergrund weiterlaufen lassen (Detach):"
echo "   Drücke  Strg + B  (loslassen) und danach  D"
echo ""
echo "4. Später jederzeit wieder reinschauen:"
echo "   tmux attach -t $SESSION_NAME"
echo "=================================================================="
echo ""

read -p "Möchtest du dich jetzt direkt verbinden? (J/n): " -n 1 -r
echo
if [[ $REPLY =~ ^[Nn]$ ]]; then
    echo "Alles klar. Du kannst dich jederzeit mit folgendem Befehl einklinken:"
    echo "tmux attach -t $SESSION_NAME"
else
    tmux attach -t "$SESSION_NAME"
fi
