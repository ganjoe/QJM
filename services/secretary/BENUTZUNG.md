# Die Workitem-Engine benutzen

**Der Normalfall ist der Chat.** Du sagst deinem DSH-Session, was du willst — er reicht es über
die MCP-Tools `run_submit`, `run_status` und `run_result` bei der Engine ein. Shell brauchst du
nur noch zum Steuern des Dämons und zum Auswerten.

```
du im Chat ──► run_submit ──► Sekretärin (Dämon) ──► Lead plant ──► CCO arbeitet ──► Review
                                tickt alle 2 s        mit Budget      parallel       entscheidet
```

---

## 1. Im Chat

Sag deinem Session einfach:

> „Delegiere das an die Workitem-Engine: fasse zusammen, was heute in den überwachten X-Posts
> zu Halbleitern gesagt wurde."

Der Session ruft dann `run_submit` auf und gibt dir zwei IDs zurück:

```
Eingereicht.
change_item_id: 6a993279-3cd9-4b1f-b1b5-0c60a4e97e65
workitem_id:    50546f17-5245-4009-bd90-a1edb8a67fbe
budget:         {"rounds":14}
```

Danach fragst du nach dem Fortschritt („wie steht der Lauf?") und nach dem Ergebnis
(„was ist dabei rausgekommen?"). Der Session nutzt dafür `run_status` und `run_result`.

**⚠️ Wichtig:** Die neuen Tools erscheinen nur in Sessions, die **nach** dem letzten Neustart des
MCP-Servers gestartet wurden. Wenn du sie nicht siehst: **einen neuen Chat öffnen.** (Beim ersten
Mal habe ich genau das vergessen — der Container lief noch mit dem alten Tool-Satz.)

### Was die drei Tools tun

| Tool | Zweck |
|---|---|
| `run_submit` | Auftrag einreichen. Optional `rounds`/`tokens` als Budget des Planungs-Items und `max_rounds` als Rundendeckel des Laufs. |
| `run_status` | Jedes Workitem mit Status, Runde, Budget und Ist-Verbrauch. `include_results: true` zeigt Zwischenergebnisse. |
| `run_result` | Die Antwort: das Ergebnis des letzten Reviews plus die Task-Ergebnisse. Meldet ehrlich, wenn der Lauf noch läuft. |

### Wie viele Runden darf der Lead planen?

`max_rounds` ist eine Eigenschaft des **Laufs** (`change_items.max_rounds`), nicht der
Sekretärin — **Default 2**, einstellbar 1–10. Du setzt ihn im Dashboard beim Anlegen
(„Planungsrunden"), per CLI (`--max-rounds`) oder per MCP (`run_submit`/`schedule_create`).
Bei einer stehenden Aufgabe erbt jedes Vorkommen den Wert.

Der Wert steht in der ersten Nachricht jedes Planungs- und Review-Items. Entscheidet das
Review am **letzten** erlaubten Durchgang, dass der Auftrag nicht erfüllt ist, endet der Lauf
als `failed` statt eine weitere Runde zu planen.

Zwei Runden sind knapp, wenn die erste Runde an einem gerissenen Budget scheitert: die
zweite Runde ist dann die Korrektur, und für die eigentliche Auswertung bleibt keine übrig
(genau so ist der Lauf `47c7480b` gelaufen: 3,0 Mio Tokens für zwei Scan-Versuche). Für
Aufträge, die Recherche **und** Auswertung brauchen, sind **3** realistischer.

> Nicht verwechseln: `rounds` (Budget des Planungs-Items in *Modell*-Runden) und
> `max_rounds` (Anzahl der *Planungsrunden* des Laufs) sind verschiedene Dinge.

## 2. Was du erwarten kannst

Ein **einrundiger** Lauf (Planen → 1–2 Tasks → Review) dauert **2–5 Minuten**. Ein Zwei-Runden-Lauf
etwa das Doppelte — die Phasen sind strukturell sequenziell, das ist die Kettenlogik, keine
Langsamkeit.

Der Lead plant selbstständig Parallelität: er kann mehrere Tasks gleichzeitig an den CCO geben
(`max_concurrency = 2`).

## 3. Mitlesen

Jedes Workitem ist eine normale DSH-Session und erscheint als **Chat in deiner Web-GUI** (gleiches
`DSH_HOME`). Dort kannst du dem Lead beim Planen und dem CCO bei der Recherche zusehen — das ist
die beste Art, das System zu verstehen.

Dämon-Log: `journalctl --user -u dsh-secretary -f`

## 4. Ein typischer Status

```
RUNNING  Nenne die drei Ticker, die heute am haeufigsten vorkommen.
(1 Item(s) offen)

step_key           typ      rolle          status     runde    runden          tokens
initial            initial  lead_engineer  done           1        5/14           66286/-
erhebe-tagesbasis  task     cco            failed         1       10/12     467778/360000
ticker-ranking     task     cco            skipped        1        0/10          0/300000
review-ergebnis    review   lead_engineer  running        1         0/8          0/240000
```

Das liest sich so: der Lead hat geplant (5 Runden seines 14er-Budgets). Der CCO-Task hat sein
**Token-Budget gerissen** (467k von 360k), wurde abgebrochen und als `failed` gemeldet — mit
`reason: "budget_exceeded"` und einem `needs`, das sagt, was ein Retry bräuchte. Die abhängigen
Tasks wurden dadurch **übersprungen**, aber das **Review läuft trotzdem** (es ist von der Kaskade
ausgenommen) und entscheidet, ob es mit mehr Budget geht.

## 5. Kosten auswerten

```sh
cd ~/QJM/services/secretary && source env.sh

./.venv/bin/python -m secretary.cli status <change_item_id>   # Ist/Soll pro Item
./.venv/bin/python tools/session_stats.py                     # Dauer, Runden, Tool-Zeit
```

Gesamtverbrauch:

```sh
docker exec -i openbrain-db psql -U postgres -d postgres -c "
select sum((usage->>'tokens')::bigint) as tokens,
       sum((usage->>'seconds')::int)   as sekunden,
       count(*) filter (where status = 'failed') as gescheitert,
       count(*) as items
  from workitems;"
```

## 6. Den Dämon steuern

```sh
systemctl --user status  dsh-secretary
systemctl --user restart dsh-secretary     # nach Änderungen an roles/ oder secretary/
systemctl --user stop    dsh-secretary     # laufende Aufträge dürfen zu Ende laufen
```

**Nach jeder Änderung neu starten** — Rollen-Patches und Policy-Code werden beim Start der
Rollen-Prozesse geladen:

```sh
cd ~/QJM/llm-gateway && docker compose restart mcp-wiq   # nach mcp-wiq-Änderungen
```

## 7. Rollen ändern

Alle `roles/*.cordis.yml` sind `--patch`-Overlays, versioniert im Repo.
Eine Rolle entsteht durch Abschalten ganzer Zeilen:

```yaml
- id: tool-bash          # Datenquelle: Shell öffnete DB, Docker, Repo
  disabled: true
- id: mcp-openbrain-pca  # fremder MCP-Server
  disabled: true
```

**Zwei Prinzipien, die teuer gelernt wurden:**

1. **Rollentrennung kappt Datenquellen, nicht Datenzufuhr.** `tool-fs` (`read`) und
   `tool-fs-search` (`grep`) müssen anbleiben — der Spill-Mechanismus lagert große Tool-Ausgaben
   in Dateien aus und verweist das Modell ausdrücklich auf genau diese beiden Werkzeuge.
2. **Jede Fähigkeit, die du einer Rolle nimmst, muss als Werkzeug existieren.** Sonst wird sie
   stillschweigend unmöglich — der CCO konnte „wie viele Influencer gibt es insgesamt" nicht mehr
   beantworten, weil `manage_influencers` nur die aktiven zählt und die Shell weg war.

Eine **neue Rolle** braucht vier Dinge:
1. `roles/<name>.cordis.yml` — Vorbild: `roles/cco.cordis.yml`. Eigener MCP-Server an,
   **alle** fremden aus, Orchestrierung aus (`tool-subagent*`, `tool-workflow`, `tool-ralph`,
   `tool-goal`, `tool-web`), Shell aus, `tool-fs`/`tool-fs-search` an, `budget-policy` einhängen.
2. `insert into roles (name, patch, max_concurrency, description) values (...);` —
   siehe `migrations/021_roles_all_agents.sql`.
3. **Die `description` ist die Entscheidungsgrundlage des Leads**, nicht Doku für Menschen:
   `framing.py` schickt sie in der ersten Nachricht jedes Planungs- und Review-Items mit
   (`available_roles`). Sie muss in einem Satz sagen, welche Datenquelle die Rolle besitzt
   **und was sie nicht kann** — sonst verteilt der Lead eine Aufgabe an eine Rolle, die die
   Quelle gar nicht hat.
4. Sekretärin neu starten (`systemctl --user restart dsh-secretary`) — sie hält Code und
   Rollen-Zeilen im Speicher.

Der aktuelle Rollensatz steht in `migrations/021_roles_all_agents.sql`. Die Trennung ist
dort pro Rolle erzwungen:

| Rolle | sieht | sieht nicht |
|---|---|---|
| `lead_engineer` | nur den Workitem-Graphen | alle Datenquellen |
| `cco` | X, YouTube, gespeicherte Posts, Web | Kurse, Charts, Stammdaten, Broker |
| `pta` | Broker (IBKR): Kurs, Positionen, Orders | Charts, Fundamentaldaten, Social |
| `pca` | Charts, Indikatoren, Scanner, Marktbreite | Fundamentaldaten, Filings, Broker |
| `cda` | Stammdaten, SEC-Filings, Downloads | Live-Kurse, Chart-Analyse, Social |
| `drawio` | nichts — zeichnet nur payload/context_refs | alle Datenquellen |

**Achtung `pta`:** `place_trade` ist schreibend. Das Gateway läuft derzeit im Paper-Modus;
vor einem Umschalten auf live diese Rolle prüfen. Die Persona verlangt zusätzlich, dass nur
ein ausdrücklicher Auftrag im `payload` ordert.

### Reasoning-Level (wie viel das Modell nachdenken darf)

Drei Ebenen, jede schlägt die vorige:

| Ebene | Feld | Wer setzt es |
|---|---|---|
| Global | `~/.dsh/settings.yaml` → `agent-default-model.reasoningEffort` | du (gilt für alle DSH-Sitzungen) |
| **Rolle** | `roles.reasoning_effort` | du |
| **Aufgabe** | `workitems.reasoning_effort` | der Lead Engineer beim Anlegen (`reasoning_effort` in `workitem_create`) |

Erlaubt sind `off`, `low`, `high`, `max`. Ohne Angabe auf beiden Ebenen gilt der
globale Default — der stand auf `max`, also lief **jeder** Lauf auf der teuersten Stufe,
auch ein Item, das nur eine Zahl nachschlägt.

Rollen ansehen und setzen:

```sh
cd ~/QJM/services/secretary && source env.sh
./.venv/bin/python -m secretary.cli roles                 # Übersicht
./.venv/bin/python -m secretary.cli roles set cco low     # Rolle cco auf low
./.venv/bin/python -m secretary.cli roles set cco default # zurück auf den DSH-Default
```

Der Lead Engineer sieht in seiner ersten Nachricht jede Rolle **mit** ihrem Level
(`cco [max 2 parallel, Reasoning high] — …`) und kann pro Aufgabe abweichen: `low` für
mechanische Arbeit, `high` für Bewertungen.

**Betriebsgrenze:** DSH setzt den Level beim **Start eines Rollen-Prozesses**, nicht pro
Session. Die Sekretärin hält deshalb einen Prozess **pro (Rolle, Level)**. Eine Änderung
wirkt für neue Kombinationen; ein bereits laufender Prozess behält seinen Level, bis er
endet. Die Parallelität (`max_concurrency`) gilt weiterhin pro Rolle, nicht pro Prozess.

Im Log siehst du das Ergebnis jeder Zuteilung:

```
dispatch erhebe-tagesbasis [task/cco] level=low runde=1 versuch=1/2 session=…-v1
```

## 8. Shell-Fallback

Wenn du die Engine ohne Chat benutzen willst (Skripte, Tests):

```sh
cd ~/QJM/services/secretary && source env.sh
./.venv/bin/python -m secretary.cli submit "…"     # einreichen
./.venv/bin/python -m secretary.cli status <id>    # ansehen
```

Und die MCP-Tools direkt:

```sh
cd ~/QJM/mcp/agent-wiq
bash tests/call.sh tools/list
bash tests/call.sh tools/call run_status '{"change_item_id":"…"}'
```

## 9. Was noch nicht geht

| | |
|---|---|
| **Kein Projektbudget** | Jedes Item hat einen eigenen Deckel; die Summe überwacht niemand |
| **Dashboard nur lesend** | `wiq_dashboard/` zeigt alles, schreibt aber nur neue Aufträge und stehende Aufgaben — Statusübergänge bleiben bei der Sekretärin |
| **Abgelöste Items** | Eine in Runde 1 gescheiterte Task bleibt als `failed` stehen, auch wenn Runde 2 sie löst |
| **Jede Session darf einreichen** | Der MCP-Server kennt den Aufrufer nicht (geteilter Key im internen Netz) |

---

## 10. Stehende Aufgaben (verzögert / wiederkehrend)

Ein Auftrag, der nicht jetzt läuft, sondern zu einem Termin — einmalig oder
wiederkehrend. Aus dem Chat heraus:

> „Richte eine stehende Aufgabe ein: durchsuche jeden Sonntag um 18:00 die
> gespeicherten X-Posts nach Hinweisen auf X und bewerte es semantisch."

Der Session ruft dann `schedule_create` auf und gibt dir die Kennung zurück:

```
Stehende Aufgabe angelegt.
change_item_id: 1b258dbd-fc79-45c1-9a60-b797a1effe2b
regel:          {"kind":"repeat","every":"weekly","time":"18:00","time_zone":"Europe/Berlin","weekday":6}
rolle:          cco
```

| Tool | Zweck |
|---|---|
| `schedule_create` | anlegen. Genau **eine** Zeitangabe: `at` (absolut, mit Zone), `after_seconds`, `every=daily/weekly` + `time`, oder `every_seconds` (Minimum 300) |
| `schedule_list` | Zustand, Regel, nächster Termin, Anzahl der Vorkommen |
| `schedule_run` | sofort einmal ausführen, ohne auf den Termin zu warten |
| `schedule_cancel` | abschalten — kein weiterer Termin |

**Was beim Termin passiert:** Die Sekretärin legt einen **eigenen Lauf** an — ein
neues `change_item` mit `source_change_item_id` auf die stehende Aufgabe, darin
**ein** Workitem, das den Auftrag mit den MCP-Werkzeugen der angegebenen Rolle
ausführt. Kein Lead, kein Review: eine stehende Aufgabe ist eine Aufgabe, kein
Projekt. Die Antwort steht im `result` dieses Items — im Chat mit `run_result`,
im Dashboard per Klick.

**Wenn der Server zur Terminzeit aus war:** Das Vorkommen feuert **verspätet
genau einmal** und der nächste Termin bleibt auf seinem regulären Platz. Es gibt
bewusst keinen Nachhol-Sturm.

**Wenn die Regel unbrauchbar ist** (z. B. eine Uhrzeit, die es wegen
Zeitumstellung nicht gibt), steht die Aufgabe nach dem nächsten Tick als
`failed` mit `schedule.error` in der Liste — sie läuft dann nicht.

