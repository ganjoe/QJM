
# Draw.io MCP — Migration nach QJM (Implementierungsplan)

Stand: 2026-09-19 · Autor: DSH (Trader-Preset, Session 6b0d1a99) · Status: **Entwurf zur Freigabe**

---

## 0. Auftrag und Erfolgskriterien

**Auftrag.** Den vorhandenen draw.io-MCP-Server aus `openBrain` nach `/home/daniel/QJM/` migrieren, die
Google-Drive-/Google-Cloud-Anbindung vollständig entfernen und die erzeugten `.drawio`-Dateien lokal im
`dsh_playground` ablegen. Der Server soll im DSH-Session-Katalog verfügbar sein. Die Prompts (Tool-Beschreibungen,
Personas, Namensregeln) sind mitzuziehen.

**Erfolgskriterien.**

1. Ein MCP-Server `openbrain-drawio` läuft aus QJM heraus (Image/Container `llm-gw-mcp-drawio`, Port **8796**).
2. Die drei Tools `create_drawio_diagram`, `list_drawio_diagrams`, `read_drawio_diagram` funktionieren
   **ohne** jede Google-Abhängigkeit; keine `googleapis`-Imports mehr.
3. Eine im Chat erzeugte Datei liegt physisch unter `/home/daniel/QJM/dsh_playground/drawio/<name>.drawio`
   und ist über list/read wieder auffindbar.
4. In einer **neuen** DSH-Session erscheinen die Tools als `mcp__openbrain-drawio__*`.
5. Prompts und Namensregeln verweisen auf den lokalen Speicher, nicht mehr auf Google Drive.
6. Path-Traversal, Überschreiben und Dateinamens-Bereinigung sind getestet.

---

## 1. Ist-Zustand (Befund, verifiziert)

**Frage „Habe ich Zugriff?" — Antwort: in dieser DSH-Session nein.** Es gibt zwar einen laufenden
draw.io-MCP-Server, aber er ist im DSH-Home-Patch nicht registriert; darum fehlen mir die Tools.

| Fakt | Fundstelle |
|---|---|
| Server-Quellcode | `/home/daniel/openBrain/mcp-drawio/` (Deno, MCP-SDK 1.24.3, Hono, `googleapis@144`) |
| Dateien | `index.ts` (HTTP, Port 8796), `stdio.ts`, `tools.ts`, `drawio_generator.ts`, `google_drive.ts`, `deno.json`, `Dockerfile` |
| Tools | `create_drawio_diagram`, `list_drawio_diagrams`, `read_drawio_diagram` |
| Speicher | Google Drive, Ordner `openBrain`, via `google_drive.ts`; Zugang über `GOOGLE_*` aus `/home/daniel/openBrain/.env` |
| Container | `openbrain-mcp-drawio`, Port `8796:8796`, definiert in `/home/daniel/openBrain/docker-compose.yml` Z. 379–394; **läuft** (Up 3 days) |
| Auth | `MCP_ACCESS_KEY`; ohne Key HTTP 401, mit Key HTTP 406 (406 = MCP-Accept-Header fehlen) — Server lebt |
| DSH-Registrierung | `/home/daniel/.dsh/cordis.patch.yml`: nur `mcp-openbrain-cco/pta/pca/cda/wiq` — **kein drawio** |
| QJM-Altverweise | `QJM/.agents/mcp_config.json` und `QJM/mcp/antigravity_mcp_config.json`: stdio → `/home/daniel/openBrain/mcp-drawio/stdio.ts` |
| openBrain-Konsumenten | `agent-ea/config.yaml`, `agent-pta`, `agent-pca` → `http://mcp-drawio:8795` (**falscher Port**: 8795 = CDA); `agent-cco`, `agent-srm` → `http://mcp-drawio:8796` |
| Namensregel | `/home/daniel/openBrain/.agents/AGENTS.md`: `openbrain-[agent/modul]-[thema]-[version]`, „created on Google Drive" |
| Port-Reservierung | `QJM/WORKITEM_ENGINE_PLAN.md` Z. 217: 8796 = drawio, 8797 = web-scraper (belegt) |
| dsh_playground | `/home/daniel/QJM/dsh_playground` (setgid Gruppe `daniel`); in QJM-Compose teils `:ro` (cco), teils `:rw` (chart-viewer). **Kein `drawio/`-Unterordner** vorhanden |
| QJM-MCP-Konvention | `QJM/mcp/agent-<x>/` mit `index.ts`, `tools/`, `deno.json`, `Dockerfile`; Compose-Service `mcp-<x>` in `QJM/llm-gateway/docker-compose.yml` |

