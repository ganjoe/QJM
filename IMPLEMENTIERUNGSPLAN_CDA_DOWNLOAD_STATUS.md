# Implementierungsplan: MCP-Tool für Download-/Chart-Datenbank-Status (agent-cda)

> Stand: 2026-09-03 · Repo: /home/daniel/QJM · Status: **Plan (noch nicht umgesetzt)**

## 1. Ziel

Ein **konsolidiertes MCP-Tool** im wiederhergestellten Agent **`openbrain-cda`** (Container `llm-gw-mcp-cda`), mit dem der Agent (bzw. der User) den Status der **automatischen Chart-Downloads** (OHLCV-Datenbank) prüfen kann:

- Größe der Download-Queue (stock-data-node)
- Datenqualität/Altersverteilung der gesamten Chartdatenbank (Staleness-Report)
- Status einzelner Ticker (Parquet vorhanden? letzte Kerze?)
- Provider-Fallback-Verfügbarkeit (YFinance) und Provider-/Symbol-Mapping

Statt 4–6 einzelner Tools wie im alten `agent-cda` wird alles in **ein** Tool mit `action`-Enum zusammengefasst (Muster wie `manage_feature_calculation` in agent-pca).

## 2. Geprüfte Voraussetzungen / Fakten

### 2.1 Zugriff auf stock-data-node-Quellcode: **NEIN**

- Der Quellcode liegt auf dem Host unter `/home/daniel/stock-data-node` (siehe `llm-gateway/docker-compose.yml`, Service `stock-data-node`).
- In dieser Umgebung ist nur `/home/daniel/QJM` gemountet — das stock-data-node-Verzeichnis ist **nicht vorhanden** (`find / -iname '*stock*data*'` → leer).
- **Kein Docker-Zugriff** (CLI nicht installiert, kein Socket unter `/var/run/docker.sock`) → kein `docker exec` in den Container möglich.
- Verfügungbar ist dagegen die **laufende REST-API** (`http://host.docker.internal:8002`, verifiziert 2026-09-03). Der API-Vertrag wird daher **empirisch** (Probes) + aus Git-Historie abgeleitet; bei Unstimmigkeiten ist der Quellcode auf dem Host zu konsultieren.

### 2.2 agent-cda ist aus Git wiederherstellbar

