# wiq_dashboard — das Fenster zur Workitem-Engine

Ein Qt6-Fenster (PySide6) auf dem Arbeitsrechner, das Läufe, stehende Aufgaben
und ihre Ergebnisse zeigt. Kein Chat: die Suche stellt eine Frage an die
**Datenbank**, nicht an ein Modell. Die Antwort liest man im Lauf selbst —
Change Item anklicken, Item anklicken, `result` lesen.

Aufbau und Betrieb folgen dem **Chart-Viewer-Muster**: ein Dienst auf dem
QJM-Host, der Client dort, wo der Mensch sitzt.

```
  Windows / macOS                        QJM-Host (10.20.0.23)
  ┌──────────────────────┐               ┌───────────────────────────────┐
  │ launch_windows.bat   │  HTTP 8799    │ qjm-wiq-dashboard  (Container)│
  │ launch_macos.command │ ────────────► │  /api/runs /api/detail        │
  │  ├ Code-Sync         │ ◄──────────── │  /api/submit /api/schedule    │
  │  ├ venv (PySide6)    │               │  /api/sync  (Client-Code)     │
  │  └ Qt-Fenster        │               └───────────┬───────────────────┘
  └──────────────────────┘                           │ PostgREST
                                                     ▼
                                          openbrain-db (Workitem-Graph)
```

**Warum so:** Der Client hat keine Datenbank-Zugangsdaten, und der Service-Key
bleibt auf dem Server. Der Client lädt seinen Code beim Start vom Server —
es gibt keine Installation auf den Arbeitsrechnern außer Python.

## Starten

| Wo | Befehl |
|---|---|
| **Windows** | `launch\launch_windows.bat` (Doppelklick) |
| **macOS** | `launch/launch_macos.command` |
| QJM-Host (Diagnose) | `./dashboard.sh` |

Der erste Start richtet einmalig eine eigene venv mit PySide6 ein
(`%LOCALAPPDATA%\WIQDashboard` bzw. `~/Library/Application Support/WIQDashboard`).
Danach wird bei jedem Start nur die Code-Version verglichen und bei Änderung
nachgeladen.

Schalter: `SERVER_HOST` (anderer Server), `FORCE_SYNC=1` (Code neu laden),
`RESET_VENV=1` (venv neu aufbauen).

### Windows: aus dem Netzwerkordner starten

Ein `.bat` **direkt per UNC-Pfad** (`\\10.20.0.23\daniel\...`) zu starten, scheitert
in der Regel: cmd.exe kann einen UNC-Pfad nicht als aktuelles Verzeichnis
benutzen und Windows meldet einen Zugriffsfehler. Drei Wege, und alle drei sind
in Ordnung:

1. **Über das gemappte Laufwerk** — `Z:\QJM\wiq_dashboard\launch\launch_windows.bat`
   (genau so läuft dein Chart-Viewer: dessen Verknüpfung hat als „Ausführen in"
   `Z:\QJM\chart_viewer`).
2. **Verknüpfung anlegen** mit Ziel `\\10.20.0.23\daniel\QJM\wiq_dashboard\launch\launch_windows.bat`
   und „Ausführen in" = `Z:\QJM\wiq_dashboard\launch`.
3. **Einfach doppelklicken** — das Skript erkennt den UNC-Start selbst, kopiert
   sich nach `%TEMP%` und läuft von dort weiter.

Das Skript muss übrigens **nicht** im Share liegen: es lädt den Client-Code
ohnehin über HTTP. Eine Kopie auf dem Desktop funktioniert genauso — nur der
Server muss erreichbar sein.

**macOS:** liegt die `.command` auf dem Share, verliert sie beim Kopieren oft
das Ausführbar-Bit. Dann `bash launch_macos.command` aufrufen.

## Aufbau

| Datei | Rolle |
|---|---|
| `server/app.py` | FastAPI: die HTTP-Schnittstelle |
| `server/postgrest.py` | Lesen und Schreiben über PostgREST — **kein** DB-Passwort im Container |
| `server/rules.py` | bindet `services/secretary/secretary/schedules.py` ein (gemountet) und prüft damit die Regeln |
| `client/wiq_dashboard/api.py` | HTTP-Client des Fensters (nur Standardbibliothek) |
| `client/wiq_dashboard/model.py` | Baum aus zwei Ebenen: Wurzeln und ihre Vorkommen |
| `client/wiq_dashboard/window.py` | Fenster, Liste, Detail |
| `client/wiq_dashboard/worker.py` | eigener Thread; das Fenster blockiert nie |
| `client/wiq_dashboard/dialogs.py` | „Neuer Auftrag": sofort oder stehend |
| `launch/` | die beiden Bootstrap-Skripte |

## Was es schreibt — und was nicht

**Schreiben:** einen neuen Auftrag (`change_item` + `initial`-Workitem) und die
Metadaten einer stehenden Aufgabe (anlegen, abschalten, sofort ausführen).

**Nicht schreiben:** Workitem-Status. Das bleibt bei der Sekretärin — die
Invariante „genau ein Schreiber für Statusübergänge außerhalb der Worker" ist
der Grund, warum das System zuverlässig ist.

## Die zwei Ebenen

Unter jeder stehenden Aufgabe stehen ihre Vorkommen (`Nr. 3 · 22.09.2026 00:19`).
Erst dieser Vergleich zeigt, was sich geändert hat — bei einem wöchentlichen
Check ist die Reihe die Information, nicht der einzelne Lauf.

## Spalten

| Spalte | Bedeutung |
|---|---|
| Zustand | Farbe = Zustand. `wartet` ist eine stehende Aufgabe, `laeuft` ein aktiver Lauf |
| Titel | mit Tooltip (Auftrag, id, Regel) |
| Art / Regel | `ad-hoc`, `Vorkommen` oder die Kurzform der Regel — **der Satz kommt vom Server** |
| Items | Workitems ohne Vorlagen |
| Tokens | Summe über das Item **und seine Vorkommen** (Roll-up in der View `wiq_runs`) |
| Nächster / Fertig | bei stehenden Aufgaben der nächste Termin, sonst der Abschluss |

## Server betreiben

```sh
cd ~/QJM/llm-gateway
docker compose up -d --no-deps wiq-dashboard
docker logs -f qjm-wiq-dashboard
```

Der Dienst liest die Sicht `wiq_runs` (`migrations/028_wiq_dashboard_runs.sql`)
über PostgREST. Der Service-Key kommt aus `llm-gateway/.env` — es gibt keine
zweite Kopie und kein Datenbank-Passwort in einer Datei.

**Nach einer Änderung am Client** ist nichts zu tun: der Server rechnet den
Hash des Client-Verzeichnisses bei jedem `/api/sync_version` neu, die
Arbeitsrechner laden beim nächsten Start automatisch nach. Nach einer Änderung
**am Server**:

```sh
cd ~/QJM/llm-gateway && docker compose up -d --build --no-deps wiq-dashboard
```

## Tests

```sh
cd ~/QJM/wiq_dashboard
./.venv/bin/python -m unittest discover -s server/tests -t .
```

Die Tests laufen gegen den laufenden Server und legen eine stehende Aufgabe mit
Termin im Jahr 2030 an, die sie wieder abschalten — sie kann nie feuern.