---

## 2. Zielbild

```
Chat / Agent
   │  MCP streamable-http, x-brain-key
   ▼
openbrain-drawio  (QJM/mcp/agent-drawio, Container llm-gw-mcp-drawio, :8796)
   │  Dateisystem, DRAWIO_BASE_DIR=/dsh_playground/drawio
   ▼
/home/daniel/QJM/dsh_playground/drawio/<name>.drawio      ← einzige Quelle der Wahrheit
```

Kein Google Drive, kein `GOOGLE_*`, keine Cloud-Buckets. Der Host-Pfad
`/home/daniel/QJM/dsh_playground/drawio` wird in den Container auf `/dsh_playground/drawio` gemountet.

---

## 3. Entscheidungen und Annahmen

| # | Entscheidung | Empfehlung | Begründung |
|---|---|---|---|
| E1 | Zielordner | `/home/daniel/QJM/mcp/agent-drawio/` | konsistent zu `mcp/agent-cda`, `agent-wiq` |
| E2 | Speicherwurzel | `/home/daniel/QJM/dsh_playground/drawio`, im Container `/dsh_playground/drawio` | Nutzerauftrag „einfach in dsh_playground"; eigener Unterordner hält das Playground-Root sauber |
| E3 | DSH-Registrierung | **A)** Home-Patch `~/.dsh/cordis.patch.yml`; **B)** Ersatzweise Insert im QJM-Trader-Preset | A ist konsistent zu den übrigen openbrain-Servern, liegt aber **außerhalb** von /QJM und braucht Schreibfreigabe; B bleibt komplett in /QJM |
| E4 | Ports | `8796` (host+container) | bereits reserviert, alter Container wird vorher gestoppt |
| E5 | Dateiname | aus `title` slugifiziert + `.drawio`; optionales `filename`-Override | ein stabiles, URL-sicheres Schema |
| E6 | Lese-Schlüssel | Dateiname relativ zur Basis statt Google-`fileId` | lokal gibt es keine fremde ID; Tool-API wird bewusst geändert |
| E7 | Alte Drive-Diagramme | **nicht** automatisch übernehmen | Nutzer: „brauchen Google Cloud Storage nicht"; ein optionales Export-Skript ist nicht Teil dieses Plans |
| E8 | openBrain-Cutover | separater, optionaler Schritt (außerhalb /QJM → Freigabe) | QJM-Migration hat Vorrang; openBrain bleibt bis zur Verifikation unangetastet |
| E9 | Namensschema | `openbrain-[agent/modul]-[thema]-[version]` beibehalten | bestehende Konvention, nur Speicherort ändert sich |

**Annahme:** In `QJM/llm-gateway/.env` ist `MCP_ACCESS_KEY` gesetzt (bestätigt: Key existiert).
**Annahme:** Der Container darf `dsh_playground` schreiben; Schreibzugriffe laufen als `1000:1000`, damit
die setgid-Gruppe `daniel` erhalten bleibt.

---

## 4. Ziel-Dateibaum