- Erstellt: `fb6c309` · erweitert: `5a9f56d` · gelöscht: `1c670d7` („replace CDA agent with PCA service").
- Alle Dateien liegen noch im Git-Objektbaum von `1c670d7^`. Der Server-Name war `openbrain-cda`, Port 8795.
- Wiederherstellung: `mcp/agent-cda/` mit `Dockerfile`, `deno.json`, `index.ts`, `tools/shared.ts`, `tools/cda.ts` – letzteres wird **ersetzt** durch das neue konsolidierte Tool.

### 2.3 Verifizierte stock-data-node-API (Empirie, HTTP 200)

| Endpoint | Methode | Antwort (Bsp.) |
|---|---|---|
| `/health` | GET | `{"status":"ok"}` |
| `/status` | GET | `{"queue_size":5026}` (aktuelle Queue-Größe) |
| `/staleness/report` | GET | `{"-1 days":4906,"-0 days":79,"No data":9,…}` (dauert mehrere Sekunden!) |
| `/data/status/{TICKER}` | GET | `{"ticker":"AAPL","folder_exists":true,"timeframes":{"1D":{"has_data":true,"last_candle_date":"2026-09-02 00:00:00 UTC"}}}` |
| `/fallback/check/{TICKER}` | GET | `{"ticker":"AAPL","yf_ticker":"AAPL","yfinance_available":true}` |
| `/mapping/{TICKER}` | GET | `{"ticker":"AAPL","provider":"IBKR","provider_symbols":{},"ibkr_symbol":"AAPL","ibkr_exchange":"NASDAQ","ibkr_currency":"USD"}` |
| `/download` | POST | (GET→405; POST laut alter cda.ts `{"ticker":…}`) |
| `/config/provider/{TICKER}` | POST | (GET→405; POST `{"provider":"IBKR"/"YFINANCE"}`) |

Nicht vorhanden (404): `/queue`, `/queue/status`, `/downloads`, `/tickers`.

## 3. Tool-Design: ein konsolidiertes MCP-Tool

**Tool-Name:** `manage_chart_downloads` · Server: `openbrain-cda` → DSH-Präfix `mcp__openbrain-cda__manage_chart_downloads`

**Input:** `action` (Enum) + optionale Parameter:

| action | API-Call | Beschreibung |
|---|---|---|
| `GET_STATUS` | GET `/status` | Download-Queue-Größe + Health |
| `STALENESS_REPORT` | GET `/staleness/report` | Altersverteilung aller Chartdaten (⚠ langsam, ~5–15 s) |
| `TICKER_STATUS` | GET `/data/status/{ticker}` | Parquet vorhanden? letzte Kerze pro Timeframe |
| `FALLBACK_CHECK` | GET `/fallback/check/{ticker}` | YFinance-Fallback verfügbar? |
| `MAPPING` | GET `/mapping/{ticker}` | Provider, IBKR-Symbol/Exchange/Currency |
| `TRIGGER_DOWNLOAD` | POST `/download` | Ticker(s) mit Priorität in die Queue (optionales Schreibrecht; entspricht altem `request_priority_download`) |

**Ausgabe:** menschenlesbarer deutscher Text (Stil wie pca-Tools: `✅/`⚠️`/❌`, Code-Backticks, Emojis) + `isError` bei HTTP-Fehlern.

**Code-Skizze `mcp/agent-cda/tools/cda.ts` (Kern):**

```ts
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { STOCK_DATA_NODE_URL, log } from "./shared.ts";

export function registerCdaTools(server: McpServer) {
  server.registerTool(
    "manage_chart_downloads",
    {
      title: "Manage Chart Downloads (Queue, Staleness, Ticker Status)",
      description:
        "Konsolidierter Status/Steuerung der automatischen Chart-Downloads (stock-data-node). " +
        "ACTIONS: GET_STATUS (Queue-Größe), STALENESS_REPORT (Altersverteilung aller Daten), " +
        "TICKER_STATUS (Parquet + letzte Kerze), FALLBACK_CHECK (YFinance), MAPPING (Provider/Symbol), " +
        "TRIGGER_DOWNLOAD (Ticker in Priority-Queue).",
      inputSchema: {
        action: z.enum(["GET_STATUS", "STALENESS_REPORT", "TICKER_STATUS", "FALLBACK_CHECK", "MAPPING", "TRIGGER_DOWNLOAD"]),
        tickers: z.array(z.string()).optional().describe("Für TICKER_STATUS/FALLBACK_CHECK/MAPPING/TRIGGER_DOWNLOAD"),
      },
    },
    async ({ action, tickers }: any) => {
      const ticker = (tickers || [])[0]?.toUpperCase();
      try {
        switch (action) {
          case "GET_STATUS": {
            const [s, h] = await Promise.all([
              fetch(`${STOCK_DATA_NODE_URL}/status`).then(r => r.json()),
              fetch(`${STOCK_DATA_NODE_URL}/health`).then(r => r.json()),
            ]);
            return { content: [{ type: "text", text:
              `📥 **Download-Queue (stock-data-node)**
` +
              `**Queue-Größe:** ${s.queue_size.toLocaleString("de-DE")} Ticker` +
              (h.status === "ok" ? " · Dienst: 🟢 ok" : " · Dienst: 🔴") }] };
          }
          case "STALENESS_REPORT": {
            const res = await fetch(`${STOCK_DATA_NODE_URL}/staleness/report`);
            const data = await res.json();
            const lines = ["📊 **Staleness-Report (Chartdatenbank)**"];
            for (const [age, count] of Object.entries(data))
              lines.push(`  • \`${age}\`: ${count.toLocaleString("de-DE")} Ticker`);
            return { content: [{ type: "text", text: lines.join("\n") }] };
          }
          case "TICKER_STATUS": {
            if (!ticker) return { content: [{ type: "text", text: "Error: tickers erforderlich." }], isError: true };
            const data = await (await fetch(`${STOCK_DATA_NODE_URL}/data/status/${ticker}`)).json();
            // → folder_exists, timeframes[].has_data/last_candle_date formatiert ausgeben
          }
          // FALLBACK_CHECK / MAPPING / TRIGGER_DOWNLOAD analog …
        }
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );
}
```

(`TRIGGER_DOWNLOAD` iteriert über mehrere Ticker via POST `/download`, Ergebnisliste wie im alten `request_priority_download`.)

## 4. Betroffene Dateien

### NEU: `mcp/agent-cda/` (vollständig aus Git-Historie `1c670d7^` wiederherstellen)

| Datei | Inhalt |
|---|---|
| `deno.json` | Imports wie gehabt (hono, @hono/mcp, @modelcontextprotocol/sdk, zod) — aus Historie |
| `Dockerfile` | deno:latest, EXPOSE 8795 — aus Historie |
| `index.ts` | MCP-Server `openbrain-cda` v2.0.0, Hono, x-brain-key-Check, Port 8795 — aus Historie |
| `tools/shared.ts` | `STOCK_DATA_NODE_URL` default `http://stock-data-node:8002`, Logger — aus Historie |
| `tools/cda.ts` | **NEU geschrieben**: konsolidiertes `manage_chart_downloads` (siehe §3); `deno.lock` generieren |

