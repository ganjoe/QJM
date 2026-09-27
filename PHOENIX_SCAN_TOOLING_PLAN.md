# Implementierungsplan — Phoenix-Scan-Tooling

Status: Entwurf · Datum: 2026-09-25 · **Kein Code geändert**

Grundlage: `dsh_playground/phoenix_scan_tooling_report.md` (Retrospektive vom 2026-09-25, 10 Blocker B1–B10, Maßnahmen A/B/C).
Dieser Plan prüft jeden Blocker gegen den echten Code und **gegen eine Nachmessung vom 2026-09-25** und leitet daraus
eine priorisierte Umsetzung ab.

Betroffene Komponenten:

| Komponente | Ort | Deployment |
| :--- | :--- | :--- |
| PCA-Service (FastAPI) | `services/pca-service/` | Container `qjm-pca-service`, Source bind-mounted (`docker-compose.yml:307`), Port 8794→8791 |
| MCP-Agent PCA (Deno) | `mcp/agent-pca/` | Container `llm-gw-mcp-pca`, Tools bind-mounted read-only (`:331`), Port 8790 |
| Feature-Daemon | `/home/daniel/stock-data-features` (**außerhalb dieses Workspace**) | Container, Port 8003 — in diesem Plan **nicht** angefasst |
| DSH-MCP-Config | `~/.dsh/cordis.patch.yml` (**außerhalb dieses Workspace**) | Timeout des Tool-Aufrufs |

---

## 0. Kurzfazit

Der Report beschreibt das Problem richtig, aber zwei Ursachen sind anders als vermutet — und beide sind **kleiner** als gedacht:

1. **Das Universum ist nicht „unvollständig", es ist hart bei 20.000 abgeschnitten.** `watchlists: {"all": 20000}` bei
   **41.723** Tickern mit Parquet-Daten. Ursache ist ein einzelner PostgREST-GET mit `limit=POSTGREST_UNIVERSE_LIMIT` —
   kein Paging. Das ist ein ~20-Zeilen-Fix, keine Architekturfrage.
2. **Der Timeout ist kein Rechenproblem, sondern ein Ladepfad-Problem.** Der Scanner öffnet pro Ticker eine eigene
   DuckDB-Verbindung samt `DESCRIBE` und Full-`SELECT`. Gemessen: **3,68 ms/Ticker** (16 Threads) gegenüber
   **0,96 ms/Ticker** mit PyArrow-Spaltenprojektion — **3,9× schneller**, bei identischer Logik. Das Volluniversum
   fällt damit von ~154 s auf ~40 s, also **unter** den 60-s-Client-Timeout.

Damit gilt: **C1 (`screen_universe`) und C2 (Feature-Store) brauchen wir nicht.** Die im Report als „Zielzustand"
beschriebene Ein-Aufruf-Antwort entsteht durch vier Änderungen an *bestehenden* Endpunkten (P1–P3) plus zwei
Datenpflege-Themen (P4).

---

## 1. Nachmessung: was von den Blockern stimmt

Alle Zahlen vom 2026-09-25 gegen den laufenden Stack (Datenstand Kerzen: 2026-09-24, Master Universe: 44.507 Zeilen).

