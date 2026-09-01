# Implementierungsplan: MCP-Tool "get_timeseries" (OHLCV + Features aus lokaler DB)

> Stand: 2026-06-09 · Scope: Read-only · Kein High-Performance-Anspruch ("schnell mal checken")

## 1. Ziel

Ein neues MCP-Tool im **agent-cda** (openbrain-cda, Port 8795), das einem Agenten
auf Anfrage **OHLCV + Volumen als JSON** für einen Ticker im gewünschten
Zeitbereich liefert. Vorberechnete **Features nur für die jüngste Candle** des
Bereichs, inkl. **Staleness-Hinweis** wenn die Feature-Berechnung der jüngsten
OHLCV-Candle hinterherhinkt.

**Datenquelle:** keine neue. Der bereits laufende **PCA-Service (host.docker.internal:8791)**
hat `GET /api/chartdata`, das OHLCV **und** alle Feature-Spalten (ma_sma_*, bb_20_*,
minervini_*, adr_20, ibd_rs, …) aus lokalen Parquet-Dateien liefert (verifiziert).

## 2. Befund verifizierter Fakten (Stand heute)

| Punkt | Ergebnis |
|---|---|
| stock-data-node (8002) Lese-API | NICHT vorhanden (nur download/status/staleness/provider) |
| PCA-Service (8791) `/api/chartdata` | ✅ liefert `columns` + `data` (OHLCV + 27 Features) |
| `from`/`to`-Parameter | ❌ werden von PCA ignoriert — nur `limit` zählt → **Clientseitig filtern** |
| Timeframes mit Daten | ✅ nur `1D` (1H/1W/5m/1m → status "missing") |
| Features | kommen als Zeilen-Spalten mit; auch bei limit=5 vorhanden |
| Staleness-Erkennung | möglich: letzte Candle mit fehlenden/NaN Feature-Werten = Features nicht aktuell |

## 3. Architektur-Entscheidung

**Tool im `agent-cda` implementieren** (nicht agent-pca):

- `mcp-openbrain-cda` ist **bereits in DSH registriert** (cordis.patch.yml) — Tool erscheint sofort, keine neue Registrierung nötig.
- agent-cda mountet `tools/` als Volume → **Code-Änderung ohne Image-Rebuild**, nur `docker compose restart mcp-cda`.
- PCA-Service ist über `host.docker.internal:8791` erreichbar (agent-cda hat `extra_hosts` bereit).

## 4. Datenfluss

```
Agent → MCP-Tool get_timeseries(ticker, timeframe, from, to, limit, features)
            │
            ▼
   agent-cda (Deno, tools/cda.ts)
            │  GET /api/chartdata?symbol=…&timeframe=…&limit=…&features=true
            ▼
   PCA-Service (8791) → Parquet (lokale DB, Host /home/daniel/stock-data-node/data)
            │
            ▼
   agent-cda: Zeilen nach from/to filtern (clientseitig, Unix-Sekunden)
            │  Features: letzte Zeile mit vollständigen Feature-Werten extrahieren
            ▼
   JSON-Antwort an Agent (siehe §6)
```

## 5. Konkrete Änderungen (Code — erst nach Freigabe)

### 5.1 `mcp/agent-cda/tools/shared.ts` (1 Zeile)

Neue Env-Konstante:
```ts
export const PCA_SERVICE_URL = Deno.env.get("PCA_SERVICE_URL") || "http://host.docker.internal:8791";
```

### 5.2 `mcp/agent-cda/tools/cda.ts` (neu: Tool-Registrierung)

`registerTool("get_timeseries", …)` mit zod-Schema:

| Feld | Typ | Default | Beschreibung |
|---|---|---|---|
| `ticker` | string | — | Ticker-Symbol (großgeschrieben) |
| `timeframe` | string | "1D" | Timeframe (aktuell nur 1D verfügbar) |
| `from` | string\|number | optional | ISO-Datum oder Unix-Sekunden, inklusiv |
| `to` | string\|number | optional | ISO-Datum oder Unix-Sekunden, inklusiv |
| `limit` | number | 300 | max. Zeilen vor Filterung (Cap: 2000) |
| `features` | boolean | true | Features für jüngste Candle mitliefern |

