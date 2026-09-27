# DSH-Update auf v0.1.7-rc.2 — Updateplan

**Stand:** 2026-09-27 · **Status:** Entwurf/Planung · **An der bestehenden Installation wurde nichts geändert**
(alle bisherigen Schritte waren lesend: Git, Dateisystem, npm-Registry, GitHub-Release-API).

Auftrag: Update auf v0.1.7-rc.2 planen, Updateplan und Updatescript erstellen — Installation noch nicht umstellen.

---

## 1. Kurzfassung

| Punkt | Ergebnis |
|---|---|
| Ausgangslage | Quellcode-Installation (Git-Checkout + Symlink-CLI), Version **0.1.5-rc.2**, Commit fb2c4b9e |
| Ziel | Tag **dsh-v0.1.7-rc.2**, Commit 477b4f42 (Pre-Release vom 24.09.2026, zuletzt aktualisiert 27.09.2026) |
| Update-Weg | Checkout umstellen → pnpm install → pnpm run build → Dienst neu starten (dokumentierter Weg aus der README) |
| Ausfallzeit | Dienst-Stopp für die gesamte Dauer: erfahrungsgemäß **10–30 Min** (nicht gemessen; Build umfasst native-system + lib host/client + Web-Frontend) |
| Heikelste Punkte | (1) Agent-Preset **trader** muss von Verzeichnis-Roots auf Plugin-Rows migriert werden, (2) **settings.yaml** wird einmalig importiert und umbenannt, (3) **Session-Log V3 → V4** schränkt den Rollback ein |
| Bereits erledigt | Migrationskandidat für das Preset liegt fertig vor und ist gegen 0.1.7 geprüft; Update-/Rollback-Script ist geschrieben und im Dry-Run getestet |
| Empfehlung | Update in einem ruhigen Fenster über SSH ausführen (nicht aus der Web-Session), mit Script in zwei Boot-Schritten; Preset-Patch erst nach erfolgreichem Smoke-Test scharf schalten |
| **Gewählter Weg (Variante B)** | **Parallelinstallation (Blue-Green):** 0.1.7 wird in eigenem Worktree und eigenem Home auf Port 3092 gebaut und getestet, während 0.1.5 unverändert auf 3090 weiterläuft. Umschalten später in 1–2 Minuten, Rückweg jederzeit — siehe Abschnitt 10 |

**Der wichtigste Satz vorab:** Das Update darf **nicht aus einer laufenden DSH-Session** heraus gestartet werden —
der Dienst-Stopp beendet genau die Session, die den Befehl ausführt. Aufruf über SSH/Konsole
(z. B. von einem anderen Rechner: ssh daniel@10.20.0.23).

---

## 2. Ausgangslage (geprüft)