```
/home/daniel/QJM/
├── mcp/agent-drawio/                 (NEU)
│   ├── index.ts                      HTTP-Transport + /health + Auth
│   ├── stdio.ts                      lokaler stdio-Transport (Tests/IDE)
│   ├── deno.json                     ohne googleapis
│   ├── Dockerfile                    ohne google_drive.ts
│   ├── README.md                     Kurz-Doku (Tools, ENV, Speicherort)
│   ├── tools/
│   │   ├── drawio.ts                 Tool-Registrierung (Prompts!)
│   │   ├── drawio_generator.ts       unverändert aus openBrain übernommen
│   │   └── storage.ts                NEU: lokaler Store statt google_drive.ts
│   └── tests/
│       ├── call.sh                   CLI-Aufruf (Port 8796)
│       └── e2e.sh                    create→list→read→Traversal-Negativtest
├── dsh_playground/drawio/            (NEU, leer, .gitkeep)
├── AGENTS.md                         (NEU) migrierte Namensregel
└── DRAWIO_MCP_MIGRATION_PLAN.md      dieses Dokument
```

Geänderte Dateien:
`QJM/llm-gateway/docker-compose.yml`, `QJM/restart.sh`, `QJM/.agents/mcp_config.json`,
`QJM/mcp/antigravity_mcp_config.json`, `QJM/agent-presets/trader/agent.cordis.yml`.

---

## 5. Subsystem A — MCP-Server-Code

### A1. `tools/storage.ts` (ersetzt `google_drive.ts`)

Zentrale Regeln:

- Basis aus `DRAWIO_BASE_DIR` (Default `/dsh_playground/drawio`), beim Start `mkdir -p`.
- `slugify(title)`: lowercase, `[^a-z0-9._-]+` → `-`, Mehrfach-`-` kollabieren, auf 120 Zeichen kappen,
  `.drawio` erzwingen.
- `resolveInBase(name)`: `resolve(BASE, name)` und **harten** Prefix-Check (`abs === BASE` ist verboten,
  sonst `abs.startsWith(BASE + "/")`); sonst `Error("Pfad außerhalb des Diagramm-Ordners")`.
- Schreiben atomar: Temp-Datei `<abs>.tmp-<uuid>` + `Deno.renameSync` → keine halben Dateien.
- Lesen/Schreiben nur `.drawio`-Dateien; `Deno.readDirSync` ohne Symlink-Verfolgung.

Skizze:

```ts
import { join, resolve } from "node:path";

export const BASE = resolve(Deno.env.get("DRAWIO_BASE_DIR") ?? "/dsh_playground/drawio");
export function ensureBase() { Deno.mkdirSync(BASE, { recursive: true }); }

export function slugify(title: string): string {
  const s = title.trim().toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/-+/g, "-").replace(/^-|-$/g, "")
    .slice(0, 120) || "diagram";
  return s.endsWith(".drawio") ? s : s + ".drawio";
}

export function resolveInBase(name: string): string {
  const abs = resolve(BASE, name);
  if (abs === BASE || !abs.startsWith(BASE + "/")) throw new Error("Pfad außerhalb des Diagramm-Ordners");
  return abs;
}

export function writeDiagram(name: string, xml: string, overwrite: boolean): string {
  ensureBase();
  const abs = resolveInBase(name);
  if (!overwrite && fileExists(abs)) throw new Error("Datei existiert bereits: " + name);
  const tmp = abs + ".tmp-" + crypto.randomUUID();
  Deno.writeTextFileSync(tmp, xml);
  Deno.renameSync(tmp, abs);
  return abs;
}

export function listDiagrams() { /* name, path, bytes, modified — sortiert nach mtime absteigend */ }
export function readDiagram(name: string): string { return Deno.readTextFileSync(resolveInBase(name)); }
```

### A2. `tools/drawio.ts` — Tool-API (bewusste Breaking Changes)

| Tool | alt | neu |
|---|---|---|
| `create_drawio_diagram` | `title, nodes, edges` → Google Drive | `title, nodes, edges, filename?, overwrite?, return_xml?` → lokale Datei |
| `list_drawio_diagrams` | `{}` → Drive-Liste mit `id`/`drawioAppUrl` | `{}` → `{name, path, bytes, modified}` |
| `read_drawio_diagram` | `fileId` (Drive) | `filename` (relativ zur Basis) |

Antwort von `create` (Text): absoluter Host-Pfad, Dateiname, `file://`-URI, Hinweis „in draw.io öffnen
(app.diagrams.net → Datei öffnen)", optional das XML (`return_xml: true`). **Kein** Google-Link mehr.

