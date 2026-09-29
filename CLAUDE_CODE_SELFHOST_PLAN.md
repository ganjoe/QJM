# Claude Code parallel zu DSH selbst hosten — Installationsplan

**Stand:** 29.09.2026 · **Ziel:** Claude Code läuft auf dem QJM-Server und arbeitet wie DSH am Projekt `/home/daniel/QJM`, bedient über eine Website vom Client aus — ohne Arbeit in der Claude-CLI. Subscription: **Claude Pro**.

> **Umsetzungsstand (29.09.2026, verifiziert).** Weg A läuft. Login (Pro/claude.ai), Workspace-Trust, `remoteControlAtStartup` und die systemd-User-Unit `claude-rc.service` sind eingerichtet; der Dienst läuft stabil und die Session **QJM** ist von claude.ai/code aus erreichbar. Ende-zu-Ende-Test bestanden: ein aus dem Browser gegebener Auftrag hat `dsh_playground/rc-smoke.txt` **auf dem Server** angelegt.
>
> Artefakte in `scripts/`: `claude-rc.sh` (Launcher), `claude-rc.service` (User-Unit), `setup-claude-rc.sh` (Einrichtung), `rc-status.sh` (Diagnose), `README-claude-rc.md` (Runbook).
>
> Zwei Erkenntnisse, die den Aufbau bestimmen: (1) `claude setup-token` ist für Remote Control **unbrauchbar** — es braucht einen Full-Scope-Login. (2) Ohne TTY bleibt der Server an der einmaligen Frage `Enable Remote Control? (y/n)` hängen und legt **nie** eine Session an; es gibt keinen `--yes`-Schalter, der Launcher beantwortet sie deshalb über einen FIFO.

---

## 1. Empfehlung in einem Absatz

Es gibt **kein offizielles, selbst hostbares Claude-Code-Web-Frontend** — anders als bei DSH, dessen UI unter `http://<server>:3091` auf deiner Maschine läuft. Der offizielle Weg, der deine Anforderung erfüllt, heißt **Remote Control**: Der *Agent* läuft lokal auf deinem Server (voller Dateisystem- und Shell-Zugriff auf `/home/daniel/QJM`), die *Bedienoberfläche* ist `claude.ai/code` im Browser deines Clients. Das ist auf **Pro freigeschaltet**, braucht **keinen offenen Port** auf dem Server und keine CLI-Bedienung nach der Einrichtung. Ich empfehle diesen Weg (Abschnitt 5) und optional `rcpilot` als eigenes Web-UI obendrauf (Abschnitt 7, B1).

**Wichtig und überraschend:** Der Token, den du anbieten wolltest (`claude setup-token`), funktioniert für diesen Weg **nicht**. Die Doku ist explizit: *"Remote Control requires a full-scope login token … These tokens can only make model requests, so they can't establish Remote Control sessions."* Nötig ist ein einmaliger Browser-Login (`claude auth login`). Den kann ich headless starten und dir die URL geben — **du brauchst dafür keinen Token**, nur einen Browser.

---

## 2. Ist-Zustand deines Servers (geprüft, nicht angenommen)

| Punkt | Befund |
|---|---|
| OS / Runtime | Ubuntu 25.10, Node **22.23.2**, npm 10.9.8, pnpm 11.7.0 |
| Claude Code | **2.1.283** installiert (`/home/daniel/.npm-global/bin/claude`), `claude auth status` → **loggedIn: false** |
| Vorhanden | `tmux` ✓, `script`/`socat` (PTY) ✓, `git` ✓, `docker` ✓, `jq` ✓ |
| Fehlt | nginx, caddy, Tailscale, cloudflared, ngrok |
| systemd | `user@1000.service` läuft, **`Linger=no`** |
| Belegte Ports | 3090+**3091** (DSH), 8000, 3000–3002, 4000/4002, 5900 (VNC), **6901** (Web-Desktop), 8080er-Range der MCP-Dienste |
| Freie Ports | **8010**, 8011, 3092, 3095 |
| Projekt | `/home/daniel/QJM` ist git-Repo (`origin github.com/ganjoe/QJM.git`), 5,3 GB, **Arbeitsbaum hat uncommitted changes**, 2,4 TB frei |
| Sandbox | Mein Schreibrecht endet bei `/home/daniel/QJM` — Schritte nach `~/.claude` oder `/etc/systemd` brauchen deine Freigabe oder deine Hand |