| Fakt | Wert | Beleg |
|---|---|---|
| Checkout | /home/daniel/deepseek-harness | Verzeichnis, 2,0 GB |
| Zustand | detached HEAD, sauberes Arbeitsverzeichnis (0 Änderungen) | git status --porcelain |
| Revision / Tag | fb2c4b9e698e30edb738bca4cf0618587db7d203 = dsh-v0.1.5-rc.2 | git describe --tags |
| Remote | https://github.com/deepseek-ai/deepseek-harness.git (öffentlich, kein Credential nötig) | git remote -v, git ls-remote |
| CLI-Verkettung | ~/.local/bin/dsh → ~/.npm-global/bin/dsh → <Checkout>/apps/cli/lib/bin.js | ls -l |
| Dienst | systemd --user **dsh-native.service** → /home/daniel/start-dsh-native.sh | Unit-Datei |
| Start-Kommando | dsh --profile web --host 127.0.0.1 --port 3090 --no-open (+ 19 trusted-host-Einträge) | start-dsh-native.sh |
| Zusatz-Ports | socat 0.0.0.0:3091 → 3090 (HTTP), 0.0.0.0:3444 → 3090 (HTTPS mit ~/.dsh/ssl/server.pem) | start-dsh-native.sh |
| Zweiter Dienst | systemd --user **dsh-secretary.service** (arbeitet Workitems ab, startet dsh-Subprozesse) | Unit-Datei |
| DSH-Home | /home/daniel/.dsh (110 MB) | du -sh |
| Home-Patch | ~/.dsh/cordis.patch.yml — 8 MCP-Server (cco, pta, pca, cda, wiq, drawio, journal über streamable-http, shelly über stdio/Deno) | Datei |
| Profil-Patch | ~/.dsh/profiles/web/cordis.patch.yml — nur der alte agent-presets-Block (roots → /home/daniel/QJM/agent-presets) | Datei |
| Profil-Module | ~/.dsh/profiles/node_modules/@deepseek-ai/* sind **Symlinks in den Checkout** (apps/cli/node_modules/…) | ls -l |
| Einstellungen | ~/.dsh/settings.yaml: ui-onboarding, llm-pi-ai (eigene Provider: switchyard/Google, openrouter), agent-default-model (deepseek-official / deepseek-flash), agent-presets (default: trader), ui-theme | Datei |
| Preset | /home/daniel/QJM/agent-presets/trader/{preset.yml, agent.cordis.yml} (724 Zeilen, 21 Paket-Referenzen) | Dateien |
| Sessions | ~/.dsh/sessions: 243 Sessions, 92 MB, Dateien session.v3.jsonl.zstd (**V3**) | find/du |
| Session-Writer | SESSION_FORMAT_VERSION = 3 in packages/core/session/src/types.ts | Quelltext 0.1.5 |
| Build-Record | .dsh-build/client-build-environment.json: DSH_CLIENT_VERSION 0.1.5-rc.2, Hash fb2c4b9, 234 Artefakte | Datei |
| Toolchain | node v22.23.2, pnpm 11.7.0, gcc/g++/make/python3 vorhanden | Checks |
| Anforderungen Ziel | node ^22.19.0 \|\| >=24.0.0, packageManager pnpm@11.7.0 — **unverändert gegenüber 0.1.5** | package.json @ Tag |
| Speicher | 2,4 TB frei, pnpm-Store 1,6 GB (warm) | df, du |
| Netz | registry.npmjs.org und github.com erreichbar (HTTP 200) | curl |
| Lokale DSH-Plugins | **keine** — im QJM-Workspace existiert kein package.json mit dsh-Manifest und keine eigene cordis-Patchdatei | Suche im Workspace |

---

## 3. Delta-Analyse: 0.1.5-rc.2 → 0.1.7-rc.2

Geprüft gegen die Release Notes der sechs Releases (0.1.6-alpha.1 … 0.1.7-rc.2), die Dokumentation und die
Paket-Metadaten des Ziel-Tags.

| # | Änderung | Quelle | Betrifft uns? | Maßnahme |
|---|---|---|---|---|
| 1 | **Agent-Presets werden Plugin-Rows.** @deepseek-ai/dsh-agent-presets (Verzeichnis-Roots) entfällt; neu: @deepseek-ai/dsh-agent-preset-registry + @deepseek-ai/dsh-agent-preset. „The registry neither scans directories nor accepts preset paths." | Release 0.1.7-alpha.1; config-catalog + README @ Tag | **JA — kritisch.** Unser trader-Preset liegt als Verzeichnis vor; der Profil-Patch zielt auf die nicht mehr existierende Row agent-presets | Preset in eine @deepseek-ai/dsh-agent-preset-Row überführen; Kandidat liegt fertig vor (Abschnitt 4.1) |
| 2 | **ptc-runtime-Familie umbenannt, keine Legacy-Aliase.** @deepseek-ai/dsh-workflow-worker-thread existiert in 0.1.7 nicht mehr (npm: keine 0.1.7-Version). Neu im Basis-Bundle: ptc-runtime = @deepseek-ai/dsh-ptc-runtime-node und workflow-ptc = @deepseek-ai/dsh-workflow-ptc | Release 0.1.6-alpha.1; Basis-Bundle @ Tag; npm-Registry-Abfrage | **JA — kritisch im Preset.** Der Compat-Check findet genau diese eine fehlende Referenz | Im Kandidaten automatisch ersetzt: eine Row wird zu zwei Rows (erledigt, geprüft) |
| 3 | **settings.yaml wird einmalig importiert** in die Plugin-Konfiguration des aktiven Profils; die Datei wird vor dem ersten Schreiben nach settings.yaml.imported umbenannt; Abschnitte ohne Ziel-Entry werden verworfen (nur dort protokolliert) | Release 0.1.7-alpha.1; settings-README @ Tag | **JA — mittel.** ui-onboarding → ui-settings-general, llm-pi-ai, agent-default-model, ui-theme existieren weiter; der Abschnitt **agent-presets hat kein Ziel-Entry mehr** | Default-Preset über die Registry-Row setzen (im Kandidaten enthalten); nach dem ersten Start Settings + Modelle prüfen; settings.yaml liegt zusätzlich im Backup |
| 4 | **Session-Log V3 → V4** (finalisierte Baseline V4), Migrationswerkzeug für Entwickler; beim Öffnen wird gelesen/migriert, beim Schreiben entsteht ein V4-Nachfolger neben der unveränderten V3-Datei; V3-Leser verweigern V4 | Release 0.1.7-alpha.1; docs/session-format-status.md + persistence-changes/2026-09-16-session-format-v4.md @ Tag | **JA — mittel (Rollback!).** 243 Alt-Sessions | Kein Vorab-Werkzeug nötig; Backup vor dem Update; beim Rollback Home zurückspielen oder V4-Nachfolger entfernen |
| 5 | **MCP auf offizielles SDK v2**, Ressourcen/URI-Templates; Konfigschema unverändert (serverName, transport stdio/streamable-http, url, headers, command, args, env) | Release 0.1.6-alpha.1; mcp-client-README @ Tag | Nein — unsere 8 Einträge bleiben gültig. Neu: stdio startet zusätzlich einen kurzlebigen Probe-Prozess | Nach dem Start MCP-Tools in der GUI prüfen (openbrain-*), Journal auf Verbindungsfehler ansehen |
| 6 | **DeepSeek-Adapter nur noch Messages-API**, Option protocol entfernt | Release 0.1.7-alpha.1 | Nein — 0 Treffer für „protocol" in settings.yaml, Home-Patch, Profil-Patch, credentials | keine |
| 7 | **PTC läuft in eigenem Prozess**, process.env leer, Zeitlimits (Default 120 s, max 600 s) | Release 0.1.6-alpha.1 | Gering — betrifft run_code/Workflows, die Umgebungsvariablen erwarteten | Workflows nach dem Update stichprobenartig ausführen |
| 8 | Plugin-Abhängigkeiten zur Laufzeit, Plugin Manager kann zur Laufzeit entladen; agent/session-start → agent/created; Workspace-Dateizugriffe auf readBytes vereinheitlicht | Releases 0.1.6-alpha.2 / 0.1.7-alpha.1 | Nein — keine eigenen DSH-Plugins im Workspace | keine |
| 9 | Inspector nicht mehr standardmäßig enthalten (separat installieren) | Release 0.1.7-rc.2 | Nein — im 0.1.5-Web-Bundle gab es keinen Inspector-Eintrag | keine |
| 10 | Ein id-gezielter Patch auf eine **nicht mehr existierende Row** ist nur eine Warnung, kein Fehler | vendor/include/src/index.ts (0.1.5 und Tag identisch) | Entlastend: der alte agent-presets-Block kann den Start nicht verhindern | Alten Block trotzdem ersetzen (Kandidat) |
| 11 | Anforderungen (Node 22.19+/24, pnpm 11.7.0, Windows/Mac-Themen, Desktop-Updater, Auto-Review, Voice) | Releases + package.json @ Tag | Nein | keine |
| 12 | Neue Features (Sessions pinnen/archivieren, Sidebar-Terminal, Shortcuts-Editor, Plugin-Manager, Zeitpläne) | Releases 0.1.6/0.1.7 | Chance, kein Zwang | nach dem Update optional nutzen |

---

## 4. Migrationsdetails

### 4.1 Agent-Preset trader (Kernarbeit)

**Alte Form (0.1.5):** Verzeichnis /home/daniel/QJM/agent-presets/trader/ mit preset.yml + agent.cordis.yml;
gefunden über die Row agent-presets mit config.roots in ~/.dsh/profiles/web/cordis.patch.yml.

**Neue Form (0.1.7):** eine Plugin-Row

~~~yaml
- id: agent-preset-registry
  config:
    default: trader          # Deployment-Default (überschreibbar in den Settings)

- insert:
    - id: preset-trader
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: trader
        description: '…'
        order: 10
        plugins:             # die bisherige Komposition, unverändert eingerückt
          - id: persona
            …
~~~

**Was automatisch erledigt ist** (Skript scripts/dsh_preset_migrate.mjs, bereits gelaufen):

* Die 724-zeilige Komposition ist um zehn Leerzeichen eingerückt unter config.plugins übernommen.
* Die Row workflow-worker-thread (@deepseek-ai/dsh-workflow-worker-thread) wurde in die 0.1.7-Form überführt:
  ptc-runtime (@deepseek-ai/dsh-ptc-runtime-node) + workflow-ptc (@deepseek-ai/dsh-workflow-ptc, provider: spawn).
* preset.yml → description, order: 10; der alte Anzeigename („QJM Trader Agent") hat in 0.1.7 kein Feld mehr,
  die UI zeigt die Preset-ID.
* Compat-Check gegen npm: **alle 21 Paket-Referenzen existieren in 0.1.7-rc.2** (scripts/dsh_preset_compat_check.mjs).

**Was noch manuell zu prüfen ist:** einzelne Row-Konfigurationen (z. B. persona, tool-subagent) können Felder
verloren oder dazugewonnen haben. Deshalb zwei Boot-Schritte (Abschnitt 5) und ein Blick ins Journal.

**Governance nach der Migration:** Quelle der Wahrheit bleibt /home/daniel/QJM/agent-presets/trader/agent.cordis.yml
(Persona-Text, QJM-Komposition). Der Generator erzeugt daraus jederzeit den Profil-Patch:

~~~bash
node /home/daniel/QJM/scripts/dsh_preset_migrate.mjs \
  --preset-dir /home/daniel/QJM/agent-presets/trader \
  --out /home/daniel/QJM/dsh_playground/dsh_update_0.1.7/boot2_trader --default
# dann: scripts/dsh_apply_profile_patch.sh apply <kandidat> --yes
~~~

Änderungen an der Persona werden also weiter im QJM-Repo gepflegt und danach generiert/ausgerollt.

### 4.2 settings.yaml

Beim ersten Start nach dem Update importiert DSH die Abschnitte in Einträge gleicher ID und benennt die Datei
in settings.yaml.imported um. Erwartetes Verhalten hier:

| Abschnitt | Ziel-Entry in 0.1.7 | Erwartung |
|---|---|---|
| ui-onboarding | ui-settings-general | übernommen |
| llm-pi-ai (switchyard/openrouter) | llm-pi-ai | übernommen — **nachprüfen**, das sind die eigenen Provider |
| agent-default-model | agent-default-model | übernommen |
| ui-theme | ui-theme | übernommen |
| agent-presets | **kein Ziel mehr** | verworfen (bleibt in settings.yaml.imported) → Default kommt aus der Registry-Row |

Prüfschritt nach dem Start: Einstellungen → Modelle/Provider; eine Testfrage an das Modell deepseek-flash;
Sichtprüfung von ~/.dsh/settings.yaml.imported.

### 4.3 Sessions (V3 → V4)

* Alte Sessions bleiben auf der Platte unverändert (session.v3.jsonl.zstd); V4-Nachfolger entstehen erst beim Schreiben.
* Kein Batch-Werkzeug nötig — das ist laut Release Notes ein Entwicklerwerkzeug, kein Pflichtschritt.
* **Rollback-Konsequenz:** Nach dem ersten Schreiben in alten Sessions liegt V4 vor, das 0.1.5 nicht lesen kann.
  Rollback daher mit --restore-home (Backup) oder indem die V4-Nachfolger der betroffenen Sessions entfernt werden.

### 4.4 MCP-Server

Home-Patch ~/.dsh/cordis.patch.yml bleibt unverändert gültig. Nach dem Start prüfen:
8 Server, davon 7 über streamable-http (Docker-Services außerhalb von DSH) und shelly über stdio (Deno).
Ein MCP-Ausfall blockiert den Start nicht (failOnStartupError ist Default false), fällt aber im Tool-Angebot auf.

### 4.5 Bewusst nicht Teil dieses Updates

* Kein Umbau der QJM-Services, Docker-Container, MCP-Server oder der Datenbanken.
* Keine Änderung an Inhalten von agent-presets/ oder an MCP-Konfigurationen.
* Keine Migration des SDK-Profils (~/.dsh/profiles/sdk) — es hat keine eigenen Plugins.
* Kein Wechsel auf npm-Paketinstallation (npx @deepseek-ai/dsh) — die Quellcode-Installation bleibt.

---

## 5. Ablaufplan

### Phase 0 — Vorbereitung (ohne Ausfall, ca. 10 Min)

1. Fenster ankündigen: Web-UI und Workitem-Verarbeitung sind während Phase 1–4 weg.
2. Vorabprüfung: scripts/update_dsh.sh preflight (rein lesend).
3. Kandidaten reviewen: dsh_playground/dsh_update_0.1.7/** (Diff gegen den aktuellen Profil-Patch).
4. Sicherstellen, dass keine Session gerade etwas Wichtiges tut (laufende Turns beenden).

### Phase 1 — Backup und Zustand einfrieren (Script: backup)

~~~bash
sudo -u daniel -i          # bzw. normale SSH-Shell als daniel
/home/daniel/QJM/scripts/update_dsh.sh backup
~~~

Ergebnis: ~/backups/dsh/dsh_backup_<stamp>.tar.gz (gesamtes ~/.dsh), zusätzlich Snapshot-Ordner mit
settings.yaml, Home-Patch, Web-Profil-Patch, Git-Revision/Version von vorher, dump-config-vorher,
Session-Zähler — plus Zustandsdatei dsh_update_0.1.7/update-state.env für den Rollback.

### Phase 2 — Dienst stoppen (Script: stop)

~~~bash
/home/daniel/QJM/scripts/update_dsh.sh stop
~~~

Stoppt dsh-secretary (Workitems laufen in ihr Lease zurück) und dsh-native samt socat-Proxies.

### Phase 3 — Checkout, Installation, Build (Script: update)

~~~bash
/home/daniel/QJM/scripts/update_dsh.sh update
~~~

Schritte im Einzelnen:

1. git fetch --tags --prune origin (GIT_TERMINAL_PROMPT=0)
2. git checkout --detach dsh-v0.1.7-rc.2 → Prüfung: git describe --tags --exact-match == dsh-v0.1.7-rc.2
3. pnpm install --frozen-lockfile (Abbruch, falls das Lockfile nicht exakt passt; --no-frozen als Ausweg)
4. pnpm run build → native-system, lib host+client (inkl. neuem Desktop-Bundle-Schritt), Web-Frontend
5. Prüfung: dsh --version == 0.1.7-rc.2 und .dsh-build/client-build-environment.json zeigt 0.1.7-rc.2

Optional (nur falls der Build später Auffälligkeiten zeigt): --clean vor dem Build (pnpm run clean).
Empfehlung: erster Versuch ohne --clean.

### Phase 4 — Boot 1 (Smoke-Test, Preset-Patch mit default: standard)

~~~bash
# Der Applier legt selbst eine Sicherung in dsh_update_0.1.7/patch-backups/ an.
/home/daniel/QJM/scripts/dsh_apply_profile_patch.sh apply \
  /home/daniel/QJM/dsh_playground/dsh_update_0.1.7/boot1_standard/cordis.patch.candidate.yml --yes
/home/daniel/QJM/scripts/update_dsh.sh start
/home/daniel/QJM/scripts/update_dsh.sh verify
~~~

Der Applier sichert die alte Datei, prüft die neue Komposition mit dsh --profile web --dump-config und
stellt bei Fehlern automatisch den alten Patch wieder her. In Boot 1 ist das Preset trader **registriert**,
aber nicht Default — ein Fehler in der Komposition kann damit keine neuen Sessions blockieren.

Prüfungen in Boot 1 (siehe Matrix): GUI erreichbar, Journal ohne Preset-Fehler, alte Session öffnen,
Settings/Provider, MCP-Tools.

### Phase 5 — Boot 2 (trader als Default)

~~~bash
/home/daniel/QJM/scripts/dsh_apply_profile_patch.sh apply \
  /home/daniel/QJM/dsh_playground/dsh_update_0.1.7/boot2_trader/cordis.patch.candidate.yml --yes
systemctl --user restart dsh-native
~~~

Danach: neue Session anlegen → sie muss als trader starten (Persona: QJM-Systemprompt, MCP-Filter,
Tools run_code/subagent/todo/goal wie gewohnt). Eine bestehende trader-Session öffnen und prüfen, dass sie
fortsetzbar ist.

Rückweg, falls die Komposition Fehler zeigt:
~~~bash
/home/daniel/QJM/scripts/dsh_apply_profile_patch.sh revert      # letzte Sicherung
systemctl --user restart dsh-native
~~~

### Phase 6 — Nachbereitung (nach dem Fenster)

* dsh-secretary startet mit (im Script enthalten); die ersten Workitems beobachten.
* Ein paar alte Sessions unterschiedlichen Alters öffnen (u. a. eine große) — Ladezeit/Fehlerbild.
* Backup-Aufbewahrung: ~/backups/dsh rotiert automatisch (14 Stände); den Stand von heute nicht löschen.
* Eintrag im Trader-Tagebuch/Logbuch: Update, Version, aufgetretene Abweichungen.

### Phase 7 — Rollback (falls nötig)

~~~bash
/home/daniel/QJM/scripts/update_dsh.sh rollback --yes                 # Checkout + Build zurück auf 0.1.5-rc.2
/home/daniel/QJM/scripts/update_dsh.sh rollback --yes --restore-home  # zusätzlich ~/.dsh aus dem Backup
~~~

Ohne --restore-home bleiben die migrierten Settings und V4-Sessions liegen; 0.1.5 kann V4-Sessions nicht lesen.
Mit --restore-home wird der Zustand von vor dem Update vollständig wiederhergestellt (das aktuelle Home wird
vorher nach ~/.dsh.vor-rollback-<stamp> verschoben, nichts wird gelöscht).

---

## 6. Verifikationsmatrix

| # | Prüfung | Kommando / Ort | Erwartung |
|---|---|---|---|
| 1 | Revision | git -C /home/daniel/deepseek-harness describe --tags --exact-match | dsh-v0.1.7-rc.2 |
| 2 | Version der CLI | dsh --version | 0.1.7-rc.2 |
| 3 | Client-Build-Record | jq .environment ~/deepseek-harness/.dsh-build/client-build-environment.json | DSH_CLIENT_VERSION 0.1.7-rc.2, neuer Hash |
| 4 | Dienst | systemctl --user is-active dsh-native | active |
| 5 | HTTP-Listener | curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3090/ | 401 (Token-Schutz) oder 200 — alles außer 000 |
| 6 | Proxies | curl auf http://10.20.0.23:3091/ | Antwort wie 3090 |
| 7 | Journal | journalctl --user -u dsh-native -n 200 --no-pager \| grep -i -E 'error\|fail\|warn' | nur bekannte/unauffällige Zeilen |
| 8 | Preset registriert | journalctl … \| grep -i preset   und  dsh --profile web --dump-config \| grep preset-trader | kein „failed definition", Row vorhanden |
| 9 | Default-Preset | neue Session in der GUI | startet als trader (Persona + Tool-Filter) |
| 10 | Alte Sessions | 2–3 Sessions öffnen (alt/neu, groß/klein) | öffnen ohne Fehler; Verlauf vollständig |
| 11 | Settings | GUI → Einstellungen; ~/.dsh/settings.yaml.imported vorhanden | Provider switchyard/openrouter vorhanden, Theme dunkel |
| 12 | Modell | Testfrage mit deepseek-official/deepseek-flash | Antwort kommt |
| 13 | MCP-Tools | GUI: Tool-Liste / Frage „welche openbrain-Tools hast du?" | 8 Server sichtbar (cco, pta, pca, cda, wiq, drawio, journal, shelly) |
| 14 | Secretary | systemctl --user is-active dsh-secretary; Workitem-Liste | active, Items werden abgearbeitet |
| 15 | Alt-Dateien | ls ~/.dsh/sessions/<ws>/<id>/ | session.v3.jsonl.zstd weiterhin vorhanden |
| 16 | Rollback-Probe (optional, vor dem Fenster) | scripts/update_dsh.sh all --dry-run --force | zeigt alle Schritte ohne Änderung |

---

## 7. Risiken und Gegenmaßnahmen

| Risiko | Eintritt | Wirkung | Gegenmaßnahme |
|---|---|---|---|
| Preset-Komposition enthält ein in 0.1.7 entferntes Feld/Row-Verhalten | mittel | trader startet nicht | Boot 1 mit default standard; Registry behält fehlerhafte Definitionen sichtbar; Journal-Check; Applier-Rollback |
| Build bricht ab (native-system, Desktop-Bundle-Schritt) | niedrig | kein Start möglich | Dienst bleibt gestoppt, Checkout zurück auf 0.1.5-rc.2, Build erneut, starten — ca. 15 Min |
| pnpm install --frozen-lockfile scheitert | niedrig | Abbruch vor dem Build | mit --no-frozen wiederholen |
| settings-Import verliert Provider-Konfiguration | niedrig | Modelle fehlen | settings.yaml + .imported vergleichen; Werte im GUI neu setzen (Backup vorhanden) |
| Alte Sessions nach dem Update nicht lesbar | niedrig | Verlauf fehlt | V3-Dateien bleiben; Backup; ggf. Rollback mit --restore-home |
| Rollback nach V4-Schreibvorgängen unvollständig | mittel | 0.1.5 kann einzelne Sessions nicht lesen | --restore-home nutzen (Home wird vorher gesichert) |
| Ausfallzeit länger als erwartet (Build) | mittel | GUI/Workitems warten | Fenster am Abend/WE, Ankündigung; Secretary-Arbeit läuft nach |
| RC-Qualität (Pre-Release) | mittel | neue Fehler im Alltag | Alternativen: auf 0.1.7-rc.3/Final warten oder dsh-v0.1.6-alpha.2 testen; Rollback ist gebaut |

---

## 8. Offene Entscheidungen

1. **Zeitpunkt/Reifegrad:** 0.1.7-rc.2 ist ein Release Candidate. Empfehlung: durchführen, aber in einem
   ruhigen Fenster; wenn die nächsten Tage kritisch sind, auf rc.3/Final verschieben (Script ist tag-parametrisiert:
   --tag dsh-v0.1.7-rc.3).
2. **Default-Preset im Endzustand:** trader (wie bisher) — im Kandidaten boot2_trader bereits so gesetzt.
3. **--clean:** Empfehlung nein im ersten Versuch, ja bei Verdacht auf Altartefakte.
4. **Perspektive:** Die trader-Komposition künftig als eigenes Plugin-Bundle (Plugin Manager) statt als
   Profil-Patch führen — erst nach dem Update bewerten, nicht jetzt.
5. **Home-Patch (MCP):** unverändert lassen. Ein späterer Umbau (z. B. shelly auf HTTP) ist ein eigenes Thema.

---

## 9. Artefakte und Befehlsreferenz

**Neu erstellt (nichts davon ist aktiv):**

| Datei | Zweck |
|---|---|
| DSH_UPDATE_PLAN_v0.1.7-rc.2.md | dieser Plan |
| scripts/update_dsh.sh | Update/Rollback/Verify: status, preflight, backup, stop, update, start, verify, rollback, all |
| scripts/dsh_apply_profile_patch.sh | Profil-Patch einsetzen/zurücksetzen, mit Auto-Rollback bei kaputter Komposition (show, check, apply, revert) |
| scripts/dsh_preset_migrate.mjs | erzeugt aus dem Verzeichnis-Preset die 0.1.7-Row (inkl. ptc-Rename) |
| scripts/dsh_preset_compat_check.mjs | prüft alle Paket-Referenzen einer Komposition gegen eine Zielversion |
| dsh_playground/dsh_update_0.1.7/boot1_standard/ | Kandidat für Boot 1 (default: standard) + Komposition + Notes |
| dsh_playground/dsh_update_0.1.7/boot2_trader/ | Kandidat für Boot 2 (default: trader) + Komposition + Notes |
| scripts/dsh_snapshot.sh | verifizierter Snapshot von ~/.dsh (Chats + Konfigs) mit Manifest, SHA-256, Restore-Anleitung |
| scripts/setup_dsh_parallel_017.sh | baut/betreibt die Parallelinstallation (Worktree, Home, Dienst auf 3092); Aktionen preflight/worktree/build/seed/service/verify/reset |
| scripts/switch_dsh.sh | Umschalten 015 ↔ 017 auf Port 3090 inkl. Chat-Sync und Sicherheits-Snapshot |
| scripts/dsh-start/ | eingefrorenes Original-Startskript + Unit der 0.1.5 (sha256-geprüft) und das 0.1.7-Startskript |

**Die wichtigsten Befehle in Reihenfolge:**

~~~bash
# 0) aus einer SSH-Shell, NICHT aus der DSH-Session
cd /home/daniel/QJM
scripts/update_dsh.sh status
scripts/update_dsh.sh all --dry-run --force     # zeigt den kompletten Ablauf, ändert nichts
scripts/update_dsh.sh preflight                 # echte Vorabprüfung

# 1) echtes Update (Backup + Stop + Checkout + Build + Start + Verify)
scripts/update_dsh.sh all --yes

# 2) Preset-Migration scharf schalten (zwei Stufen)
scripts/dsh_apply_profile_patch.sh apply dsh_playground/dsh_update_0.1.7/boot1_standard/cordis.patch.candidate.yml --yes
# Smoke-Test, Journal, Sessions, MCP ...
scripts/dsh_apply_profile_patch.sh apply dsh_playground/dsh_update_0.1.7/boot2_trader/cordis.patch.candidate.yml --yes
systemctl --user restart dsh-native

# 3) Notausgang
scripts/dsh_apply_profile_patch.sh revert
scripts/update_dsh.sh rollback --yes [--restore-home]
~~~

---

## 10. Variante B — Parallelinstallation (gewählter Weg)

### 10.1 Prinzip

Zwei vollständige Installationen nebeneinander, jede mit eigenem Build, eigenem Home und eigenem Port.
Es wird **nichts** an der bestehenden 0.1.5 verändert — kein Checkout-Wechsel, keine Migration, kein
Umbenennen von settings.yaml.

| | Produktion (heute) | Neu 0.1.7-rc.2 |
|---|---|---|
| Checkout | /home/daniel/deepseek-harness @ dsh-v0.1.5-rc.2 | /home/daniel/deepseek-harness-017 (git worktree @ dsh-v0.1.7-rc.2) |
| DSH-Home | /home/daniel/.dsh | /home/daniel/.dsh-017 |
| Dienst | dsh-native.service (Port 3090, Proxies 3091/3444) | dsh-native-017.service (Port 3092, Proxies 3093/3445) |
| Startskript | ~/start-dsh-native.sh | ~/start-dsh-native-017.sh (Parameter per Umgebung) |
| Chats | 243 Sessions (V3) — bleiben unangetastet | Kopie der Sessions (darf migrieren und schreiben) |

Geteilt: die MCP-Server und Modell-Endpunkte (laufen außerhalb), der Secretary (bleibt auf 0.1.5),
das gemeinsame .git und der pnpm-Store. Aufwand: ~2 GB Disk, etwas RAM pro Instanz.

### 10.2 Rollback-Garantie — was genau gilt

* **~/.dsh wird von keinem der Skripte verändert.** Alle heutigen Chats, Settings, Credentials,
  Patches, Storages und Attachments bleiben dort liegen, wie sie sind.
* **Rückweg = Startskript tauschen** (switch_dsh.sh to-015): die Produktion läuft wieder auf 0.1.5
  mit ~/.dsh auf Port 3090. Dauer: Sekunden, kein Rebuild, keine Migration.
* **Zusätzliches Netz:** scripts/dsh_snapshot.sh schreibt verifizierte Snapshots (Manifest, SHA-256,
  Testentpackung) nach QJM/backups/dsh_state/ — unabhängig von der Plattform.
* **Grenze, ehrlich benannt:** Chats, die du **nach** dem Umschalten auf 0.1.7 schreibst, liegen im
  V4-Format in ~/.dsh-017 und sind mit 0.1.5 nicht lesbar. Sie sind nicht verloren (das Home bleibt),
  werden aber erst wieder sichtbar, wenn du auf 0.1.7 gehst. Der Rückweg stellt den Stand zum
  Umschaltzeitpunkt her.

### 10.3 Ablauf

~~~bash
cd /home/daniel/QJM
scripts/dsh_snapshot.sh                                       # Ist-Zustand sichern (verifiziert)

scripts/setup_dsh_parallel_017.sh all --yes --with-sessions   # Worktree + Build + Home + Dienst 3092
scripts/setup_dsh_parallel_017.sh verify                      # Version, Linkfarm, Komposition, HTTP
# -> Testen auf http://127.0.0.1:3092/ : Chats öffnen, MCP-Tools, Preset trader, Modelle

scripts/switch_dsh.sh status                                  # wer ist live?
scripts/switch_dsh.sh to-017 --yes                            # 0.1.7 wird Produktion auf 3090 (Chats werden gesynct)
scripts/switch_dsh.sh to-015 --yes                            # jederzeit zurück auf 0.1.5
scripts/setup_dsh_parallel_017.sh reset --yes                 # Testkanal wegwerfen (Produktion bleibt)
~~~

### 10.4 Kritischer Prüfpunkt nach dem ersten Start

Die Profil-Linkfarm (~/.dsh-017/profiles/node_modules) muss auf den **neuen** Checkout zeigen, sonst
läuft 0.1.5-Plugin-Code unter 0.1.7:

~~~bash
readlink ~/.dsh-017/profiles/node_modules/@deepseek-ai/dsh-persona
# erwartet: /home/daniel/deepseek-harness-017/apps/cli/node_modules/@deepseek-ai/dsh-persona
~~~

Zeigt der Link auf den alten Checkout oder fehlt er: Instanz stoppen, das Verzeichnis
~/.dsh-017/profiles/node_modules löschen und neu starten (der Boot legt die Farm neu an).
setup_dsh_parallel_017.sh verify prüft das automatisch.

### 10.5 Nebenbefund

Beim Fetch ist ein Tag **dsh-v0.1.5-rc.3** aufgetaucht — ein Wartungs-Release auf deiner aktuellen
Minor-Linie. Falls du die Mechanik erst ohne Feature-Sprung testen willst: derselbe Worktree-Aufbau
funktioniert mit --tag dsh-v0.1.5-rc.3, und die Migrationsthemen (Presets, settings.yaml-Import,
Session-V4) entfallen dort weitgehend.

### 10.6 Nachträge aus der Inbetriebnahme (27.09.2026)

Beim ersten echten Start des Testkanals traten zwei Dinge auf, die im Vorfeld nicht sichtbar waren:

1. **Die Interception-Ebene fehlte im frischen Home.** Plugin-Namen aus einem Profil-Patch werden
   relativ zum Profilverzeichnis aufgelöst (Node-Suche über `profiles/node_modules`). Im 0.1.5-Home
   existierte diese Ebene (240 Links), im neuen 0.1.7-Home nicht: 0 von 22 Paketen auflösbar.
   Fix: **scripts/dsh_fix_profile_links.sh** projiziert die Ebene auf den neuen Checkout und ergänzt
   die in 0.1.7 neuen Pakete (`dsh-agent-preset`, `dsh-ptc-runtime-node`, `dsh-workflow-ptc`).
   Kontrolle: `--check` muss „22 / 22 auflösbar" melden; die Links überleben Dienstneustarts.

2. **`ptc-runtime` ist eine Host-Row und gehört nicht in ein Agent-Preset.** In 0.1.5 hieß die
   Runtime-Row `workflow-worker-thread` und lag im Preset; in 0.1.7 liegt `ptc-runtime`
   (`@deepseek-ai/dsh-ptc-runtime-node`) im Basis-Bundle (Host-Plan). Eine zweite Registrierung im
   Agent-Preset lässt den Mount scheitern („Failed to load"). Im Preset bleibt nur `workflow-ptc`,
   genau wie im mitgelieferten `standard`-Preset. Der Generator ist entsprechend korrigiert.
   Für `run_code` im Agenten sind zwei zusätzliche Rows nötig: `tool-presentation`
   (`@deepseek-ai/dsh-agent-tool-presentation`) und `present` (`@deepseek-ai/dsh-tool-present`).
   Der Deployment-Default ist `native` (= **kein** `run_code`, `packages/core/tools/src/index.ts:811`);
   im Testkanal ist `mode: both` gesetzt (native Schemas + run_code, wie in 0.1.5).

Weitere Beobachtungen:

* Das Badge „Failed to load" speist sich aus `record.broken` = `diagnostic()` (Audit bei jedem
  Listen-Aufruf), **nicht** aus dem Aktivierungs-Log — deshalb steht in dem Fall nichts im Journal.
  Der Fehlertext steckt im Tooltip der Preset-Karte (`brokenTip`).
* **Ctrl+Shift+R ist in 0.1.7 ein UI-Shortcut** (Session umbenennen), kein Browser-Reload.
  Zum Neuladen F5, den Reload-Button oder ein Inkognito-Fenster verwenden.
* Reale Dauer des Aufbaus (fetch + worktree + install + build + seed + Dienst): **~2,5 Minuten** —
  die Schätzung 10–30 Minuten war deutlich zu konservativ (32 Kerne, warmer pnpm-Store).

---

## 11. Quellen

* Release Notes: https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.1.7-rc.2
  (und die Releases 0.1.6-alpha.1 … 0.1.7-rc.1)
* Session-Format: https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.7-rc.2/docs/session-format-status.md
  und docs/persistence-changes/2026-09-16-session-format-v4.md
* Preset-Registry: https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.7-rc.2/packages/preset/agent-preset-registry/README.md
* MCP-Client: https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.7-rc.2/packages/mcp/mcp-client/README.md
* Settings-Import: https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.7-rc.2/packages/settings/settings/README.md
* Build/Installation: https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.7-rc.2/README.md
* Patch-Semantik: https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.7-rc.2/vendor/include/src/index.ts
