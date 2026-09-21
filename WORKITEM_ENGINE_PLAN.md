# Workitem-Engine — Implementationsplan

**Kernprinzip:** Ein persistenter Workitem-Graph in Postgres, eine LLM-freie Sekretärin die ihn
abarbeitet, und DSH als Flotte von Agenten-Sessions — eine pro Workitem.

**Nicht im Scope:** Dashboard, periodische Tasks (aber schema-seitig nicht verbaut), Promotion-UI.
**Im Scope:** der Kern, ordentlich, mit Erweiterungspunkten.

---

## 0. Der Befund, der die Architektur bestimmt

Das DSH-SDK kann **keine Agent-Presets wählen**: `packages/sdk/server/src/server.ts:275`
("No preset composition: this server's compositions keep the model-facing rows in the host plane"),
und `agent-presets` ist ausschließlich im `web-app`-Bundle gemountet — nicht in `base` und nicht in
`sdk-app`.

**Konsequenz:** Rollentrennung (CDA sieht Charts, CCO sieht Social) ist über das SDK nur über
**ein eigenes Profil pro Rolle** möglich. Das ist kein Workaround, sondern deckt sich exakt mit dem
Originalprompt: *"die Sekretärin ... hat im Hintergrund einen LLM-Endpunkt gewählt"* — hier: sie
wählt einen Rollen-Prozess.

```
Sekretärin (Python, kein LLM, 1 Instanz, pollt)
  ├── dsh --profile sdk --patch roles/lead.cordis.yml   Modell A  Tools: workitems + read/write
  └── dsh --profile sdk --patch roles/cco.cordis.yml    Modell B  Tools: workitems + openbrain-cco
```

Jeder Prozess ist eine "LLM-Ressource" im Sinne des Originalprompts. Prozesse sind langlebig
(Session-Kontinuität für den Reviewer), nicht pro Workitem gespawnt.

---

## Phase 0 — Fundament verifizieren (kein Produktivcode)

Vier Annahmen, die der ganze Plan trägt. Jede wird einmal praktisch geprüft und das Ergebnis
im Plan dokumentiert.

| # | Annahme | Prüfung |
|---|---|---|
| 0.1 | SDK-Prozess auf **deinem Build** + gemeinsamer `DSH_HOME` mit `dsh web` → die per SDK erzeugte Session erscheint im Web-GUI | `DeepSeekHarness(dsh_bin="/home/daniel/.local/bin/dsh", dsh_home="/home/daniel/.dsh", ...)`, ein `run()`, danach GUI neu laden |
| 0.2 | Zwei parallele `run()` auf **verschiedenen** `session_id`s in **einem** Client funktionieren | zwei Prompts gleichzeitig, beide Antworten prüfen |
| 0.3 | Fortsetzen mit derselben `session_id` behält den Kontext | zweiter `run()` fragt nach etwas aus dem ersten |
| 0.4 | Rollen-Patch bootet und schneidet die Tools | `DeepSeekHarness(dsh_bin=..., patches=("roles/lead.cordis.yml",))` — ein Prompt, der `search_influencer_posts` verlangt, muss am fehlenden Tool scheitern |

**Fallstricke, die hier auffallen müssen:**
- **`dsh_bin` muss gesetzt werden.** Ohne die Angabe startet das Python-SDK seine eigene
  Wheel-Runtime (`deepseek-harness-runtime-bin`) — ein anderer Build als deine GUI, mit eigenem
  Node und eigenem virtuellen Plugin-Dateisystem. Dann gelten die Patches in `~/.dsh/profiles/`
  unter Umständen nicht so, wie du sie im GUI siehst.
- **Zwei Harness-Prozesse auf einem `DSH_HOME`** — Session-Logs sind getrennt (eine Datei pro
  Session), aber `workspace.json` und der Projection-Cache sind geteilt. Wenn 0.1 wackelt:
  eigenes `DSH_HOME` für die Sekretärin (schlechter, weil dann nichts im GUI sichtbar ist).
- Das Python-SDK verlangt `dsh_home` explizit und findet `~/.dsh` **nie** von selbst.
- `provider`/`model` werden beim `initialize` übergeben → pro Rollen-Prozess fest, nicht pro Session.

### Ergebnis (durchgeführt 2026-09-19)

| # | Ergebnis | Konsequenz |
|---|---|---|
| 0.1 | ✅ Session liegt durabel unter `$DSH_HOME/sessions/--home-daniel-QJM--/probe-phase0-gui/session.v3.jsonl.zstd`; `sessionQuery.listSessions()` mischt **persistierte** und laufende Sessions (`packages/session-query/session-query/src/corpus.ts:61`) → die GUI liest aus dem Korpus, nicht aus `workspace.json` | SDK-Sessions sind für die GUI lesbar. **Aber:** das SDK-Protokoll kennt **keinen** Workspace (kein einziges `workspace` in `packages/sdk/protocol`), die Session wird also keinem Workspace zugeordnet. Die Sidebar-Gruppierung bleibt offen → visuell bestätigen |
| 0.2 | ✅ 3 parallele `run()` auf 3 `session_id`s in **einem** Client: Wall 1,1 s gegen Summe 3,1 s | Ein Prozess trägt echte Parallelität → `roles.max_concurrency` ist real, nicht nur Papier |
| 0.3 | ✅ Fortsetzung derselben `session_id`: die Zahl 4711 wurde erinnert | Der Reviewer-Kontext über Runden funktioniert |
| 0.4 | ⚠️ **Mechanismus war falsch angenommen** — Phase 3 korrigiert | siehe unten |

**Der wichtigste Befund: `ctx.tools.restrict()` funktioniert nicht aus einem `--patch`.**

```
Error: tools.restrict() requires a scoped context (agent.ctx):
a context-global restriction would mask every agent — deny the tool for the intended agent instead
```

`--patch`-Overlays werden im Root-Kontext angewandt; `restrict()` ist nur innerhalb eines
Agent-Scopes legal (also in einem Preset des Web-Profils, nicht in einem Profil-Patch des SDK).

**Der Mechanismus, der funktioniert, ist A/B-verifiziert:** ganze Zeilen per `disabled: true`
abschalten. Der `--patch` wird nach dem Home-Patch angewandt und darf dessen Zeilen deaktivieren.

| Lauf | Prompt: „Rufe `mcp__openbrain-cco__search_influencer_posts` auf" | Antwort |
|---|---|---|
| A — ohne Rollen-Patch | Tool sichtbar | `VORHANDEN` |
| B — mit `roles/lead.cordis.yml` (4 MCP-Zeilen `disabled: true`) | Tool weg | `NICHT-VORHANDEN` |