Zwei Konsequenzen daraus: (a) Der Login-Flow ist ohne TTY startbar — ich habe es getestet, die CLI druckt die OAuth-URL und wartet auf den Code. (b) Für den Autostart empfehle ich eine **System-Unit** statt einer User-Unit, dann entfällt das `loginctl enable-linger`-Gefummel.

---

## 3. Die Rechtslage 2026 — warum die Architekturwahl hier kein Detail ist

Das war 2026 in Bewegung und bestimmt, was du benutzen darfst:

| Datum | Ereignis |
|---|---|
| 20.02.2026 | Anthropic schärft die Terms: **Drittanbieter-Harnesses mit Claude-Abos untersagt** (The Register) |
| 04.04.2026 | Harte Sperre: Abos dürfen **keine Drittagenten** (OpenClaw & Co.) mehr versorgen — Begründung: diese Harnesses umgehen den Prompt-Cache und verbrennen unverhältnismäßig Compute |
| 14.05.2026 | **Wiederfreigabe mit Auflage**: neuer, separater **"Agent SDK"-Credit-Topf**, monatlich, gedeckelt, **nicht übertragbar, verfällt am Monatsende** |
| ab 15.06.2026 | Credits aktiv; einmaliges Opt-in nötig |

Was in welchen Topf fällt (offizielle Help-Center-Seite *"Use the Claude Agent SDK with your Claude plan"*):

* **Normale interaktive Claude-Code-Nutzung** (also auch **Remote Control**) → läuft gegen die **normalen Pro-Limits** (5-Stunden-Fenster + Wochengrenzen).
* **Agent SDK, `claude -p`, Claude-Code-GitHub-Actions und Drittanbieter-Apps, die sich per Agent SDK an ein Abo authentifizieren** → laufen gegen den **Agent-SDK-Credit**. Auf **Pro: 20 USD/Monat**, einmalig freischalten, kein Rollover; danach stoppen die Requests (oder es wird zu API-Preisen abgerechnet, wenn du Extra-Usage aktivierst).

**Die Schlussfolgerung für dein Vorhaben:** Je näher die Lösung am offiziellen Harness bleibt, desto besser. Remote Control ist der offizielle Harness, interaktiv bedient → normales Pro-Budget. Jeder Eigenbau oder Fremd-Client, der das Abo über Agent-SDK-Auth nutzt, landet in einem 20-USD-Topf, der bei agentischer Arbeit an einem 5,3-GB-Repo nach wenigen Stunden leer sein kann. Dazu kommt das Policy-Risiko: dieselbe Praxis war im April verboten und ist erst seit Mai wieder erlaubt.

---

## 4. Zielarchitektur (Weg A)

```
  DEIN CLIENT                          DEIN SERVER (QJM)                      ANTHROPIC
┌────────────────────┐              ┌──────────────────────────────┐      ┌──────────────┐
│ Browser            │              │ systemd: claude-rc.service   │      │ api.anthropic│
│ claude.ai/code     │◀─── WSS ────▶│  └─ claude remote-control     │─────▶│ .com         │
│ (oder Claude-App)  │  ausgehend   │      cwd: /home/daniel/QJM   │      │ claude.ai    │
└────────────────────┘              │      tmux-Session zur Sicht  │      └──────────────┘
                                    │      Agent + Shell + git     │
                                    │      laufen LOKAL             │
                                    └──────────────────────────────┘
```

* **Kein Inbound-Port, kein Reverse Proxy, kein TLS-Zertifikat.** Der Server baut die Verbindung ausgehend auf. Firewall bleibt zu.
* Der Agent hat vollen Zugriff auf `/home/daniel/QJM` — Dateien, Shell, git, MCP — genau wie DSH.
* Du kannst aus dem Browser **neue Sessions on demand** starten (`--spawn`), parallel arbeiten und Bilder/Dateien vom Client anhängen.
* Unterschied zu DSH, den du bewusst akzeptieren musst: Die **Oberfläche** ist Anthropics Seite, nicht deine. Wenn du eine eigene Seite auf deiner Maschine willst, ist das Option B1/B2 in Abschnitt 7.

---

## 5. Installationsschritte (Weg A)

### S0 — Vorbereitung
```bash
cp ~/.claude.json ~/.claude.json.bak-$(date +%F)     # Trust/State sichern
mkdir -p ~/.claude
```