`drawio_generator.ts` wird 1:1 übernommen (XML-Erzeugung ist speicherneutral); nur der `agent`-String im
`<mxfile>`-Header kann auf `QJM draw.io MCP` geändert werden.

### A3. Transport, Config, Container

- `index.ts`: wie `mcp/agent-cda/index.ts` — Hono, `/health`, `x-brain-key`/Query-Key-Prüfung,
  `StreamableHTTPTransport`, `PORT` Default `8796`.
- `stdio.ts`: unverändert (nur Import auf `./tools/drawio.ts`).
- `deno.json`: `@hono/mcp`, `@modelcontextprotocol/sdk`, `hono`, `zod` — **`googleapis` entfernen**.
- `Dockerfile`: `google_drive.ts`-COPY entfernen, `tools/` kopieren, `EXPOSE 8796`,
  `CMD ["run", "-A", "index.ts"]`.

**Datenfluss:** Tool-Aufruf → Zod-Validierung → `generateDrawioXml` → `storage.writeDiagram` → Antwort mit
Pfad. Kein Netzwerk außer MCP selbst.

---

## 6. Subsystem B — Betrieb (Docker/Compose)

Neuer Service in `QJM/llm-gateway/docker-compose.yml` (Muster `mcp-cda`):

```yaml
  # ============================================
  # Draw.io MCP Server — Architekturdiagramme (lokal statt Google Drive)
  # ============================================
  mcp-drawio:
    build:
      context: ../mcp/agent-drawio
      dockerfile: Dockerfile
    image: llm-gw-mcp-drawio:latest
    container_name: llm-gw-mcp-drawio
    restart: unless-stopped
    user: "1000:1000"
    volumes:
      - ../dsh_playground/drawio:/dsh_playground/drawio:rw
    environment:
      PORT: "8796"
      MCP_ACCESS_KEY: ${MCP_ACCESS_KEY}
      DRAWIO_BASE_DIR: /dsh_playground/drawio
    ports:
      - "8796:8796"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    networks:
      default:
        aliases:
          - mcp-drawio
          - llm-gw-mcp-drawio
```

`QJM/restart.sh`: `mcp-drawio` in Stop-/Rm-/Build-Listen aufnehmen und die URL-Zeile
`Draw.io MCP Server: http://10.20.0.23:8796` ergänzen.

**Konflikt:** Der alte `openbrain-mcp-drawio` belegt 8796. Vor dem Start des neuen Containers
`docker stop openbrain-mcp-drawio` (danach nicht mehr starten; Entfernen des Compose-Eintrags ist optional/E8).

---

## 7. Subsystem C — Zugang und Registrierung

**C1 DSH (empfohlen, Home-Patch — außerhalb /QJM, Freigabe nötig).**
In `/home/daniel/.dsh/cordis.patch.yml` als weitere Insert-Zeile, exakt im vorhandenen Muster:

```yaml
  - id: mcp-openbrain-drawio
    name: "@deepseek-ai/dsh-mcp-client"
    config:
      serverName: openbrain-drawio
      transport: streamable-http
      url: !!js process.env.OPENBRAIN_DRAWIO_MCP_URL || "http://127.0.0.1:8796"
      headers:
        x-brain-key: !!js process.env.MCP_ACCESS_KEY || "<MCP_ACCESS_KEY>"
```

> **Sandbox-Hinweis:** `~/.dsh/` liegt außerhalb des Workspaces `/home/daniel/QJM`. Diese Änderung
> erfordert eine Schreibfreigabe (Eskalation) oder wird vom Nutzer manuell übernommen. Der Plan liefert den
> exakten Snippet; ohne diesen Schritt bleibt der Server nur per curl/stdio erreichbar, nicht als DSH-Tool.
> Nach der Änderung ist ein **DSH-Host-Neustart** nötig; laufende Sessions sehen das neue Tool erst danach.