| # | Blocker laut Report | Befund | Ursache im Code | Beleg |
|--:|:---|:---|:---|:---|
| B1 | Scanner-Timeout, kein Async | **bestätigt** | Kein Job-Modus; Laufzeit > Client-Timeout | 2 Läufe: **71,48 s / 72,07 s** für 16.488 Ticker (16 Worker); DSH-Client-Timeout **60.000 ms** (`packages/mcp/mcp-client/src/index.ts:34`) |
| B2 | Universum-Cap ohne Vorabanzeige | **bestätigt, Ursache präzisiert** | `watchlists_api.fetch_master_universe_tickers` holt **eine** Seite mit `limit=20000` (`:51-57`); identisch in `universal_scanner.fetch_master_universe_metadata` (`:305-310`), `tools/shared.ts:19` und `tools/pca.ts:68-73` | Antwort-Header: `watchlists: {"all": 20000}` → `requested/resolved: 19995` bei **41.723** Tickern mit `has_parquet` (41.718 Parquet-Ordner) |
| B3 | Response-Cap 1.000, kein Export | **bestätigt** | `UNIVERSAL_SCANNER_MAX_LIMIT=1000` (`universal_scanner.py:61`), kein `offset`, kein `columns` | `analysis.ts:602` „Max: 1000" |
| B4 | Keine Sortierung/Projektion im Scanner | **bestätigt** | `run_technical_scanner` kennt nur `limit_hits`, die Antwort enthält immer die volle Tabelle + JSON-Dump | `scanners.py:1557-1595`, `analysis.ts:395-428` |
| B5 | `scan_latest` wird überschrieben | **bestätigt** | `_sync_watchlist(...)` wird an **drei** Stellen unbedingt aufgerufen (`:828, :867, :959`); `sync_scan_watchlist` unterstützt aber bereits `list_name=` (`scan_watchlist.py:185`) | Kein `no_write`, kein Namespace in `UniversalScannerRequest` |
| B6 | `market_cap` fehlt für 73 % | **bestätigt, Ursache widerlegt** | **Nicht FX**: von 5.606 Tickern mit `shares_outstanding` haben nur **22** eine Währung ≠ USD. Das Problem ist Coverage (12,6 %) und stilles `null`. `market_cap` ist **keine** DB-Spalte, sondern wird berechnet | `universal_scanner.py:456-463` (`close × shares`); Counts: 5.606 / 44.507 mit Shares, 31.372 mit Währung |
| B7 | Keine Instrumenttyp-Klassifikation | **bestätigt, Daten existieren** | `cda_master_universe.type` ist gefüllt (CS 9.862 · OS 8.280 · ETF 6.202 · ADRC 2.448 · WARRANT 636 · UNIT 569 · RIGHT 144 · PFD 733 · OTHER 1.427); der Scanner liest die Spalte nur nicht | Mit Parquet: **28.442 typisiert, 13.281 ohne Typ** |
| B8 | RS-Trajektorie nicht nachlesbar | **bestätigt, Daten existieren bereits** | `PhoenixScanner.evaluate` liefert `rs_min`, `rs_min_date`, `rs_min_bars_ago`, `rs_rise` (`scanners.py:762-776`); `include_details: true` gibt sie heraus — **das MCP-Tool bietet den Schalter nicht an** | Live-Test: `detail = {days:20, rs_from:30, rs_to:80, rs_now:95, rs_min:87, rs_min_bars_ago:10, rs_rise:8}` |
| B9 | Provenance fehlt | **bestätigt und verschärft** | 3.507 von 19.995 aufgelösten Tickern wurden **übersprungen** (98 % `insufficient_history`) — und `warnings: []`. Der Nutzer sieht „115 Treffer" ohne Nenner | `skipped` wird nur auf 100 Einträge gekürzt ausgegeben (`:1479-1481`); keine aggregierte Ursachen-Zählung |
| B10 | Token-Volumen | **bestätigt** | Antwort = Markdown-Tabelle + vollständiger JSON-Dump | `analysis.ts:714-722` |
| **neu** | *(nicht im Report)* **Ladepfad ist der Flaschenhals** | — | `load_scan_frame`: neue DuckDB-Verbindung + `DESCRIBE` + Full-`SELECT` **pro Ticker** | Benchmark 1.000 Ticker: **6,76 ms** (1 Thread) / **3,68 ms** (16) vs. PyArrow **1,45 / 0,96 ms** ⇒ Volluniversum 154 s → **40 s** |

**Zusatzbefund:** `115` Phoenix-Treffer auf dem aktuellen Bar (as of 2026-09-24, 16.488 gescannt) — der Report nennt 95
im Zeitfenster 01.08.–21.09. Beides ist konsistent; die Diskrepanz ist der Zeitraum, nicht die Logik.

---

## 2. Zielbild