Grenze: gefiltert wird auf **Server**-Ebene, nicht innerhalb eines Servers. Für die v1-Rollen
(lead = kein Server, cco = genau ein Server) reicht das exakt.

**Nebenbei:** die SDK-Version aus PyPI (`0.1.5rc1`) deckt sich exakt mit dem Checkout
(`0.1.5-rc.1`) — trotzdem bleibt `dsh_bin` gesetzt, damit beide Oberflächen denselben Build
benutzen. Ein eigenes `DSH_HOME` braucht `settings.yaml` **und** `.credentials.yaml` kopiert,
sonst hat der Prozess kein Modell.

---

## Phase 1 — Datenschicht (Migration `019_workitem_engine.sql`)

```sql
create table change_items (
  id                     uuid primary key default gen_random_uuid(),
  title                  text not null,
  entry_prompt           text,                                   -- ad-hoc: der Boss-Prompt
  state                  text not null default 'running',        -- running|done|failed|silent
  -- Template-Felder: angelegt, ungenutzt (periodische Tasks bewusst nicht gebaut)
  is_template            bool not null default false,
  template_version       int,
  promoted_at            timestamptz,
  source_change_item_id  uuid references change_items(id),
  created_at             timestamptz not null default now(),
  finished_at            timestamptz
);

create table workitems (
  id               uuid primary key default gen_random_uuid(),
  change_item_id   uuid not null references change_items(id) on delete cascade,
  step_key         text not null,                 -- überlebt jede Kopie: 'plan','check','review'
  type             text not null,                 -- initial|task|review
  role             text not null,
  parent_id        uuid references workitems(id), -- Baumachse (Herkunft), NICHT die Blockade
  payload          jsonb not null default '{}',
  status           text not null default 'pending', -- pending|running|done|failed|skipped|cancelled
  result           jsonb,
  attempts         int  not null default 0,
  max_attempts     int  not null default 1,
  priority         int  not null default 0,
  round            int  not null default 1,
  lease_owner      text,
  lease_expires_at timestamptz,
  created_at       timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz
);

create table workitem_links (          -- from braucht to
  from_id uuid not null references workitems(id) on delete cascade,
  to_id   uuid not null references workitems(id) on delete cascade,
  kind    text not null,               -- 'depends_on' | 'context'
  primary key (from_id, to_id, kind)
);

create table roles (
  name            text primary key,
  patch           text not null,       -- Rollen-Patch im Repo, z.B. 'roles/cco.cordis.yml'
  model           text,
  max_concurrency int not null default 1
);

create table agent_sessions (          -- nur für Rollen mit Gedächtnis (lead_engineer)
  change_item_id uuid not null references change_items(id) on delete cascade,
  role           text not null,
  session_id     text not null,
  primary key (change_item_id, role)
);

create index on workitems (status, priority desc, created_at);
create index on workitems (change_item_id);
create index on workitems (parent_id);
create index on workitem_links (from_id, kind);
create index on workitem_links (to_id, kind);

create view ready_workitems as
select w.* from workitems w
where w.status = 'pending'
  and not exists (
    select 1 from workitem_links l
    join workitems p on p.id = l.to_id
    where l.from_id = w.id and l.kind = 'depends_on'
      and p.status not in ('done','skipped')
  );
```

**Festgelegte Invarianten** (in Kommentaren der Migration dokumentiert):
1. `status`-Übergänge: Sekretärin `pending→running` (Lease) und `running→pending` (Lease-Ablauf);
   Worker `running→done|failed`. Keine Überlappung.
2. Blockiert ist **abgeleitet** (siehe View) — es gibt keinen `blocked`-Status.
3. `skipped` verhält sich wie `done`. Ein Vorgänger mit `failed`/`skipped`/`cancelled` macht
   abhängige **Tasks** unerreichbar ⇒ sie werden selbst `skipped` (rekursiv). Ein **Review ist
   ausgenommen**: er wartet auf *Terminalität* statt auf Erfolg, damit er die Lücke bewertet
   statt zu hängen. Das ist die Antwort auf den Fehlerpfad aus der Diskussion.
4. `parent_id` = Baum, `workitem_links` = Querkanten. Ein DAG ist kein Baum.
5. `step_key` ist die stabile Knotenidentität innerhalb (Lauf, Runde) — `unique`.

Die Regeln 1 bis 3 sind nicht nur Kommentar, sondern als Constraints und Funktionen
materialisiert: `workitems_lease_check`, `workitems_finished_check`,
`workitem_recover_leases()`, `workitem_skip_cascade()`.

### Ergebnis (angewendet 2026-09-19)

```
docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  < migrations/019_workitem_engine.sql
```

Angelegt: `change_items`, `workitems`, `workitem_links`, `roles`, `agent_sessions`,
7 Indizes, View `ready_workitems`, 2 Wartungsfunktionen, 2 Rollen
(`lead_engineer` mit `roles/lead.cordis.yml`, `cco` mit `roles/cco.cordis.yml`).

Verifikation über `migrations/019_workitem_engine.verify.sql` (läuft in einer Transaktion,
die zurückgerollt wird — kein Testdatum bleibt liegen):

| Prüfung | Erwartet | Ergebnis |
|---|---|---|
| A1 nach Anlage bereit | `initial` | ✅ |
| A2 nach INITIAL | `t1, t3` | ✅ |
| A3 nach t1 | `t2, t3` | ✅ |
| A4 nach allen Tasks | `t4` (review) | ✅ |
| B1 Skip-Kaskade bei `t1 failed` | 1 Item (`t2`) | ✅ |
| B2 Endstatus | t1 failed, t2 skipped, t3 done, t4 pending | ✅ |
| B3 Review trotz Lücke bereit | `t4` | ✅ |
| B4 Lease-Erholung | 1 Item | ✅ |

---

## Phase 2 — MCP-Server `workitems` (agent-facing)

Vorbild und Bauplan: `mcp/agent-pca/` (Deno + `@modelcontextprotocol/sdk` + Hono +
`@supabase/supabase-js` + zod, Auth per `x-brain-key`).
Neuer Server `mcp/agent-wiq/`, Port **8798**, `AGENT_ID=wiq`, Supabase-Zugang wie bei den anderen.
(8796 und 8797 sind belegt: `openbrain-mcp-drawio` bzw. `openbrain-web-scraper`.)

**Tools (4, mehr nicht):**