### S1 — Einmaliger Login (der Schritt, der deinen Token ersetzt)
```bash
claude auth login --claudeai
```
Ablauf (headless verifiziert): Die CLI druckt eine `https://claude.com/cai/oauth/authorize?...`-URL und wartet auf `Paste code here if prompted >`. Du öffnest die URL **im Browser deines Clients**, loggst dich mit dem Pro-Konto ein, kopierst den Code zurück. Die Credentials landen in `~/.claude` — **voll-scope**, das ist genau das, was Remote Control verlangt.
> Wenn `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` oder ein `apiKeyHelper` gesetzt ist, bricht Remote Control mit *"requires claude.ai subscription auth"* ab — vorher entfernen.

### S2 — Workspace-Trust einmalig setzen
In einem Verzeichnis ohne Trust fragt Remote Control *"Trust &lt;dir&gt;? [y/N]"* — **ohne TTY kann es das nicht und bricht mit `Workspace not trusted` ab.** Deshalb einmalig:
```bash
cd /home/daniel/QJM && claude   # "Yes, I trust this folder" bestätigen, dann beenden
```
* **Niemals in `/home/daniel` selbst starten** — im Home-Verzeichnis wird Trust nie gespeichert, die Frage kommt bei jedem Lauf wieder (offizielle Doku).
* Der Trust landet in `~/.claude.json` unter `projects[<git-root>].hasTrustDialogAccepted` (Mechanik gegen den CC-Quellcode verifiziert). Falls Autostart später doch am Trust scheitert, kann man den Eintrag vorab per `jq` setzen — der interaktive Weg ist aber der robuste.
* **Für `--spawn worktree` prüfen:** Jeder Worktree ist ein eigener Pfad/eigenes git-Root. Ob der Trust vererbt wird, ist nicht garantiert (genau dazu gibt es Upstream-Issues). Deshalb starten wir mit `same-dir`.

### S3 — Settings
`~/.claude/settings.json`:
```json
{
  "theme": "dark-ansi",
  "remoteControlAtStartup": true
}
```
`remoteControlAtStartup` lässt jede neue Session automatisch Remote-Control-fähig starten (in Projekt-Settings wird ein `true` bewusst ignoriert, ein `false` respektiert).

### S4 — Autostart als Dienst
Launcher `/home/daniel/QJM/scripts/claude-rc.sh` (ausführbar):
```bash
#!/usr/bin/env bash
set -euo pipefail
export HOME=/home/daniel
export PATH="/home/daniel/.npm-global/bin:$PATH"
cd /home/daniel/QJM
exec claude remote-control \
  --continue \
  --name "QJM" \
  --permission-mode acceptEdits \
  --verbose
```
`/etc/systemd/system/claude-rc.service`:
```ini
[Unit]
Description=Claude Code Remote Control (QJM)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=daniel
Group=daniel
Environment=HOME=/home/daniel
WorkingDirectory=/home/daniel/QJM
ExecStart=/home/daniel/QJM/scripts/claude-rc.sh
Restart=always
RestartSec=15
# Logs: journalctl -u claude-rc -f

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now claude-rc
journalctl -u claude-rc -f      # sollte die Session-URL zeigen
```
Warum `--continue`: Genau die Falle ist dokumentiert (Upstream-Issue *"remote-control: Persistent session that survives reboots"*) — ohne `--continue` erzeugt **jeder Reboot eine neue Session**, und alte Sessions hängen in der Oberfläche als "connected" herum, bis sie serverseitig auslaufen. `--continue`/oder `--session-id` holt die vorige Session zurück.
Optional zum Mitlesen/Debuggen: dieselbe Unit mit `tmux new-session -d -s claude-qjm …` starten, dann `tmux attach -t claude-qjm`.

### S5 — Abnahme (Smoke-Test)
1. Am Client `https://claude.ai/code` öffnen → die Session **QJM** muss in der Liste stehen.
2. Prompt: *"Fasse die Struktur von chart_viewer/ zusammen"* → Antwort streamt.
3. Änderungstest: *"Lege /home/daniel/QJM/dsh_playground/rc-smoke.txt mit 'ok' an"* → `cat` und `git status` prüfen.
4. Parallelität: zweite Session aus dem Browser starten.
5. Reboot-Test: `sudo reboot`, danach darf **keine** Zombie-Session entstehen, die Session muss wieder auftauchen.