**C2 Alternative (vollständig in /QJM).** Denselben Row als Top-Level-Insert im QJM-Preset
`agent-presets/trader/agent.cordis.yml` eintragen. Vorteil: keine Fremdschreibzugriffe. Nachteil: gilt nur für
Sessions mit diesem Preset und muss gegen die Realm-/Preset-Regeln geprüft werden (der Preset-Header weist die
MCP-Clients aktuell dem Home-Patch zu). Nur wählen, wenn C1 nicht freigegeben wird.

**C3 QJM-Altverweise aktualisieren (im Workspace).**

- `QJM/.agents/mcp_config.json`: `openbrain-drawio` → `/home/daniel/QJM/mcp/agent-drawio/stdio.ts`.
- `QJM/mcp/antigravity_mcp_config.json`: dito.

**C4 openBrain-Konsumenten (optional, außerhalb /QJM, Freigabe nötig).** `agent-ea/pta/pca` von falschem
Port 8795 auf 8796 korrigieren bzw. auf den neuen QJM-Server zeigen lassen; danach alten Container/Compose-Eintrag
entfernen. Bis dahin läuft der alte Dienst unverändert.

---

## 8. Subsystem D — Prompts (Kern des Auftrags)

Geändert werden **vier** Prompt-Ebenen plus optional eine Skill.

### D1 Tool-Beschreibungen (das Prompt, das das Modell bei jedem Tool sieht)
In `tools/drawio.ts` alle Google-Drive-Formulierungen ersetzen:

- `create_drawio_diagram`: „Erzeugt ein draw.io-Diagramm (Architektur, Flowchart, Sequenz) und legt es als
  `.drawio`-Datei **lokal im QJM dsh_playground** ab (kein Google Drive). Verwende das Namensschema
  `openbrain-[agent/modul]-[thema]-[version]`; `[thema]` ist genau ein Wort. Nenne dem Nutzer immer den
  vollständigen Pfad."
- `list_drawio_diagrams`: „Listet alle lokalen `.drawio`-Dateien mit Name, Pfad, Größe und Änderungszeit."
- `read_drawio_diagram`: „Liest eine lokale `.drawio`-Datei über ihren Dateinamen (relativ zum Diagramm-Ordner)."
- Parameter: `title` = „Titel und Dateiname-Grundlage", `filename` = „optionaler Dateiname", `overwrite`.

### D2 `QJM/AGENTS.md` (neu) — Namensregel migrieren
Regel und Beispiele aus `openBrain/.agents/AGENTS.md` übernehmen, aber „created on Google Drive" →
„im lokalen `dsh_playground/drawio`". Schema und die Ein-Wort-Regel für `[thema]` bleiben. Optional die
openBrain-Originaldatei mit Freigabe nachziehen.

### D3 EA-Persona (`agent-ea/prompt.txt` → nach QJM migrieren)
Neuer Abschnitt, damit der EA Diagramme proaktiv anbietet:

> **Diagramme (draw.io).** Dir steht `openbrain-drawio` zur Verfügung
> (`create_drawio_diagram`, `list_drawio_diagrams`, `read_drawio_diagram`).
> Erstelle ein Diagramm, wenn der Nutzer Architektur, Ablauf oder Systemzusammenhänge visuell will.
> Dateien liegen lokal unter `dsh_playground/drawio`; nenne immer den vollständigen Pfad.
> Namensschema: `openbrain-[agent/modul]-[thema]-[version]` (`[thema]` = ein Wort).
> Bestätige knapp mit Pfad und Öffnungshinweis.

Zugehörig `agent-ea/config.yaml`: `http://mcp-drawio:8795` → `http://mcp-drawio:8796`.

### D4 Trader-Persona (`QJM/agent-presets/trader/agent.cordis.yml`)
- Neuer Unterabschnitt in §1, z. B. **„F. Architekturdiagramme (`openbrain-drawio`)"**, mit Wann-Nutzen,
  Tool-Reihenfolge (create → list → read) und Namensschema.