| Tool | Zweck | Wer darf |
|---|---|---|
| `workitem_get(workitem_id)` | eigenes Item + Links lesen | alle |
| `workitem_results(ids[])` | Kontext-Pull der Vorgänger (= `get_workitem_result` aus dem Diagramm) | alle |
| `workitem_create(items[])` | Kind-Items anlegen (step_key, role, type, payload, depends_on, context) | nur `type=initial|review` |
| `workitem_finish(workitem_id, status, result)` | `running→done|failed` | nur das eigene Item |

**Bewusste Entscheidungen:**
- Ergebnisse kommen **in-band** über `workitem_finish`, nicht über stdout-Scraping. Der Agent
  entscheidet selbst, wann er fertig ist.
- Kein `status`-Setzen für fremde Items, kein Unblock-Tool: das ist Sekretärinnen-Hoheit.
- Der Server kennt den Aufrufer nicht (geteilter Key im internen Netz). Die Rollenprüfung für
  `workitem_create` ist eine **Typ**-Prüfung am Parent, keine Identitätsprüfung. Bekannte Grenze,
  dokumentieren.

**Deployment:** Service in `llm-gateway/docker-compose.yml` nach dem Muster von `mcp-pca`
(Volume-Mounts der Quelldateien, `restart: unless-stopped`, Port-Mapping),
Eintrag in `~/.dsh/cordis.patch.yml` neben den anderen vier MCP-Clients.

**Test:** MCP-Client direkt (curl `tools/list`, dann `tools/call`) gegen eine Fixture-Zeile.

### Ergebnis (2026-09-19)

Gebaut: `mcp/agent-wiq/` (`index.ts`, `tools/shared.ts`, `tools/workitems.ts`, `deno.json`,
`Dockerfile`, `tests/e2e.sh`), Compose-Service `mcp-wiq` (Port 8798, `llm-gw-mcp-wiq`).
`deno check index.ts` läuft sauber durch.

**Zwei Befunde aus dem e2e-Test, beide behoben:**

1. **`permission denied for table workitems` (42501).** PostgREST greift als `service_role`/`anon`
   zu und hat auf neue Tabellen ohne explizite Grants **keinen** Zugriff. Die Konvention dieses
   Repos ist `arwdDxtm` für beide Rollen (`\dp pca_watchlists`). Die Migration enthält die Grants
   jetzt; Sicherheitshinweis steht dort als Kommentar (RLS wäre der nächste Schritt).
2. **Eigener Bug:** `workitem_finish` setzte `status='done'`, ließ aber `lease_owner`/`lease_expires_at`
   stehen → `workitems_lease_check` schlug zu (23514). Der Lease gehört zum Zustand `running` und
   wird beim Abschluss mit geräumt. **Der Constraint hat hier genau das getan, wofür er da ist.**

Verifikation über `bash mcp/agent-wiq/tests/e2e.sh` (legt Fixture an, räumt am Ende auf):

| Prüfung | Ergebnis |
|---|---|
| `workitem_get` liest Item + Kanten | ✅ |
| `workitem_create` plant 2 Items, löst step_keys zu 2 Kanten auf | ✅ |
| `ready_workitems` nach Planung | ✅ `analyse-social`, `initial` |
| `workitem_finish` → `done` inkl. Lease-Räumung | ✅ |
| `workitem_results` zieht das Ergebnis | ✅ `{"posts":42,"sentiment":"bullish"}` |
| zweiter `workitem_finish` ist ein No-op | ✅ |
| Guard: `task`-Item darf nicht planen | ✅ `isError` |
| Review ist bereit, sobald der Vorgänger terminal ist | ✅ `bewertung` |

**Noch offen (bewusst):** der Eintrag in `~/.dsh/cordis.patch.yml`, damit die Agenten die vier Tools
überhaupt sehen. Das ist eine Änderung an der globalen Konfiguration und löst den Live-Patch-Reloader
deines Web-Baums aus — deshalb erst nach ausdrücklicher Freigabe.

---

## Phase 3 — Rollen als Patch-Dateien im Repo

Das SDK akzeptiert `patches=(...)` pro Launch (`DeepSeekHarnessConfig.patches`), und
`_default_launch_args` baut daraus `dsh --profile sdk --patch <datei> ...`. Rollen müssen also
**keine** Profile in `$DSH_HOME` sein — sie bleiben versionierte Dateien im QJM-Repo.

**Kein `.mjs`, kein `restrict()`** (Phase-0-Befund: aus einem `--patch` nicht aufrufbar).
Rollentrennung läuft über `disabled: true` auf Server-Ebene plus Persona:

```yaml
# QJM/roles/cco.cordis.yml — nur der CCO-Server bleibt übrig
- id: mcp-openbrain-pta
  disabled: true
- id: mcp-openbrain-pca
  disabled: true
- id: mcp-openbrain-cda
  disabled: true

- id: system-prompt
  config:
    personaPrefix: >-
      Du bist der Chief Communications Officer. Du recherchierst Social-Sentiment
      und beendest jedes Item mit workitem_finish.
```

`roles/lead.cordis.yml` schaltet alle vier MCP-Zeilen ab — der Lead bekommt später nur den
`workitems`-Server.

**Zwei Varianten, wie die MCP-Zeilen liegen:**

| | Aufwand jetzt | Neue Rolle | Neuer MCP-Server |
|---|---|---|---|
| **a) Home-Patch behalten + abschalten** (verifiziert) | null, das GUI bleibt wie es ist | 4 `disabled`-Zeilen kopieren | muss in jede Rollendatei |
| **b) MCP-Zeilen ins Web-Profil verschieben** | ein Umzug in `profiles/web/cordis.patch.yml` | nur den benötigten Server `insert`en | betrifft nur, wer ihn will |

(b) ist sauberer — Rollen bekommen genau das, was sie explizit anfordern, und der `disabled`-Block
verschwindet. Preis: `headless`-Läufe hätten dann keine MCP-Tools mehr und bräuchten ein eigenes
Overlay. Für v1 reicht (a); (b) ist der logische nächste Schritt, wenn eine dritte Rolle dazukommt.

**Grenze, die bleibt:** innerhalb eines Servers lässt sich nicht filtern. Braucht eine Rolle später
z.B. 3 von 13 PCA-Tools, geht das nur über einen Agent-Scope (Web-Preset oder eine eigene
Plugin-Zeile, die sich in den Agent-Kontext hängt).

**v1-Rollen (entschieden):**

| Rolle | Aufgabe | Erlaubte Tools | Prefix |
|---|---|---|---|
| `lead_engineer` | Orchestrator *und* Reviewer (INITIAL + REVIEW) | `workitems` + `read`/`write` | winzig (~4 MCP-Tools) |
| `cco` | Chief Communications Officer (der Worker) | `workitems` + `openbrain-cco` | ~19 KB MCP-Schemas |