**Logik:**
1. PCA-Request: `GET ${PCA_SERVICE_URL}/api/chartdata?symbol=${T}&timeframe=${TF}&limit=${limit}&features=${features}`
2. HTTP-/`status`-Fehler prüfen → MCP isError
3. columns+data → Objekte; `timestamp` (Unix-Sek.) nach from/to filtern (inklusiv)
4. **Feature-Extraktion:** ab letzter Zeile rückwärts die letzte Zeile mit **keinen** fehlenden/NaN
   Feature-Werten suchen → dieser Satz wird geliefert, mit `features_as_of` = Timestamp
5. **Staleness:** wenn `features_as_of < letzte OHLCV-Candle des Bereichs` → `features_stale:true` + notice
6. Wenn `to` nicht definiert: letzte Candle im Datensatz als Ende verwenden
7. Wenn nach Filterung 0 Zeilen: freundliche Meldung mit verfügbarem Datenzeitraum

### 5.3 `llm-gateway/docker-compose.yml` (optional, 2 Zeilen)

Env `PCA_SERVICE_URL: http://host.docker.internal:8791` zum `mcp-cda` Service ergänzen
(Default greift sonst — nur für explizite Konfiguration).

### 5.4 Kein Rebuild nötig — nur Neustart

```bash
cd /workspace/qjm/llm-gateway && docker compose restart mcp-cda
```

## 6. Antwortformat (JSON an den Agenten)

```json
{
  "ticker": "MSFT",
  "timeframe": "1D",
  "range": { "from": 1787529600, "to": 1787702400, "count": 3 },
  "bars": [
    { "timestamp": 1787529600, "open": 483.18, "high": 490.57, "low": 481.86, "close": 487.31, "volume": 1962904.0 }
  ],
  "features": {
    "as_of": 1787702400,
    "ma_sma_50": 425.32,
    "bb_20_upper": 514.36,
    "minervini_score": 4,
    "minervini_trend_template": false
  },
  "features_stale": false,
  "notice": null
}
```

**Hinweis-Szenarien (notice):**
- "⚠️ Feature-Berechnung nicht aktuell: Features vom <as_of>, letzte Candle <ts> — jüngster verfügbarer Feature-Satz wird mitgeliefert."
- "ℹ️ Nur Timeframe '1D' verfügbar. '<tf>' ist leer — ggf. Download anstoßen (request_priority_download)."
- "⚠️ Keine Daten im Zeitraum <from>–<to>. Verfügbar: <min>–<max> (<n> Candles)."

## 7. Verifikation (nach Umsetzung)

1. `docker compose -p llm-gw logs mcp-cda | tail` — Start ohne Fehler
2. DSH-Session: Tool `mcp__openbrain-cda__get_timeseries` aufrufen mit
   - `ticker=MSFT, timeframe=1D, from=2026-05-01, to=2026-05-29`
   - `ticker=MSFT, limit=5` (Features auf letzter Candle)
   - ungültiger Ticker (Fehlerpfad)
3. Werte mit `/api/chartdata`-Rohantwort abgleichen (Konsistenz)
4. Staleness-Simulation: `to` kurz vor der letzten Feature-Candle setzen

## 8. Bewusst NICHT in Scope (v1)

- ❌ Server-seitige Parquet-Queries (DuckDB/hyparquet) — "schnell mal checken" reicht über PCA-API
- ❌ Automatische Feature-Berechnung anstoßen (separates Tool existiert: manage_feature_calculation)
- ❌ Hochfrequenz-Timeframes (5m/1m) — fehlen in DB; Download ist separater Flow
- ❌ Streaming / Pagination über 2000 Zeilen

## 9. Risiken & Mitigation

| Risiko | Mitigation |
|---|---|
| PCA-Service (8791) temporär down | MCP-Fehler sauber als isError + Retry-Hinweis |
| Timeframe leer (nur 1D vorhanden) | eindeutige Meldung + Verweis auf request_priority_download |
| Großer Zeitbereich > limit | limit-Cap 2000 + Hinweis "nur letzte N Candles" |
| Features in Zeile null statt NaN | Prüfung auf null/undefined/NaN |
| Compose-Neustart braucht Host-Zugriff | restart.sh vorhanden; manuelle Ausführung dokumentiert |

## 10. Geschätzter Aufwand

- `cda.ts` Erweiterung: ~80–120 Zeilen (Tool + Filter + Feature-Extraktion)
- `shared.ts`: 1 Zeile · Compose: optional
- Test & Verifikation: ~15–30 min
