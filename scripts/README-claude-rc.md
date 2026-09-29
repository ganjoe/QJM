# scripts/claude-rc – Claude Code Remote Control fuer QJM

Weg A aus `CLAUDE_CODE_SELFHOST_PLAN.md`: Der Agent laeuft auf diesem Server,
bedient wird er im Browser unter https://claude.ai/code (oder in der Claude-App).

## Dateien

| Datei | Zweck |
|---|---|
| `claude-rc.sh` | Startet `claude remote-control` in `/home/daniel/QJM` |
| `claude-rc.service` | systemd-User-Unit (Muster wie `dsh-native*.service`) |
| `install-claude-rc.sh` | Kopiert die Unit, aktiviert und startet den Dienst |

## Autostart einrichten (normale Shell, kein root)

```bash
/home/daniel/QJM/scripts/install-claude-rc.sh
```

Fuer Autostart **nach Reboot ohne Login** einmalig:

```bash
sudo loginctl enable-linger daniel
```

## Bedienung

* Session-URL erscheint im Log: `journalctl --user -u claude-rc -f`
* Im Browser: https://claude.ai/code → Session **QJM**
* Stoppen/Starten: `systemctl --user stop claude-rc` / `restart claude-rc`

## Flag-Entscheidungen (bewusst)

* `--continue` holt nach einem Neustart **dieselbe** Session zurueck. Ohne das
  erzeugt jeder Reboot eine neue Session, und alte bleiben in der Oberflaeche
  als "connected" stehen, bis sie serverseitig auslaufen.
  `--continue` ist **nicht** mit `--capacity`, `--spawn` oder
  `--create-session-in-dir` kombinierbar.
* Wer stattdessen viele parallele Sessions will, tauscht in `claude-rc.sh`
  `--continue` gegen `--capacity 4` (dann aber obige Alt-Session-Besonderheit
  akzeptieren).
* `--permission-mode acceptEdits`: Datei-Aenderungen laufen ohne Rueckfrage,
  Shell-Kommandos fragen weiterhin nach. Fuer weniger Automatik `default`
  setzen, fuer mehr `bypassPermissions` (nur mit Bedacht - der Agent hat sonst
  ungepruefte Shell auf diesem Server).
* `--spawn worktree` ist nicht aktiv: Worktrees sind eigene git-Roots und
  erben den einmalig erteilten Workspace-Trust nicht zuverlaessig.

## Rollback

```bash
systemctl --user disable --now claude-rc
rm ~/.config/systemd/user/claude-rc.service
claude auth logout
```