Der Lead bekommt **keine** Markt- oder Social-Tools: er plant und bewertet, er recherchiert nicht.
Das ist gleichzeitig die schärfste Prüfung, ob die Rollentrennung wirklich greift.

**Test:** je Rolle ein `run()` mit einem Prompt, der die Rollen-Tools braucht, und einer, der
prüft, dass ein Fremd-Tool **nicht** verfügbar ist (z.B. Lead darf `search_influencer_posts` nicht
sehen).

### Ergebnis (2026-09-19)

Gebaut: `roles/lead.cordis.yml`, `roles/cco.cordis.yml`. Beide sind reine `--patch`-Overlays und
liegen versioniert im Repo — kein Profil in `$DSH_HOME`, kein `.mjs`.

Zusätzlich zu den MCP-Zeilen schalten beide Rollen die Orchestrierung ab, die sie nicht besitzen:
`tool-subagent`, `tool-subagent-fork`, `tool-subagent-control`, `tool-subagent-list-agents`,
`tool-workflow`, `tool-ralph`, `tool-goal`, `tool-web`. Arbeit wird über Workitems verteilt,
nicht über Subagenten.

**Komposition** (`--dump-config` je Rolle, 0 Loader-Warnungen):

| Rolle | abgeschaltete MCP-Server | aktiv |
|---|---|---|
| `lead_engineer` | cco, pta, pca, cda | `wiq` |
| `cco` | pta, pca, cda | `cco`, `wiq` |