Die Originalfrage („Phoenix-Scanner für RS/ADR, sortiert nach Market Cap") in **einem** Aufruf:

```jsonc
run_technical_scanner {
  scanners: ["phoenix"],
  watchlists: ["all"],
  from: "2026-08-01", to: "2026-09-21",
  universe: { instrument_types: ["CS","OS","ADRC"], min_dollar_volume_50d: 1000000 },
  sort_by: "market_cap", sort_direction: "desc",
  columns: ["ticker","market_cap","market_cap_source","type","close","adr_20","ibd_rs",
            "rs_min","rs_days_since_low","hit_date"],
  limit: 50,
  auto_watchlist: "scan_phoenix"
}
```

| Metrik | heute | Ziel |
| :--- | ---: | ---: |
| Tool-Aufrufe für die Originalfrage | 18 (4 Fehlschläge) | **1** |
| abgedecktes Universum | 20.000 von 41.723 (48 %) | **41.723 (100 %)**, filterbar |
| Laufzeit Volluniversum | ~154 s → Client-Abbruch bei 60 s | **≤ 40 s** |
| Treffer ohne Marktkap. | 73 % still leer | 0 % still; `market_cap_source: "unknown"` sichtbar |
| Antwortgröße | ~15 k Token | **≤ 1 k Token** (Projektion) |
| Seiteneffekt | `scan_latest` überschrieben | nur auf Wunsch |
| Nicht-Aktien im Ergebnis | 6+ (Warrants, Units, Funds) | **0** bei `instrument_types: [CS,OS,ADRC]` |
| „übersprungen"-Transparenz | keine Warnung | Pflicht-Provenance-Block |

---

## 3. Umsetzung in Phasen

### P0 — Sofort, ohne Code (Betrieb) · 15 min

1. **MCP-Client-Timeout für `openbrain-pca` anheben** (der eigentliche B1-Fix, solange P1 nicht live ist):
   `~/.dsh/cordis.patch.yml`, Block `mcp-openbrain-pca` → `toolCallTimeoutMs: 300000`.
   *Liegt außerhalb des Workspace → braucht deine Freigabe; wirkt erst nach Session-Neustart.*
2. Optional Compose-Env für `pca-service`: `SCANNER_TOTAL_TIMEOUT_S=600` (heute Default 300, `scanners.py:63`).
3. Betriebsregel bis P1/P2: Scan-Ergebnis sofort per `manage_watchlist` (CREATE) in eine eigene Liste sichern;
   Universal-Scans nur mit bewusst gesetztem Filter absetzen.

**Risiko:** keins (reine Config). **Abbruch:** falls der Timeout nicht durchschlägt (Zwischenschichten prüfen) → P1 vorziehen.

### P1 — Ladepfad: 4× schneller · 0,5 Tag · **größter Hebel, keine API-Änderung**

**Datei:** `services/pca-service/scanners.py`

- `load_scan_frame` (`:1035-1098`): DuckDB-Connect + `DESCRIBE` + Full-`SELECT` ersetzen durch
  `pyarrow.parquet.read_table(path, columns=needed)` + Fensterschnitt in pandas. Schema-Cache pro Datei
  (`Pfad → Spalten`, TTL oder mtime). DuckDB bleibt als Fallback hinter `SCANNER_LOAD_BACKEND=arrow|duckdb`.
  Das Muster ist im Haus bereits erprobt (`benchmark_scanner.py` Strategie 4, `universal_scanner.load_ticker_record`).
- `BaseScanner.required_features: Tuple[str, ...] = ()`; `needed = BASE_COLUMNS + ⋃ required_features`.
  `phoenix` → `("ibd_rs",)`. Scanner mit `requires_features=True` ohne Deklaration lesen weiterhin **alle**
  Feature-Spalten (sichere Voreinstellung, kein Verhaltensrisiko).
- Feature-Datei bevorzugen (`1D_features.parquet` enthält OHLCV **und** Features; bestätigt: 28 Spalten, 1 Row-Group).

**Erwartung (gemessen):** 3,68 → 0,96 ms/Ticker ⇒ 41,7 k Ticker in ~40 s statt ~154 s.
**Risiko:** Spaltenprojektion übersieht eine benötigte Spalte → **Equivalenztest Pflicht** (§5).
**Abbruch:** Equivalenz ≠ 100 % → Backend-Schalter auf `duckdb` zurück, Rest bleibt.

### P2 — Universum & Provenance · 1 Tag

1. **Echtes PostgREST-Paging** in `watchlists_api.fetch_master_universe_tickers` (`:43-88`) und
   `universal_scanner.fetch_master_universe_metadata` (`:293-341`): Seiten à `POSTGREST_PAGE_SIZE=5000` bis
   kurze Seite (Muster existiert fertig in `scan_watchlist._fetch_list_rows`, `:281-308`).
   `POSTGREST_UNIVERSE_LIMIT` wird vom stillen Deckel zum **Sicherheitsmaximum** `POSTGREST_UNIVERSE_MAX`
   (Default 100.000); geladene Anzahl wird geloggt und in der Antwort ausgewiesen.
2. `mcp/agent-pca`: `manage_watchlist` LOAD/LIST `'all'` (`tools/pca.ts:68-73`) auf `.range()`-Paging umstellen;
   `tools/shared.ts:19` Default anheben.
3. **Universumsfilter** in `ScannerRunRequest` (`scanners.py:130-173`):
   `universe: { instrument_types, exclude_instrument_types, active_only, require_features, min_dollar_volume_50d, market_cap_min }`.
   Auflösung von `'all'` gegen `cda_master_universe` **mit** Metadaten (gecacht, TTL 300 s) und Filterung
   **vor** dem Cap. Damit ist B7 gelöst und `max_tickers`-Raten entfällt.
4. **Provenance-Block als Pflichtfeld** jeder Scan-Antwort (+ Rendering im MCP-Tool):
   ```jsonc
   "provenance": {
     "parquet_as_of": "2026-09-24", "universe_total": 41723, "universe_resolved": 41718,
     "instrument_types": {"CS": 9641, "OS": 7043, "ADRC": 2038, "ETF": 6076, "...": 0},
     "filtered_out": { "instrument_type": 12405, "adv": 300 },
     "evaluated": 38211, "skipped": { "insufficient_history": 3507, "timeframe_unavailable": 12 },  // Zahlenbeispiel
     "market_cap_source": { "computed": 5605, "unknown": 36000 },
     "truncated": false, "load_backend": "arrow", "scanner_version": "phoenix@1.1"
   }
   ```
   Zusätzlich: **Warnung, wenn `skipped > 0`** — heute `warnings: []` bei 3.507 Übersprungenen.
5. `max_tickers`: Service-Cap `min(..., 20000)` (`:1373`) → `SCANNER_MAX_TICKERS_LIMIT` (Default 60.000);
   MCP-Default 6.000 (`analysis.ts:309`) → „alle aufgelösten".

### P3 — Antwortform: sortieren, projizieren, paginieren, nicht schreiben · 1 Tag

1. `run_technical_scanner` (`scanners.py:1326-1595` + `analysis.ts:276-433`): neue Parameter
   `sort_by`, `sort_direction`, `columns`, `limit`, `offset`, `enrich`, `include_details`.
   - **Anreicherung ohne Zusatz-I/O:** letzte Zeile des ohnehin geladenen Frames (`close`, `adr_20`, `ibd_rs`,
     `ma_sma_50_dollar_volume`, `dcr`/`wcr`) + Phoenix-Trajektorie aus `details` + Marktkap. aus dem
     Metadaten-Cache (`close × shares_outstanding`, inkl. `market_cap_source`).
   - Serverseitige Sortierung über die angereicherten Felder, Spaltenprojektion, `limit/offset` für Treffer
     (Cap aus Env statt hart 1.000). `unknown` sortiert immer ans Ende.
   - MCP-Adapter: generisches Spaltenrendering; **Default-Spalten bleiben wie heute** (rückwärtskompatibel).
2. `run_universal_scanner`: `columns` (DuckDB-Projektion `:913` + PyArrow-Spalten im Loader `:373`) und `offset`;
   `limit`-Cap aus Env.
3. **Schreibschutz/Namespace:**
   - `no_write: true` → `_sync_watchlist` an `:828/:867/:959` überspringen;
   - `auto_watchlist: "<name>"` → `list_name=<name>` an `sync_scan_watchlist` (Unterstützung existiert bereits);
   - optional `SCAN_WATCHLIST_TEMPLATE=scan_{scanner}`; `scan_latest` bleibt Default;
   - Tool-Beschreibungen in `analysis.ts:580-583` und `tools/pca.ts:37-41` korrigieren (sie behaupten „jeder Lauf schreibt").
4. Optional: `run_technical_scanner` schreibt Treffer nach `scan_<scanner>` (gleiche Mechanik) — nur auf Wunsch.

### P4 — Marktkapitalisierung & Instrumente flächendeckend · 1 Tag Code + Backfill-Laufzeit

1. **Coverage statt FX**: `shares_outstanding` fehlt bei 87 % der Ticker.
   `scripts/update_ticker_metadata.py` ist resumable → Backfill über die ~36.000 fehlenden in Batches
   (Rate-Limit; Nicht-US über den Resolver). Ziel: „mit Shares" ≥ 80 % der typisierten CS/OS/ADRC.
2. `market_cap_source ∈ {db, computed, unknown}` + `market_cap_currency`; **kein stilles `null`** mehr.
   Die 22 Nicht-USD-Fälle: `market_cap_usd` **beim Backfill** mitschreiben, nicht zur Scanzeit rechnen.
3. **Datenqualitäts-Gate** im Updater (Report-Randnotiz `BNC` EPS 503,57 / `ELYS` Marktkap. 3.744):
   Plausibilitätsprüfung (`shares > 0`, `|EPS| ≤ Kurs × 10`, `0 < market_cap ≤ 5e12`) →
   `data_quality_flags`-Spalte + Report; betroffene Ticker erscheinen als Scan-Warnung.
4. `type` für die **13.281** Parquet-Ticker ohne Typ nachtragen (Massive-Universum liefert ihn; Rest über
   IBKR/YFinance `quoteType`) — sonst greift der Instrumentfilter nur für 68 % des Universums.

#### P4.2 (Zusatz, klein) — Phoenix-Trajektorie als Computed Field im Universal-Scanner
`rs_min_20d`, `rs_rise_20d`, `rs_days_since_low`, `rs_was_below_30_20d` in `load_ticker_record`
(`universal_scanner.py:358-497`) + `COMPUTED_FIELDS` (`:129-137`). Die letzten 30 Zeilen liegen dort bereits
im Speicher — gemessene Zusatzkosten **+0,04 ms/Ticker**. Damit ist „aktuelle Phoenix-Liste nach Marktkap. sortiert"
**ein** `run_universal_scanner`-Aufruf:
`filters: [{rs_was_below_30_20d == true}, {ibd_rs >= 80}]`, `sort_by: market_cap`, `columns`, `no_write: true`.
Kosten: ~40–60 s über 41,7 k Ticker (mit `columns`-Projektion weniger).
**Grenze:** nur der letzte Bar — für „wer hat zwischen A und B gefeuert" bleibt `run_technical_scanner` zuständig.

### P5 — Optional: Async-Scan-Job · 1,5 Tage — **zurückgestellt**

Mit P1 läuft das Volluniversum in ~40 s und P0 hebt den Client-Timeout: ein Job-Framework wäre dann Komplexität
ohne Nutzen. Falls doch nötig:
`POST /api/scanner/run?async=true` → `{job_id}` (In-Process-Registry + `ThreadPoolExecutor`, Fortschritt aus der
`as_completed`-Schleife `:1423-1426`), `GET /api/scanner/jobs/{id}` (Status) und `.../result?offset&limit&sort_by&columns`;
TTL-Cleanup; MCP-Tool `scan_job` mit STATUS/RESULT/CANCEL. Persistenz optional (`pca_scan_jobs`).
**Kriterium für „doch nötig":** gemessener Volluniversum-Scan > 120 s oder Bedarf an Läufen > 5 min
(z. B. mehrere Scanner gleichzeitig über 41,7 k).

---

## 4. Was wir bewusst **nicht** umsetzen

1. **C2 — Feature-Store-Erweiterung (`rs_min_20d`, `rs_days_since_low`, …).**
   Nicht nötig: die Trajektorie ist zur Scanzeit für **+0,04 ms/Ticker** berechenbar (gemessen) und liegt für
   Phoenix bereits in `details` vor. Ein neuer Feature-Typ erfordert Änderungen in
   `/home/daniel/stock-data-features` (außerhalb dieses Workspace), `config/features.json`, einen kompletten
   Feature-Neulauf über 41,7 k Ticker und eine Chart-Viewer-Registrierung — für **null** Zusatzinformation.
2. **C1 — `screen_universe` als neuer Mega-Endpunkt.** Die Wirkung entsteht durch P1–P3 an den bestehenden
   Endpunkten. Ein Parallel-Endpunkt wäre eine zweite Wartungsfläche auf denselben Datenpfaden.
3. **B5-Ringpuffer `scan_latest[0..4]`.** Namespace pro Scanner (P3.3) löst die Kollision; ein Ringpuffer erzeugt
   nur neue Mehrdeutigkeit („welcher der letzten fünf?").
4. **Universum-Cap-„Binärsuche" (A1) als Dauerlösung.** Nach P2 ist `max_tickers` kein Betriebsgeheimnis mehr,
   sondern eine Zahl im Provenance-Block.

---

## 5. Test- und Verifikationsplan

**Bestehende Suiten** (im Container, brauchen `/parquet`):
```bash
docker exec qjm-pca-service python3 /app/test_universal_scanner.py
docker exec qjm-pca-service python3 /app/test_stoch_scanners.py
```

**Neue Tests:**
1. **Paging**: gefälschter PostgREST-Client mit 3 Seiten (Summe > 20.000) ⇒ vollständige, duplikatfreie Liste.
2. **Provenance**: `requested/resolved/scanned/skipped_by_reason` vorhanden; Warnung bei `skipped > 0`.
3. **Universumsfilter**: WARRANT/UNIT/RIGHT fliegen raus, ADV-Filter greift, leeres Ergebnis ⇒ klare Meldung
   statt „0 Treffer".
4. **Sortierung/Projektion**: `sort_by=market_cap&desc` monoton fallend mit `unknown` am Ende;
   `columns` exakt eingehalten.
5. **`no_write`**: kein DELETE/INSERT gegen `pca_watchlists` (httpx-Mock); `auto_watchlist="scan_phoenix"` schreibt dorthin.
6. **Equivalenz Ladepfad (P1, Pflicht)**: für 500 zufällige Ticker müssen `matched`/`score`/`hits`
   Arrow- vs. DuckDB-Backend identisch sein — für `phoenix`, `minervini_trend`, `dcr`, `ww_bdoh_2`.
7. **Trajektorie (P4.2)**: `rs_min_20d`/`rs_days_since_low` gegen `PhoenixScanner.evaluate` als Referenz.

**Regression vor/nach P1:**
Volluniversum-Lauf gegen die gespeicherte Baseline `dsh_playground/_measure_phoenix_universe.json`
(19.995 aufgelöst, 16.488 gescannt, **115 Treffer**, as_of 2026-09-24) — Treffermenge muss identisch bleiben.

**Abnahmemessung je Phase:** Laufzeit · gescannte Ticker · Treffer · Antwortgröße (Token) ·
Side-Effect-Check (`scan_latest` unverändert bei `no_write`).

**Deployment der Änderungen:**
```bash
docker restart qjm-pca-service          # Python, Source ist bind-mounted
./restart.sh mcp-pca                    # Deno, kein Hot-Reload (Build + Recreate)
curl -s http://localhost:8794/health
```

---

## 6. Reihenfolge & Aufwand

| Phase | Inhalt | Aufwand | Wirkung | Abhängigkeit |
| :--- | :--- | ---: | :--- | :--- |
| **P0** | MCP-Timeout + Betriebsregel | 15 min | Scans bis 300 s laufen durch | DSH-Config (Freigabe) |
| **P1** | Ladepfad PyArrow | 0,5 d | **4×** schneller, 154 s → 40 s | — |
| **P2** | Paging + Universumsfilter + Provenance | 1 d | 41,7 k statt 20 k, B7/B9 gelöst | P1 sinnvoll vorher |
| **P3** | `sort_by`/`columns`/`limit`/`no_write` | 1 d | **18 Aufrufe → 1**, B3/B4/B5/B10 gelöst | P2 (Metadaten) |
| **P4** | Marktkap.-Coverage, `type`, Qualitäts-Gate | 1 d + Backfill | B6/B7 vollständig | — |
| **P4.2** | Trajektorie im Universal-Scanner | 0,25 d | Ein-Aufruf für den aktuellen Bar | P3 (`columns`) |
| **P5** | Async-Job | 1,5 d | nur falls Messung es fordert | P1-Messung |

**Empfehlung zum Start:** **P1 + P2.1** (klein, messbar, ohne API-Änderung, keine Rückwärtskompatibilität betroffen),
danach **P3**. Nach P0+P1+P2+P3 ist die Originalfrage ein Aufruf, ~40 s, 100 % Universum, sortiert und projiziert.

---

## 7. Offene Entscheidungen

1. **DSH-MCP-Timeout anheben?** Änderung in `~/.dsh/cordis.patch.yml` liegt außerhalb dieses Workspace
   (Freigabe/Approval nötig, wirkt nach Session-Neustart).
2. **`scan_latest`-Semantik:** Default beibehalten oder generell auf `scan_<scanner>` umstellen?
   (Breaking Change für bestehende Workflows und Chart-Viewer-Verknüpfungen.)
3. **Market-Cap-Backfill** über ~36.000 Ticker jetzt starten (yfinance, Rate-Limit, Laufzeit) oder erst nach P1–P3?
4. **Default-Universum:** Nicht-Aktien (ETF/WARRANT/UNIT/PFD/OS) drin lassen oder standardmäßig auf `CS/OS/ADRC` filtern?
5. **P5 (Async):** jetzt bauen oder erst nach der P1-Messung entscheiden?

---

## Anhang A — Messprotokoll 2026-09-25

```bash
# Volluniversum-Scan (2 Läufe, identisches Ergebnis)
curl -s -X POST http://localhost:8794/api/scanner/run -H 'Content-Type: application/json' \
  -d '{"scanners":["phoenix"],"watchlists":["all"],"max_tickers":20000,"limit_hits":5}'
# -> universe: {requested: 19995, resolved: 19995, scanned: 16488, watchlists: {all: 20000}}
# -> timing: {elapsed_seconds: 71.36, workers: 16} / 71.95
# -> true_count: {phoenix: 115}, warnings: []

# Ladepfad-Benchmark (im Container, 1.000 Ticker)
docker exec -i qjm-pca-service python3 - <<'PY'
# duckdb per-ticker (IST) : 6.76 ms 1 Thread / 3.68 ms 16 Threads -> 41,7k: 154 s
# pyarrow 7 Spalten (SOLL): 1.45 ms 1 Thread / 0.96 ms 16 Threads -> 41,7k:  40 s
PY

# Coverage cda_master_universe
#  44.507 Ticker · 41.723 has_parquet · 5.606 shares_outstanding · 31.372 currency
#  type: CS 9.862 · OS 8.280 · ETF 6.202 · ADRC 2.448 · OTHER 1.427 · PFD 733
#        WARRANT 636 · UNIT 569 · FUND 372 · SP 195 · RIGHT 144 · NULL 13.295
```

**Artefakte dieser Messung** (im Playground, unverändert nutzbar als Baseline):
`dsh_playground/_measure_phoenix_universe.json` (115 Treffer + Details),
`dsh_playground/_measure_phoenix_timing.json` (Laufzeitnachweis).
