# services/secretary — die Sekretärin

Eine Instanz, **kein LLM**, ein Tick alle 2 Sekunden. Sie arbeitet den
Workitem-Graphen aus `migrations/019_workitem_engine.sql` ab und startet für
jedes Workitem eine DSH-Session.

## Aufbau

| Datei | Rolle |
|---|---|
| `secretary/config.py` | Konfiguration aus Umgebungsvariablen |
| `secretary/store.py` | Datenschicht. Der **einzige** Schreiber für Statusübergänge außerhalb der Worker |
| `secretary/framing.py` | Die eine Nachricht, mit der ein Workitem präsentiert wird — inkl. Rollen-Katalog (Fähigkeiten + Parallelität) als Entscheidungsgrundlage des Leads |
| `secretary/harness.py` | Ein langlebiger DSH-SDK-Prozess **pro Rolle** |
| `secretary/loop.py` | Der Tick |
| `secretary/cli.py` | `submit`, `run`, `status` |

## Der Tick macht vier Dinge

1. fertige Läufe einsammeln — und **Protokollverletzungen** behandeln: endet ein
   Lauf, ohne dass der Agent `workitem_finish` gerufen hat, wird das Item
   erneut versucht (bis `max_attempts`) und sonst `failed`.
2. abgelaufene Leases erholen (`workitem_recover_leases()`)
3. unerreichbare Tasks überspringen, Reviews ausgenommen (`workitem_skip_cascade()`)
4. bereite Items bis zur Rollen-Kapazität dispatchen

**Es gibt bewusst keinen Unblock-Code.** Bereitschaft ist die View
`ready_workitems`, und weil die Sekretärin sowieso pollt, ist Ableiten
einfacher und selbstheilend.

## Warum ein Prozess pro Rolle

Das DSH-SDK kann **keine Presets wählen** (`packages/sdk/server/src/server.ts`:
"No preset composition"), und `agent-presets` ist nur im Web-Bundle gemountet.
Rollentrennung läuft deshalb über `--patch`-Overlays (`roles/*.cordis.yml`).

Der langlebige Prozess ist gleichzeitig die "LLM-Ressource" aus dem
Originalprompt: er trägt mehrere Sessions parallel (in Phase 0 verifiziert:
3 Läufe in 1,1 s statt 3,1 s).

**Sessions mit Gedächtnis:** nur `initial`- und `review`-Items behalten eine
Session über `agent_sessions`. Damit laufen INITIAL, REVIEW Runde 1 und
REVIEW Runde 2 in **derselben** Session — der Reviewer lernt aus seinem
eigenen Fehlschlag. Worker laufen zustandslos mit `session_id = workitem_id`.

## Betrieb

```sh
source services/secretary/env.sh          # liest das DB-Passwort aus dem Container
./.venv/bin/python -m secretary.cli submit "fasse alle X-Posts von heute zusammen"
./.venv/bin/python -m secretary.cli run   # die Schleife
./.venv/bin/python -m secretary.cli status <change_item_id>
```

Das DB-Passwort wird **nie** in eine Datei geschrieben: `env.sh` liest es zur
Laufzeit aus dem `openbrain-db`-Container und exportiert es nur.

## Konfiguration

| Variable | Default | Bedeutung |
|---|---|---|
| `SECRETARY_TICK_SECONDS` | `2` | Takt der Schleife |
| `SECRETARY_LEASE_SECONDS` | `900` | Lease-Dauer eines laufenden Items |
| `SECRETARY_MAX_ROUNDS` | `2` | Ab dieser Runde schließt ein unbefriedigendes Review mit `failed` ab |
| `SECRETARY_DSH_BIN` | `/home/daniel/.local/bin/dsh` | **Muss** gesetzt sein, sonst startet das SDK seine eigene Wheel-Runtime |
| `SECRETARY_DSH_HOME` | `/home/daniel/.dsh` | Dasselbe Zuhause wie die Web-GUI — deshalb sind die Läufe dort sichtbar |
| `SECRETARY_WORKSPACE` | `/home/daniel/QJM` | cwd der Agenten |
| `SECRETARY_ROLES_DIR` | `/home/daniel/QJM/roles` | Wo die Rollen-Patches liegen |
| `SECRETARY_PROVIDER` / `SECRETARY_MODEL` | `deepseek-official` / `deepseek-flash` | Modell, wenn die Rolle keins vorgibt |

## Noch nicht gebaut

- **Kein Daemon-Betrieb.** Für den Dauerbetrieb fehlt eine systemd-User-Unit
  (Muster: `~/.config/systemd/user/dsh-native.service`).
- **Keine Kostenbremse.** Ein Lauf kann beliebig viele Items planen; es gibt
  kein Budget pro Lauf.
- **Kein Dashboard.** `status` ist die Kommandozeilen-Vorstufe davon.
- **Ein Schreiber.** Zwei Sekretärinnen parallel sind nicht vorgesehen (die
  Lease-Claims sind atomar, aber die Abschlusslogik ist es nicht).