### S6 — Bedienung danach
Nur noch Browser: Session wählen, tippen, Dateien/Bilder anhängen, `@` vervollständigt Pfade aus dem Projekt. Kein Terminal nötig. Optional Claude-App auf dem Handy für dieselben Sessions.

---

## 6. Betrieb, Sicherheit, Grenzen

* **Berechtigungen:** `--permission-mode acceptEdits` lässt Datei-Edits ohne Rückfrage zu, Shell-Kommandos fragen weiter nach. `--sandbox`/`--no-sandbox` steuert die Dateisystem-/Netz-Isolierung. `--dangerously-skip-permissions` **nicht** blind setzen: der Agent hat sonst ungeprüfte Shell auf dem Server, der auch deine MCP-Dienste, Docker und Postgres hostet.
* **Konto = Shell:** Wer Zugriff auf dein claude.ai-Konto hat, hat vollen Zugriff auf die Dateien, die die Session sieht. Wenn du das absichern willst: in den Claude-Settings **"Require trusted devices"** aktivieren (auf Pro selbst einschaltbar) — dann muss sich jedes Gerät einmal frisch authentifizieren.
* **Parallelbetrieb mit DSH:** Beide Agenten würden im selben Arbeitsbaum schreiben. Regel: nicht gleichzeitig dieselben Dateien anfassen; für parallele Stränge eigene Branches/Worktrees verwenden. (DSH hat gerade selbst uncommitted changes in `chart_viewer/` liegen.)
* **Budget:** Remote Control zählt auf die normalen Pro-Limits (5-h-Fenster + Woche). Modellwahl (Sonnet statt Opus) und `/compact` sind die wirksamen Hebel. Der Agent-SDK-Credit wird hier **nicht** angetastet.
* **Updates:** `claude update` aktualisiert die npm-Installation; die Unit danach `systemctl restart claude-rc`. Versionen pinnen ist sinnvoll, weil sich RC-Flags bewegen (z. B. `-c/--continue` erst ab 2.1.200).
* **Rückbau:** `sudo systemctl disable --now claude-rc`, Unit löschen, `claude auth logout`, `~/.claude.json`-Backup zurückspielen. Nichts davon ist systemweit invasiv — es gibt genau eine Unit und ein Verzeichnis `~/.claude`.

---

## 7. Alternativen, falls du ein *eigenes* Web-UI willst

| Option | Was es ist | Preis/Budget | Bewertung |
|---|---|---|---|
| **B1 `rcpilot`** | Drittprojekt (PyPI, v1.10.2): selbst gehostetes, mobil-optimiertes Web-UI, das Claude-Code-**Remote-Control-Sessions** startet/verwaltet, Git-Diff-Viewer, Projekt-Import, Usage-Anzeige, Watchdog, PWA. Läuft als `systemd --user`-Dienst. | Nutzt den offiziellen Harness + RC → normales Pro-Budget | **Bester Kompromiss**, wenn du eine Seite auf *deiner* Maschine willst. Port 8000 ist bei dir belegt → `8010` nehmen. Nachteile: Drittanbieter, Solo-Projekt, exponiert Session-Kontrolle → nur LAN/VPN + Token/TLS, nicht ins Internet |
| **B2 Eigenbau auf dem Agent SDK** | `@anthropic-ai/claude-agent-sdk` (TS/Python) ist Claude Codes Motor als Bibliothek; du baust dein eigenes Web-Frontend (Streaming, Approval-UI, Session-Storage). | **Agent-SDK-Credit: 20 USD/Monat auf Pro**, Opt-in, verfällt; Auth per `setup-token`/API-Key | Maximale Kontrolle, maximaler Aufwand — und der knappe Credit-Topf ist bei agentischer Arbeit schnell leer. **Hier** passt dein `setup-token` |
| **B3 Cloud-Sessions** | `claude.ai/code` führt Sessions in Anthropics Sandbox gegen ein GitHub-Repo aus | normales Pro-Budget | Null Installation, aber läuft **nicht auf deinem Server** → widerspricht deiner Anforderung |
| **B4 Claude Desktop (Linux, Beta)** | Offizielle Desktop-App mit Code-Tab (parallele Sessions, Diff-Review) auf dem Server; Zugriff über deinen **bereits laufenden Web-Desktop auf Port 6901** | normales Pro-Budget | Offiziell und GUI-stark, aber du bedienst einen ganzen Desktop statt einer Web-App — schwerfällig |
| ✗ Fremd-Harness-Nachbauten | Web-UIs, die den Agent-Loop selbst nachbauen | Agent-SDK-Credit + Policy-Risiko | Genau die Kategorie, die im April 2026 gesperrt war. Nicht empfehlen |