- Kopfkommentar „Changes vs. standard" um den Draw.io-Punkt ergänzen.
- Der dort erwähnte `trader-mcp-filter` existiert im Preset **nicht** (nur Kommentar) — entweder Allow-Liste
  `openbrain-drawio` ergänzen, falls der Filter wieder eingeführt wird, oder den veralteten Kommentar bereinigen.

### D5 Optional: Skill für Diagramm-Konventionen
`QJM/agent-presets/trader/skills/drawio/SKILL.md` mit Frontmatter (`name`, `description`) und der Namens-/
Speicher-Konvention. Da `skill-filesystem` + `tool-skill` im Preset aktiv sind, erscheint sie automatisch im
Katalog. Hält D4 schlank und macht die Details nachladbar.

---

## 9. Edge Cases und Fehlerfälle

| Fall | Behandlung |
|---|---|
| `title` leer / Unicode / `/` | `slugify`, Fallback `diagram.drawio` |
| Pfad `../`, absolut, `~/` | `resolveInBase` wirft; `isError: true` |
| Symlink aus dem Ordner | `readDir`/Prefix-Check verhindern Ausbruch |
| Datei existiert | Default Fehler; `overwrite: true` erlaubt Ersetzen |
| Schreibabbruch | Temp-Datei + atomarer Rename |
| Basisordner fehlt | `ensureBase()` legt ihn an |
| Rechte | `user: 1000:1000`; dsh_playground ist setgid `daniel` |
| Port 8796 belegt | alten `openbrain-mcp-drawio` vorher stoppen |
| Kein `MCP_ACCESS_KEY` | Server startet, aber 401; Key aus `QJM/llm-gateway/.env` |
| Google-Creds | werden nirgends mehr gelesen; `googleapis` entfernt |
| DSH-Restart vergessen | Tool fehlt bis Host-Neustart/neuer Session |
| Paralleler Aufruf gleicher Titel | zweiter Aufruf scheitert ohne `overwrite`, sonst Rename-Race-frei |

---

## 10. Tests und Abnahmekriterien

1. **Statisch:** `deno check index.ts stdio.ts tools/*.ts`; `grep -R googleapis mcp/agent-drawio` → leer.
2. **Storage-Unit:** Traversal-Negativfälle (`../x`, `/etc/passwd`, leer) müssen werfen; `slugify`-Tabelle.
3. **stdio-Smoke:** Server starten, `tools/list` zeigt genau die drei Tools.
4. **HTTP/MCP:** `bash mcp/agent-drawio/tests/call.sh tools/list` (analog `agent-wiq/tests/call.sh`, Port 8796, Key aus `~/.dsh/cordis.patch.yml`).
5. **e2e:** `create_drawio_diagram` → Datei existiert unter `/home/daniel/QJM/dsh_playground/drawio/`;
   `list` enthält sie; `read` liefert das XML; Negativtest `read ../../etc/passwd` → `isError`;
   Aufräumen der Testdatei.
6. **Docker:** `docker compose build mcp-drawio && up -d`; `curl /health` = 200; MCP-Handshake mit Key.
7. **DSH:** Host neu starten, neue Session → `mcp__openbrain-drawio__create_drawio_diagram` im Katalog;
   Diagramm aus dem Chat erzeugen und Pfad prüfen.
8. **Regression:** bestehende Server (cco/pta/pca/cda/wiq) bleiben erreichbar; `docker ps` zeigt
   `llm-gw-mcp-drawio` gesund und `openbrain-mcp-drawio` gestoppt.

---

## 11. Rollback

- Code: QJM-Ordner belassen, DSH-Row wieder entfernen; alte QJM-Configs aus Git.
- Betrieb: `docker stop llm-gw-mcp-drawio`, alten `openbrain-mcp-drawio` wieder `start`; Compose-Zeile
  zurücknehmen. Alt-Server liest weiter Google Drive.
- Da die Migration additiv ist (neuer Container + neuer Ordner), ist der Rollback ohne Datenverlust möglich;
  bereits lokal erzeugte Diagramme bleiben erhalten.

---

## 12. Arbeitspakete und Reihenfolge