> Hinweis: Die Historie enthielt außerdem `mcp/agent-cda/IMPLEMENTIERUNGSPLAN_TIMESERIES.md` — wird **nicht** wiederhergestellt (obsolet, dessen Inhalt lebt in pca/`get_timeseries`).

### MODIFIZIEREN: `llm-gateway/docker-compose.yml`

- DSH-Service-Env (≈ Zeile 76–79): `OPENBRAIN_CDA_MCP_URL=${OPENBRAIN_CDA_MCP_URL:-http://llm-gw-mcp-cda:8795}` ergänzen; `depends_on` + `mcp-cda` (Block nach `mcp-pca`, identisch zur alten Definition aus `1c670d7^`):
  - build: `../mcp/agent-cda`, image `llm-gw-mcp-cda:latest`, container `llm-gw-mcp-cda`
  - volumes: `../mcp/agent-cda/index.ts:/app/index.ts:ro`, `../mcp/agent-cda/tools:/app/tools:ro`
  - env: `MCP_ACCESS_KEY`, `STOCK_DATA_NODE_URL=http://stock-data-node:8002`, `PORT=8795`, `AGENT_ID=cda`
  - ports `8795:8795`, extra_hosts `host.docker.internal:host-gateway`

### MODIFIZIEREN: `llm-gateway/dsh-config/cordis.patch.yml`

- `mcp-openbrain-cda`-Eintrag einfügen (exakt wie in `1c670d7^`): serverName `openbrain-cda`, URL `http://host.docker.internal:8795`, Header `x-brain-key` mit `MCP_ACCESS_KEY`.

### MODIFIZIEREN: `llm-gateway/dsh-config/.agent-presets/trader/agent.cordis.yml`

- Allowlist um `- mcp__openbrain-cda__manage_chart_downloads` ergänzen (bei `mcp__openbrain-pca__…`-Block) und Kommentar „three OpenBrain MCP servers" → „four".

### Bereits konsistent, kein Change nötig

- `llm-gateway/restart.sh` referenziert `llm-gw-mcp-cda` schon (lines 13/14/17 + URL-Echo line 30) — funktioniert erst wieder, wenn der Service im Compose existiert. Damit wird `restart.sh` durch Schritt (1) **repariert** (aktuell bricht `docker compose build … mcp-cda` mit „no such service" ab).

### MODIFIZIEREN (Doku): `AGENTS.md`, `DSH_SYSTEM_PROMPT.md`

- Neuer Abschnitt „Chart-Datenbank & Downloads (agent-cda)" mit `manage_chart_downloads` + Actions; Abgrenzung zu `add_ticker` (pca = Ticker hinzufügen, cda = Status/Steuerung der Downloads).

## 5. Umsetzungsschritte

1. **`mcp/agent-cda/` wiederherstellen** (git show `1c670d7^` : Dateien) und `tools/cda.ts` mit konsolidiertem Tool neu schreiben.
2. **`docker compose build mcp-cda`** (im `llm-gateway/`), dabei `deno.lock` fixieren.
3. **Compose + cordis.patch.yml + trader-Allowlist** wie in §4 ändern.
4. **`docker compose up -d mcp-cda`**, dann **`llm-gw-dsh` neu starten** (MCP-Client lädt cordis.patch → registriert `openbrain-cda` global).
5. **Verifikation:**
   - `curl http://host.docker.internal:8795/health` → `{"status":"healthy","server":"openbrain-cda"}`
   - MCP `tools/list` via `X-Brain-Key` → `manage_chart_downloads` sichtbar
   - Smoke-Test aller 6 Actions gegen die live-API (Queue 5026, Staleness, AAPL-Status…)
   - End-to-End: DSH-Web (diese GUI, Port 3081) nach Neustart → Tool `mcp__openbrain-cda__manage_chart_downloads` im Trader-Preset aufrufbar

## 6. Risiken & offene Punkte

- **Kein Quellcode-Zugriff auf stock-data-node**: API-Vertrag aus Probes abgeleitet. Falls `/staleness/report` unter Last deutlich länger dauert → Timeout im MCP-Handler großzügig (>10 s) und in der Action-Beschreibung vermerken.
- **Redundanz mit pca**: `add_ticker` (pca) nutzt POST `/add`, cda-`TRIGGER_DOWNLOAD` nutzt POST `/download` — bewusst getrennt lassen (add = Auflösung+Eintrag, download = reine Queue). Kein Funktionskonflikt.
- **Abhängigkeit von `MCP_ACCESS_KEY`** im dsh-container: vorhanden (Env & cordis-Patch nutzen denselben Key).
- **Rollback**: `mcp/agent-cda` ist reiner Zusatz; bestehende pca/cco/pta-Server bleiben unverändert. Entfernen = Block & DSH-Restart.
