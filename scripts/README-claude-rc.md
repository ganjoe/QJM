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

* **Zustimmung wird automatisch gegeben:** `claude remote-control` fragt beim
  ersten Start `Enable Remote Control? (y/n)`. Ohne TTY wartet der Server
  endlos auf diese Antwort und legt **nie** eine Session an - genau das war am
  29.09.2026 der Fehler. Einen `--yes`-Schalter gibt es nicht (siehe
  `claude remote-control --help`). Der Launcher hält deshalb einen FIFO mit
  beiden Enden offen, in dem `y` bereitliegt.
* **Kein `--continue`** (bewusst): `--continue` unterdrueckt
  `--create-session-in-dir`. Beim allerersten Start gibt es aber keine
  Vorgaenger-Session zum Fortsetzen - der Server legt dann **gar keine** Session
  an, und in der Oberflaeche ist nichts zu sehen. Genau das ist am 29.09.
  passiert. Ohne den Schalter legt der Server beim Start eine Session im
  Projektverzeichnis an (`--create-session-in-dir`, Standard an).
* Auch **spaeter nicht** `--continue` setzen, obwohl es verlockend klingt:
  laut `--help` bricht es mit Fehler ab, wenn in diesem Verzeichnis nichts
  innerhalb der **letzten ~4 Stunden** aufgezeichnet wurde. Bei einem Dienst mit
  `Restart=always` ergibt das nach laengerer Auszeit eine Neustartschleife -
  schlimmer als die kosmetischen Alt-Sessions.
* Parallel-Sessions: Standard-Kapazitaet ist 32, es braucht kein `--capacity`.
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