**Laufzeit** (echter `run()` je Rolle, Prompt: „prüfe deine Tool-Liste"):

| Rolle | `openbrain-cco__search_influencer_posts` | `openbrain-wiq__workitem_get` |
|---|---|---|
| `lead_engineer` | `NICHT-VORHANDEN` | `VORHANDEN` |
| `cco` | `VORHANDEN` | `VORHANDEN` |

Damit ist die Rollentrennung nicht nur konfiguriert, sondern **nachgewiesen** — und der Lead kann
seine eigene Rolle nicht mehr verlassen.

Der Eintrag in `~/.dsh/cordis.patch.yml` (`mcp-wiq` neben den vier bestehenden Clients) ist gesetzt;
das Web-Profil hat live nachgeladen, die vier `mcp__openbrain-wiq__*`-Tools sind im Katalog.

---

## Phase 4 — Die Sekretärin (der Kern)

Python-Service unter `services/secretary/`, ein Instanz, kein LLM, Tick alle 2 s.

**Der Tick:**
1. `expire_leases()` — `running` mit abgelaufenem Lease → `pending`, `lease_owner=null`
2. `skip_cascade()` — Invariante 3 anwenden
3. `dispatch()` — `ready_workitems` lesen, pro Rolle bis `max_concurrency` starten
4. `reap()` — fertige `run()`-Tasks einsammeln, fehlende `workitem_finish` melden

**Dispatch eines Items:**
```python
session_id = registry.get(change_item_id, role) or new_session_id()
result = harness_for_role(role).run(build_prompt(item), session_id=session_id)
# build_prompt: payload + workitem_id + die Aufforderung, mit workitem_finish abzuschließen
```

**Wichtige Details:**
- **Session-Registry:** nur `lead_engineer` bekommt eine Zeile in `agent_sessions` — damit laufen
  INITIAL, REVIEW Runde 1 und REVIEW Runde 2 in **derselben** Session. Worker laufen stateless mit
  `session_id = str(workitem.id)`.
- **Fallback bei Protokollverletzung:** endet ein `run()` ohne `workitem_finish`, wird das Item
  `failed` mit `result={"reason": "no_report"}` — sichtbar, nicht stillschweigend akzeptiert.
- **CLI:** `secretary submit "fasse alle X-Posts von heute zusammen"` → `change_item` +
  Wurzel-Workitem (`type=initial`, `role=lead_engineer`, `payload.prompt`).
- **Kein Unblock-Code.** Bereitschaft ist die View. Das ist der ganze Grund, warum Polling hier
  einfacher ist als Events.

**Test:** der komplette Lauf aus dem Originalprompt — Prompt rein, Lead plant, zwei Worker laufen
parallel, Review schreibt den Report, `change_items.state='done'`. Alle Zeilen danach in der DB
nachvollziehbar.

### Ergebnis (2026-09-19) — der Kreis ist geschlossen

Gebaut: `services/secretary/` (config, store, framing, harness, loop, cli), eigenes venv,
`env.sh`, README. Kein LLM in dieser Schicht.

**Lauf 1** (`e731c098`): „Ermittle, wie viele X-Posts heute in der Datenbank liegen…"

```
02:03:45 dispatch initial [initial/lead_engineer] runde=1 versuch=1/2
02:04:35 dispatch count-x-posts-today [task/cco] runde=1 versuch=1/2
02:07:31 dispatch summarize-stossrichtung [task/cco] runde=1 versuch=1/2
02:07:59 dispatch review-count-and-summary [review/lead_engineer] runde=1 versuch=1/2
02:08:23 LAUF DONE
```

Der Lead hat selbst geplant: `count-x-posts-today → summarize-stossrichtung → review`, mit
`depends_on`-Ketten und `context_refs`. Die Sekretärin hat die Kette **selbstständig getaktet** —
kein Unblock-Code, nur die View. Review-Ergebnis: `satisfied=true`, mit ausgewiesenen Lücken
(„Auslegungsspielraum", „dünne Datenlage", „Sample-Schiefe @zerohedge 40%").

**Der eine echte Bug, den erst der Live-Lauf gezeigt hat:** `remember_session()` war in
`store.py` geschrieben, wurde im Dispatch aber **nie aufgerufen**. Damit lief das REVIEW in einer
anderen Session als das INITIAL — der Reviewer hatte seinen eigenen Planungskontext nicht. Die
Tabelle `agent_sessions` war leer, obwohl das Schema und der Plan genau das vorsehen.

Behoben durch `store.ensure_session()` (Insert-dann-Lesen, damit auch zwei gleichzeitige Items
derselben Rolle dieselbe Session bekommen).

**Lauf 2** (`afd6f584`) nach dem Fix — der Beweis steht im Log:

```
dispatch initial                    … session=79fd8ed0 (geteilt)
dispatch count-monitored-influencers … session=4ccacfe7            ← Worker, zustandslos
dispatch review-monitored-count     … session=79fd8ed0 (geteilt)   ← DIESELBE wie INITIAL
```

`agent_sessions`: genau eine Zeile, `lead_engineer → 79fd8ed0`. Der Reviewer sieht jetzt seine
eigene Planung und in Runde 2 seinen eigenen Fehlschlag.

**Damit ist jede Schicht einmal echt durchlaufen:** Schema → MCP-Server → Rollen → Sekretärin →
Lauf mit Planung, Ausführung, Review und Abschluss. Die Läufe liegen unter dem echten
`DSH_HOME` und sind in der Web-GUI als Chats nachvollziehbar.

**Offen (bewusst):** kein Daemon-Betrieb (systemd-Unit fehlt), keine Kostenbremse pro Lauf,
kein Dashboard. Die Phasen 5 und 6 aus diesem Plan sind noch nicht gebaut — Lauf 1 und 2 waren
beide Runde-1-Erfolge, der Fehlerpfad über zwei Runden ist bisher nur auf SQL-Ebene verifiziert
(`019_workitem_engine.verify.sql`, Szenario B).

---

## Phase 5 — Review-Runde und Fehlerpfad

- `attempts` + `max_attempts` (Default 2), `round` am Item.
- REVIEW liefert `{satisfied: false, reason, new_tasks[]}` → die Sekretärin legt Runde 2 an,
  **dieselbe** `session_id` für den Lead.
- Zweiter Fehlschlag → `change_items.state='failed'`, kein Sonderweg: die Session steht im GUI,
  du öffnest sie und siehst den ganzen Verlauf.
- `max_attempts` erreicht ⇒ `skipped`-Kaskade für alles dahinter, kein Hängenbleiben.

**Test:** absichtlich unbefriedigendes Ergebnis, Runde 2, absichtlicher zweiter Fehlschlag,
Endzustand `failed` und im GUI sichtbar.

---

## Phase 5 — Ergebnis (2026-09-19)

### Der Dämon läuft

`services/secretary/dsh-secretary.service` ist installiert, `enabled` und `active (running)` als
systemd-User-Unit. Startet mit der Sitzung, `Restart=always`, `TimeoutStopSec=60`, SIGTERM wird
sauber behandelt (laufende Aufträge dürfen zu Ende laufen, der Rest wird über den Lease-Ablauf
beim nächsten Start erholt). Log: `journalctl --user -u dsh-secretary -f`.

### Drei Läufe, drei verschiedene Ausgänge

| Lauf | Auftrag | Ausgang |
|---|---|---|
| `7fb15647` | Schlusskurs VLO vom 15.09.2027 (unmöglich) | Task `failed` → abhängiger Task `skipped` (Kaskade) → Review `failed` → **LAUF FAILED** |
| `afdbe90a` | ≥500 Belege für VLO-Aufmerksamkeit (zwingt zum zweiten Versuch) | Runde 1 → Review plant **Runde 2** → läuft |
| frühere | X-Posts / Influencer-Zahl | **LAUF DONE** nach Runde 1 |

**Bewiesen durch Lauf `afdbe90a`:**

- zwei CCO-Tasks liefen **parallel** (02:27:42 beide) — die asynchrone Auslastung aus dem Originalprompt
- das Review plante über `workitem_create` selbstständig **Runde 2** (`vlo-corpus-expanded`, `runde=2`, 02:34:48)
- INITIAL und REVIEW Runde 1 liefen in **derselben Session** (`07e2f901`, beide als „(geteilt)" geloggt)

**Deterministisch zusätzlich abgesichert** über `mcp/agent-wiq/tests/rounds.sh`: ein Review legt
Items in Runde+1 an, die neue Runde ist ein eigener Namensraum (Verweise über Rundengrenzen werden
abgewiesen), doppelte step_keys in derselben Runde werden abgewiesen.

### Zwei ehrliche Befunde

1. **Das Modell kann die Retry-Politik überstimmen.** Im unmöglichen Fall begründete das Review:
   *„ein Schlusskurs für einen zukünftigen Tag existiert nicht … eine weitere Runde wird nicht
   geplant"* — und schloss direkt mit `failed` ab, statt wie instruiert Runde 2 zu planen.
   Inhaltlich richtig, aber es heißt: `max_rounds` ist eine **Obergrenze, keine Garantie**.
2. **Ein Zwei-Runden-Lauf dauert etwa doppelt so lang.** Die Phasen sind strukturell sequenziell:
   INITIAL → (Tasks parallel) → REVIEW → (Tasks parallel) → REVIEW. Der Lead hat
   `max_concurrency=1`, und ein Review kann erst starten, wenn alle Vorgänger terminal sind.
   Gemessen: Runde 1 ≈ 9 min, ein voller Zwei-Runden-Lauf ≈ 20 min. Wer das verkürzen will, muss
   an der Planungstiefe des Leads ansetzen, nicht an der Parallelität.

---

## Nachtrag — warum Läufe lange dauern (und was es gekostet hat)

Aufgefallen, weil ein Zwei-Runden-Lauf 36 Minuten brauchte. Gemessen mit
`services/secretary/tools/session_stats.py` (schreibt Modell-Runden gegen Tool-Zeit auf).

**Der Befund:**

| Session | Dauer | Modell-Runden | Tools | davon shell/fs |
|---|---|---|---|---|
| CCO, vorher | 248 s | 48 | 63 | **51** (37× bash) |
| CCO, pathologisch | **2135 s** | **151** | 177 | **115** (96× bash) |
| Lead, vorher | – | 46 | 64 | 55 (41× bash) |

Der CCO hat **die Datenbank selbst erforscht**, statt seine MCP-Tools zu benutzen:

```
docker exec openbrain-db psql -c "\dt"
psql -h 127.0.0.1 -p 5433 -U postgres -c "\dt"
grep -n "from(" mcp/agent-cco/tools/shared.ts      ← Schema reverse-engineeren
```

Bei einer *Zählfrage* ist SQL der naheliegende Weg — der Agent hat rational gehandelt.
Er konnte es, weil ich ihm `tool-bash` und `tool-fs` gelassen hatte.

**Zwei Ursachen, beide echt:**

1. **DSH hat keine Rundengrenze — by design.** `packages/core/agent-loop/README.md:200`:
   *"No built-in turn budget — tool calls or steering continue the current turn; a policy that
   bounds runaway turns must cancel from an existing lifecycle extension point."* 151 Runden sind
   also nicht ein Fehler des Harness, sondern die Folge fehlender Begrenzung.
2. **Mein Rollen-Schnitt war unvollständig.** Phase 3 hat die MCP-Server getrennt, aber die Shell
   offen gelassen. Eine Rolle mit `bash` + Docker ist keine eingeschränkte Rolle — sie ist eine
   uneingeschränkte Rolle mit kürzerer Toolliste. Die Aussage „der Lead kann seine Rolle nicht
   verlassen" galt damit **nur auf MCP-Ebene, nicht auf Systemebene**. Zu stark formuliert.

**Nebenbefund mit Nebenwirkung:** im pathologischen Lauf rief der CCO **19×
`sync_influencer_posts`** auf — er versuchte, die geforderten 500 Belege *herbeizuschaffen*.
Ein unerreichbarer Auftrag treibt einen Agenten mit Schreibrechten zu schreibenden Handlungen.

**Der Fix** (in `roles/*.cordis.yml`): `tool-bash`, `tool-pwsh`, `tool-fs`, `tool-fs-search`
für beide Rollen abgeschaltet. Die Rollen haben damit **keine Wahl mehr**, als ihre MCP-Tools zu
benutzen. Das Ergebnis gehört ohnehin in `workitem_finish`, nicht in eine Datei.

**Der A/B-Beweis** (derselbe Auftragstyp, einmal vorher, einmal nachher):

| | Dauer | Runden | Tools | shell/fs |
|---|---|---|---|---|
| vorher (`5682f8ae`) | 248 s | 48 | 63 | 51 |
| nachher (`e81ce461`) | **28 s** | **6** | 9 | **0** |

Faktor 9 bei der CCO-Aufgabe. Der komplette Lauf (INITIAL → TASK → REVIEW) brauchte
**48 Sekunden** statt ~6,5 Minuten: `11:03:27 → 11:04:15`.

**Was offen bleibt:** es gibt weiterhin keine Rundengrenze. Die einzigen Bremsen sind die
Tool-Auswahl und die Prompt-Disziplin. Wer eine harte Obergrenze will, muss sie über
`agent/turn-stopping` als Policy bauen — das ist DSH-Erweiterungsarbeit, kein Konfigurationswert.

---

## Phase 7 — Budgets (Konzept, noch nicht gebaut)

Idee: der Lead schätzt beim Planen ein Budget, die Sekretärin verwaltet es, bei Überschreitung
geht es direkt zum Review, und der Lead bewertet, ob die Aufgabe mit den Limits lösbar ist.

**Das trägt — mit einer Korrektur bei der Rollenverteilung.** Drei geprüfte Fakten bestimmen das
Design:

| Frage | Befund |
|---|---|
| Kann die Sekretärin einen Lauf abbrechen? | **Nein.** `packages/sdk/protocol/src/types.ts:116-118` kennt genau `initialize`, `session/prompt`, `shutdown` — kein cancel |
| Runden messbar? | **Ja.** `step/start` liegt im Session-Log und kommt live über Notifications |
| Tokens messbar? | **Nein.** Kein `usage`-Event im Log; die Zahlen liegen im LLM-Adapter |
| Erzwingungspunkt? | **Ja.** `agent/pre-step` (Waterfall) liefert `{kind:'reject'}` oder `{kind:'enter', messages}` |

Die Sekretärin ist damit **Buchhalterin, nicht Vollstreckerin**. Die Durchsetzung gehört in ein
Budget-Plugin, das an `agent/pre-step` hängt.

### Die drei Schichten

1. **Lead schätzt** — `workitem_create` bekommt pro Item `budget: {rounds, tokens, seconds}`.
2. **Sekretärin verwaltet** — Summe der Item-Budgets ist das Projektbudget; sie schreibt das
   Item-Budget **in die Rahmung** (der Agent muss es kennen, sonst ist die Policy eine Überraschung
   statt eine Leitplanke) und rechnet den Ist-Verbrauch nach.
3. **DSH erzwingt** — ein `.mjs` neben den Rollen-Patches, von diesen gemountet:

```js
// budget-policy.mjs — hängt am Agent-Scope, zählt Schritte, greift vor dem Request ein
export function apply(ctx) {
  ctx.on('agent/created', ({ agent }) => {
    let steps = 0
    agent.ctx.on('agent/pre-step', (payload, next) => {
      if (++steps < agent.ctx.get('budget').rounds) return next()
      // letzte Runde: der Agent schliesst SELBST ab statt abgeschnitten zu werden
      return { kind: 'enter', messages: [anweisungBudgetEerschoepft] }
    })
  })
}
```

`enter` statt `reject` ist der Kern: der Agent bekommt **genau eine letzte Runde**, um
`workitem_finish(status='failed', result={reason:'budget_exceeded'})` aufzurufen. Danach `reject`.

### Warum das dein Routing schon trifft

Ein Item, das sein Budget reißt, endet `failed` → die Skip-Kaskade überspringt die Geschwister →
**das Review läuft trotzdem** (es ist von der Kaskade ausgenommen, seit Phase 1). Das Review sieht
Ergebnis, Abbruchgrund und Verbrauch und entscheidet semantisch:

- erfüllt → `done`
- lösbar mit mehr Budget → plant **Runde 2 mit neuem Budget**
- nicht lösbar → `failed`

Das ist genau „der Lead bewertet, ob mit den Limits die Aufgabe gelöst werden kann" — und es
braucht **keine** neue Mechanik, nur die Budget-Felder und das Plugin.

### Vier Korrekturen an der Idee

1. **Zeit ist die schlechteste der drei Metriken.** Sie misst Warteschlange und Provider-Latenz
   mit; ein Item, das drei Minuten in der Schlange stand, gilt als teuer, obwohl es billig war.
   → Nur als großzügiger Notausstieg, **Runden als Hauptmetrik**.
2. **Tokens fallen für v1 weg**, weil der Verbrauch nicht im Session-Log steht. Entweder der
   LLM-Adapter schreibt ihn künftig in die Session (Harness-Erweiterung), oder die Metrik entfällt.
3. **Budget gehört in die Rahmung**, nicht nur in die Datenbank.
4. **Nicht jede Überschreitung darf zum Review.** Reißt ein *Item* sein Budget: `failed`, der Rest
   läuft weiter. Erst wenn das *Projekt*budget reißt, wird abgekürzt (Rest skippen, Review). Sonst
   killt ein einzelnes schlecht geschätztes Item den ganzen Lauf.

### Umsetzungsreihenfolge

| Stufe | Inhalt | Aufwand |
|---|---|---|
| v1 | `budget.rounds` pro Item, in der Rahmung, Budget-Policy mit letzter Runde | klein — ein `.mjs` + zwei Spalten |
| v2 | Projektbudget in der Sekretärin, Ist-Verbrauch aus den Session-Logs nachgerechnet | mittel |
| v3 | Tokens, sobald der Verbrauch im Harness verfügbar ist | hängt am Harness |

---

## Phase 7 — Budgets: gebaut (v1)

Gebaut und an echten Läufen verifiziert.

**Was entstanden ist:**

| Baustein | Datei |
|---|---|
| Spalten `budget` (Soll) und `usage` (Ist) + View-Neuaufbau | `migrations/020_workitem_budget.sql` |
| `budget`-Parameter in `workitem_create`, Anzeige in `workitem_get`/`workitem_results` | `mcp/agent-wiq/tools/workitems.ts` |
| `budget_json` + `available_roles` in der Rahmung | `services/secretary/secretary/framing.py` |
| Erzwingung an `agent/pre-step` | `roles/budget-policy.mjs` |
| Budget-Schaetzung in der Lead-Persona | `roles/lead.cordis.yml` |
| `submit --rounds/--tokens` (Deckel des Bosses) | `services/secretary/secretary/cli.py` |

**Arbeitsweise der Policy:** reisst das Budget, bekommt der Agent **eine letzte Runde**
(`{kind:'enter'}`) mit der Anweisung, selbst `workitem_finish(status='failed',
result={reason:'budget_exceeded', needs:...})` zu rufen; danach `{kind:'reject'}`. Er wird also
nicht abgeschnitten, sondern schliesst sauber ab — und das Review (von der Skip-Kaskade
ausgenommen) bewertet die Lücke und kann eine Runde mit höherem Budget planen.

**Verifiziert:** die Kette Boss → Lead → Items → Policy steht.

```
erste pre-step: budget={"rounds":3,"tokens":600000}                  ← Deckel des Bosses
erste pre-step: budget={"rounds":15,"tokens":450000,"seconds":3600}  ← vom Lead geschaetzt
erste pre-step: budget={"rounds":10,"tokens":300000,"seconds":2400}  ← vom Lead geschaetzt
```

### Fünf Fehler, die erst der Live-Lauf gezeigt hat

Alle fünf waren unsichtbar, solange man nur die Teile testet:

1. **`load_item` lud `attempts` nicht.** Der Reap-Pfad greift darauf zu — aber nur, wenn ein
   Lauf **ohne** `workitem_finish` endet. Alle früheren Läufe endeten sauber, der Pfad war nie
   gelaufen. Erst der Budget-Test hat ihn erreicht (`KeyError: 'attempts'`).
2. **`@deepseek-ai/dsh-llm` war aus dem Repo nicht auflösbar.** Ein `--patch`-Plugin wird relativ
   zu seiner Datei aufgelöst, nicht relativ zum Profil. Fix: `roles/node_modules` als Symlink auf
   `~/.dsh/profiles/node_modules`. (Der Loader meldet das sauber: *"Cannot find package
   '@deepseek-ai/dsh-llm' imported from …"*.)
3. **Budget zu früh gelesen.** `readBudget` lief in `agent/created` — da ist die erste
   User-Nachricht noch nicht in der Session, also griffen immer die Defaults. Fix: beim ersten
   `agent/pre-step` lesen, dann liegen die Nachrichten vor.
4. **`ready_items` wählte `budget` nicht mit aus.** Die Policy bekam `budget_json: {}` und
   arbeitete mit Defaults. Sichtbar nur im Diagnose-Log des Plugins.
5. **Die View sah die neuen Spalten nicht.** Postgres friert die Spaltenliste einer View bei der
   Erstellung ein; `select w.*` in 019 enthält `budget` nicht, obwohl die Spalte in der Tabelle
   steht. Fix: `create or replace view` in 020.

Fehler 1 und 5 sind der Grund, warum dieses Projekt Live-Läufe braucht: beides sind Pfade, die
kein Unit-Test und kein `dump-config` erreicht.

### Der Kreislauf läuft (2026-09-19)

`usage` wird jetzt zurückgeschrieben (`services/secretary/secretary/usage.py` + `store.add_usage`).
Die Zahlen kommen aus `RunResult.events` — kein Log-Parsing, sie liegen im Lauf bereits vor.
Mehrere Versuche summieren sich, denn die Frage lautet „was hat dieses Item insgesamt gekostet".

Ein Lauf zeigt die vollständige Kette:

```
initial         budget={}                          usage={rounds:5, tokens:29845}
x-topics-today  budget={rounds:12, tokens:360000}  usage={rounds:9, tokens:549619}  -> failed
review-…-r2                                        -> plant Runde 2 mit hoeherem Budget
x-topics-today  budget={rounds:16, tokens:700000}  <- der Lead hat erhoeht
```

Das gescheiterte Item meldete in seinem letzten Schritt:

> „Ein erneuter Versuch braucht ein hoeheres Token-Budget (realistisch 500k+, verbraucht wurden
> 422.621 in 9 Runden)."

Und das Review entschied anhand derselben Zahlen:

> „Runde 1 scheiterte mit reason='budget_exceeded' (9 Runden, 422.621 von 360.000 Tokens) …
> Das angegebene 'needs' ist konkret und loesbar => neue Runde mit groesserem Budget statt Abbruch."

**Das ist die vollständige Umsetzung der Anforderung**: Budget gerissen → Abbruch über den
letzten Schritt → Routing zum Review (die Skip-Kaskade lässt es stehen) → der Lead bewertet
anhand von Soll und Ist → Runde 2 mit angepasstem Budget.

### Nebenbefund: der Spill braucht ein Dateisystem

Das gescheiterte Item diagnostizierte seinen eigenen Engpass und deckte damit einen Fehler
meiner Rollen-Verschärfung auf:

> „die Tool-Ausgaben [werden] bei ~40 KB abgeschnitten und laufen in Spill-Dateien, die in
> dieser Umgebung nicht lesbar sind (kein Datei-Read-Tool verfügbar)"

Der `spill-policy`-Mechanismus schreibt grosse Tool-Ergebnisse in Dateien und nennt dem Agenten
nur den Pfad. Mit abgeschaltetem `tool-fs` ist diese Ausgabe **verloren** — der Agent zahlt die
Tokens, bekommt die Daten aber nicht.

Der Spill-Hinweis ist dabei kein Nebensatz, sondern ein **Vertrag des Harness mit dem Modell**:

```
(41234 bytes omitted. Full formatted result stored at: /tmp/dsh-spill-XXXX/….
 Use read with offset/limit, or grep this path to search within it.)
```

Der Harness nennt dem Modell **zwei Werkzeuge beim Namen**. Fehlt eines davon, ist die Ausgabe
eines *erlaubten* Tools unerreichbar: der Agent zahlt die Tokens und sieht die Daten nicht.

### PRINZIP: Rollentrennung kappt Datenquellen, nicht Datenzufuhr

| | Zeile | Warum |
|---|---|---|
| **aus** | `tool-bash`, `tool-pwsh` | Daten**quellen**: die Shell öffnete den Weg zu DB-Schema, Docker und Repo — 96 Aufrufe, 36 Minuten |
| **an** | `tool-fs` (`read`/`write`/`edit`) | Daten**zufuhr**: ohne `read` ist jede gespillte Tool-Ausgabe verloren |
| **an** | `tool-fs-search` (`glob`/`grep`) | Daten**zufuhr**: der Spill-Hinweis nennt `grep` ausdrücklich zum Durchsuchen grosser Ergebnisse |

Ausgeschaltet bleiben nur die Rollen-Fremdkörper: `tool-subagent*`, `tool-workflow`, `tool-ralph`,
`tool-goal`, `tool-web`.

### Die zweite Lehre: die Werkzeugfläche muss mit den Rollen wachsen

Ein Lauf nach der Verschärfung zeigt die Kehrseite. Der CCO sollte zählen, wie viele Influencer
konfiguriert sind, und meldete:

> „Die MCP-Tool-Oberflaeche des CCO bietet keine Roh-SQL-/Count-ohne-Filter-Faehigkeit
> (`manage_influencers LIST` filtert hart auf `is_active = true`) … daher war der Nenner nicht
> messbar und wurde bewusst NICHT geschaetzt. Fuer einen erneuten Durchlauf noetig: (a) direkter
> DB-Zugriff, z.B. `select is_active, count(*) from x_users group by 1`"

Er wollte genau den Weg, den die Rollentrennung ihm genommen hat. Das ist **korrektes Verhalten**
(keine Erfindung, ehrliches `failed` mit `needs`), aber es legt eine Regel offen:

> **Jede Fähigkeit, die man der Rolle nimmt, muss als Werkzeug existieren — sonst wird sie
> stillschweigend unmöglich.**

Vorher ging „wie viele Nutzer gibt es insgesamt" irgendwie per Shell. Jetzt geht es nicht mehr,
weil das MCP-Tool nur die aktiven zählt. Die Rollentrennung hat die Frage nicht verboten, sie hat
sie unentscheidbar gemacht — und das fällt nur auf, wenn jemand sie stellt.

Für die Praxis heisst das: der Rollen-Schnitt und die MCP-Tool-Fläche müssen zusammen wachsen.
Jedes neue Verbot ist ein Auftrag, das entsprechende Werkzeug zu bauen.

### Die Lehre für die Rollenprüfung

Phase 3 hat die **Toolliste** geprüft — „sieht der Lead `search_influencer_posts`?" — und das war
richtig, aber zu flach. Die Prüfung hat nicht gefragt, ob eine Rolle mit den **Ausgaben ihrer
erlaubten Tools** arbeiten kann. Ein Rollentest muss eine echte Arbeitslast durchlaufen, nicht
nur die Werkzeugliste vergleichen. Ein Rollen-Schnitt, der die Datenzufuhr kappt, sieht in jeder
Toollisten-Prüfung korrekt aus und scheitert erst im Lauf.

### Sechster Befund: der Lauf wurde rot, obwohl er gelöst wurde

Nach dem Zwei-Runden-Lauf stand `LAUF: failed` in der Datenbank — obwohl Runde 2 erfolgreich
war und das Review `satisfied=true` meldete. Ursache: die Abschlussregel fragte nur
„existiert irgendwo ein `failed`?", und das in Runde 1 gerissene Budget-Item steht weiterhin
mit `failed` in der Tabelle (es wurde von Runde 2 abgelöst, nicht gelöscht).

**Fix:** der **letzte abgeschlossene Review** entscheidet. Sein `satisfied` schlägt alles andere;
erst wenn es keinen gibt, gilt „irgendein failed". Danach: `LAUF: done`.

Das ist derselbe Fehlertyp wie die anderen fünf: eine Regel, die im Einzeltest richtig aussieht
und im mehrrundigen Lauf falsch wird.

**Noch offen:** Projektbudget über mehrere Items hinweg (Summe statt Item-Deckel).
`seconds` ist als Metrik erfasst, wird aber nicht durchgesetzt.
Abgelöste Items aus früheren Runden bleiben als `failed` stehen — für die Nachvollziehbarkeit
richtig, aber eine Kostenübersicht muss sie von echten Fehlschlägen unterscheiden.

---

## Phase 6 — Härtung

- **Neustart-Test:** Sekretärin mitten im Lauf killen → Lease läuft ab → Item wird erneut
  dispatched → keine Doppel-Schreibung (`attempts` + Idempotenzschlüssel im `workitem_finish`).
- **Idempotenz:** `workitem_finish` auf ein bereits abgeschlossenes Item ist ein no-op, kein Fehler.
- **Zyklusprüfung:** `workitem_create` lehnt Kanten ab, die einen Zyklus erzeugen (DFS über die
  schon existierenden Links des Change-Items).
- **Logging:** ein strukturierter Log pro Tick (dispatch/complete/fail/lease) — das ist das
  Minimum, das dir später ein Dashboard ersetzt.

---

## Erweiterungspunkte (bewusst offen gelassen, nichts verbaut)

| Erweiterung | Was schon da ist | Was fehlt |
|---|---|---|
| Periodische Tasks | `change_items.is_template`, `template_version`, `promoted_at`, `source_change_item_id`; `step_key` überlebt Kopien | Kopier-Routine, Scheduler, `roles.model`-Auswertung |
| Dashboard | jede Kante und jedes Item ist eine Zeile → `WITH RECURSIVE` über 2 Tabellen; `parent_id` = Baumachse, Links = Overlay | UI |
| Semantische Trigger | `round`/`attempts`-Mechanik, Rollen-Profile | `silent`-Zustellung, Vorverdikt-Vergleich |
| Mehr Rollen | `roles`-Tabelle + Patch-Bauplan pro Rolle | weitere `.mjs`-Filter |

---

## Entschieden

1. **Python** für die Sekretärin (`deepseek-harness-sdk`), passend zu deinen übrigen Services.
2. **Diese Installation, explizit:** `dsh_bin="/home/daniel/.local/bin/dsh"` und
   `dsh_home="/home/daniel/.dsh"` — damit Sekretärin und GUI derselbe Build mit denselben Profilen
   sind und die Runs in der GUI sichtbar werden. Verifikation in Phase 0.1.
3. **v1-Rollen:** `lead_engineer` (Orchestrator + Reviewer) und `cco` (Worker).