| WP | Inhalt | Ort | Aufwand |
|---|---|---|---|
| WP1 | `mcp/agent-drawio` anlegen: `storage.ts`, `drawio.ts`, `drawio_generator.ts`, `index.ts`, `stdio.ts`, `deno.json`, `Dockerfile`, `README.md`, `tests/` | /QJM | mittel |
| WP2 | `dsh_playground/drawio/` + `.gitkeep`; lokaler deno-Test | /QJM | klein |
| WP3 | Compose-Service + `restart.sh` | /QJM | klein |
| WP4 | C1 DSH-Home-Patch (Freigabe) und/oder C2 Preset-Insert; C3 Altverweise | teils /QJM | klein |
| WP5 | Prompts D1–D5 | /QJM | mittel |
| WP6 | openBrain-Cutover (optional, Freigabe) | openBrain | klein |
| WP7 | Verifikation §10 | überall | klein |

**Reihenfolge:** WP1+WP2 → WP3 → WP7(1–6) → WP4 → WP5 → WP7(7) → optional WP6.

---


---

## 14. Nachtrag — Nutzerentscheidungen und Umsetzungsstand (2026-09-20)

Entschieden und umgesetzt:

- **Global (E3):** Registrierung als `mcp-openbrain-drawio` in `~/.dsh/cordis.patch.yml` (Home-Patch).
- **Unterordner (E2):** `/home/daniel/QJM/dsh_playground/drawio/`.
- **Google Drive (E7):** vollständig ignoriert; kein Export, keine `googleapis`-Abhängigkeit.
- **Keine EA (D3/D5):** keine EA-Rolle, kein Preset, kein Skill. Draw.io ist ein generisches Zusatzwerkzeug
  ohne Spezialbezug; es gibt **keine** Persona-/AGENTS-Änderungen und **keine** openbrain-Namensschema-Vorgabe.
- **Prompts (D1):** ausschließlich die drei MCP-Tool-Beschreibungen in `tools/drawio.ts`.

Umsetzungsstand:

| WP | Status |
|---|---|
| WP1 Server-Code | erledigt (`mcp/agent-drawio/`), `deno check` grün |
| WP2 Unterordner | erledigt (`dsh_playground/drawio/`) |
| WP3 Compose + restart.sh | erledigt (`mcp-drawio`-Service, Port 8796) |
| WP4 globale DSH-Registrierung | erledigt: `mcp-openbrain-drawio` in `~/.dsh/cordis.patch.yml` eingetragen (Freigabe erteilt) und in dieser Session live verifiziert; Installer bleibt als idempotentes Fallback |
| WP5 Prompts | erledigt (Tool-Beschreibungen); EA/Rolle/Skill bewusst nicht |
| WP6 openBrain-Cutover | alter Container gestoppt; QJM-Configs umgezogen |
| WP7 Verifikation | Build, `/health`, tools/list, e2e, Traversal-Test, Ownership: grün |

Status: Der Eintrag ist aktiv. `mcp__openbrain-drawio__create_drawio_diagram`, `..._list_drawio_diagrams` und `..._read_drawio_diagram` sind erreichbar.


## 13. Offene, nutzereigene Entscheidungen (erledigt, siehe §14)

1. **E3:** DSH-Registrierung über Home-Patch (global, Schreibfreigabe außerhalb /QJM nötig) **oder** nur im
   QJM-Preset (keine Fremdschreibzugriffe, aber presetspezifisch)?
2. **E2:** Unterordner `dsh_playground/drawio/` (Empfehlung) oder direkt `dsh_playground/`?
3. **E7:** Sollen bestehende Google-Drive-Diagramme einmalig exportiert werden (separates Skript, benötigt
   Google-Creds) oder ist ein Neustart des Bestands gewünscht?
4. **D3/D5:** EA als eigene QJM-Rolle/Preset anlegen oder die EA-Persona nur als migrierte Prompt-Datei führen?

Ohne anderslautende Entscheidung setzt die Umsetzung die **Empfehlungen** aus §3 um. Die tatsächlichen
Nutzerentscheidungen vom 2026-09-20 und der Umsetzungsstand stehen im Nachtrag §14.