Ohne Tailscale/Cloudflare-Tunnel ist der Zugriff auf ein eigenes UI (B1/B2) nur im LAN möglich — bei RC (Weg A) stellt sich die Frage gar nicht, weil nichts exponiert wird.

---

## 8. Was ich brauche, damit es losgeht

1. **Entscheidung:** Weg **A** (empfohlen) · **A + B1** (eigene Web-UI zusätzlich) · **B2** (Eigenbau).
2. **Für A:** nur den **Browser-Login** in S1 — ich starte den Flow, gebe dir URL und Codefeld; **kein Token nötig**. Plus einmalig `sudo` für die Unit in S4.
3. **Für B2:** dann tatsächlich `claude setup-token` → Token + einmaliges Opt-in für die Agent-SDK-Credits im Claude-Konto.
4. **Für mich:** Freigabe für Schreibzugriffe außerhalb `/home/daniel/QJM` (`~/.claude`, `/etc/systemd/system`) — mein Sandbox-Schreibrecht endet dort. Alternativ führe ich nichts aus und du spielst die Skripte aus diesem Plan per SSH ein.

Danach setze ich S0–S5 um und verifiziere mit dem Smoke-Test.

---

## 9. Offene Risiken

* **Policy-Churn:** Die Regeln für Drittanbieter-Auth haben sich 2026 zweimal gedreht. Weg A ist davon am wenigsten betroffen, weil er der offizielle Harness ist.
* **Abhängigkeit von claude.ai/code:** Es gibt kein self-hosted Frontend; Anthropics Seite ist die Oberfläche. Ausfall dort = keine Bedienung (der lokale Agent läuft weiter).
* **Trust-Falle:** Ohne TTY kein Trust-Dialog. Einmalig interaktiv setzen, sonst startet der Dienst nach jedem neuen Pfad nicht.
* **Stale Sessions nach Reboot:** bekanntes Upstream-Thema; durch `--continue` entschärft, im Abnahmetest explizit geprüft.
* **Pro-Limits:** Bei intensiver agentischer Nutzung sind die 5-h-/Wochenfenster der begrenzende Faktor, nicht die Installation.

---

## Quellen

* [Remote Control — Claude Code Docs](https://code.claude.com/docs/en/remote-control) (Pro/Max/Team/Enterprise; Full-Scope-Login; `--spawn`, `--capacity`, Trust-Verhalten)
* [Feature availability — Claude Code Docs](https://code.claude.com/docs/en/feature-availability) (Remote Control nur mit Claude-Abo)
* [Agent SDK overview — Claude Code Docs](https://code.claude.com/docs/en/agent-sdk/overview) (Drittanbieter dürfen claude.ai-Login nicht anbieten; SDK ≠ CLI)
* [Use the Claude Agent SDK with your Claude plan — Claude Help Center](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan) (Credits, Abgrenzung zu interaktiver Nutzung)
* [Claude Code error reference](https://code.claude.com/docs/en/errors) (`Workspace not trusted`, `requires a full-scope login token`)
* [The Register, 20.02.2026](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/) · [GIGAZINE, 14.05.2026](https://gigazine.net/gsc_news/en/20260514-anthropic-claude-agent-sdk-credits) · [Le Fil IA, 13.05.2026](https://www.lefilia.fr/article/3764361-anthropic-retablit-openclaw-et-les-agents-tiers-sur-les-abonnements-claude-mais-sous-conditions) (Timeline & Credit-Modell)
* [heyobi/claude-code-server](https://github.com/heyobi/claude-code-server) (tmux+systemd-Session-Pool, Trust-Schritt) · [anthropics/claude-code#29748](https://github.com/anthropics/claude-code/issues/29748) (Stale Sessions nach Reboot) · [iloom-cli#804](https://github.com/iloom-ai/iloom-cli/issues/804) (Trust-Mechanik `hasTrustDialogAccepted`)
* [rcpilot auf PyPI](https://pypi.org/project/rcpilot/) (Option B1)
