# Implementationsplan v4 — Massive.com (ehem. Polygon.io) als Primär-Provider

Status: **Planung abgeschlossen — Starter-Key vorhanden. Nächster Schritt: Implementierung P1–P3.**
Ziel-Paket: **Stocks Starter — aktiv.** Basic ist nicht mehr relevant.

---

## 0. Auftrag & Leitplanken

1. Massive.com-API (Nachfolger von Polygon.io) als **neuen Datenprovider** einbauen.
2. **Alle verfügbaren US-Aktien** herunterladen — **kein Tickerfilter, OTC mit**.
3. So oft wie möglich aktualisieren — **Starter: Full-Market-Snapshot jede Minute**.
4. IBKR und Yahoo Finance bleiben **Fallbacks** (v. a. Non-US-Bestand).
5. Die dynamische **„all"-Watchlist** wächst mit dem Massive-Universum.
6. **Keine Pacing-/Lasttests** — aber Starter hat unlimited Calls, daher unkritisch.

### 0.1 Entscheidungen (final)
- **Starter aktiv.** 5 J. Historie, 15-min delayed, **unlimited Calls**, Snapshot, WebSockets, Minute-Aggregates, Flat Files.
- **Kein Tickerfilter; OTC mit** (`include_otc=true`).
- **Merge statt Ersetzen:** Massive-Fenster übernehmen, **ältere/längere Historie behalten**. §6.
- **Seam: nur loggen** (keine Auto-Rück-Adjustierung). §6.2.
- **IBKR-Backlog nicht auffüllen; bestehende Queue leeren.** §3.3.
- **`cda_master_universe` bekommt `source/provider/type/active/delisted_utc`.** §7.
- Nur noch offen: **Flat Files** als Backfill-Weg ja/nein. §10.

---

## 1. Recherche

### 1.1 Massive.com API (Starter)
- Basis: `https://api.massive.com` (Legacy `api.polygon.io` parallel, gleicher Key).
- Auth: `apiKey=` / Header. Env **`MASSIVE_API_KEY`**. Nie loggen.
- **Starter = unlimited API-Calls** (Limit nur pro nicht gebuchter Asset-Class). 429 praktisch irrelevant, trotzdem defensiv behandeln. 403 nur bei Daten jenseits der 5-Jahres-Historie.

### 1.2 Endpoints
| Zweck | Endpoint | Starter |
|---|---|---|
| Universum-Liste | `/v3/reference/tickers?market=stocks&locale=us&active=true&limit=1000` | ja |
| **Bulk-Tagesbars** | `/v2/aggs/grouped/locale/us/market/stocks/{date}` | ja |
| Einzel-/Intraday-Bars | `/v2/aggs/ticker/{T}/range/{mult}/{timespan}/{from}/{to}` | ja (15m) |
| **Full Market Snapshot** | `/v2/snapshot/locale/us/markets/stocks/tickers` | **ja** |
| WebSockets | `wss://...` | **ja** |
| Marktstatus | `/v1/marketstatus/now` | ja |
| Splits/Dividends | `/v3/reference/splits`, `/dividends` | ja |
| Flat Files (S3) | Tages-/Minuten-Aggregate | ja |

Timestamp-Semantik: Grouped `t` = Fenster-**Ende**, Custom Bars/Snapshot `min.t` = **Start**.

### 1.3 Ist-Zustand
- **stock-data-node** (`/home/daniel/stock-data-node`, Docker `:8002`): Provider-Abstraktion `src/providers/` (`ibkr/`, `yfinance/`), `Downloader` IBKR/YF-Pfade, IBKR-`AdaptiveRateLimiter`, `ParquetWriter` (Sekunden-TS, Dedup „neue gewinnen"), Loops inkl. stündlichem Staleness-Sweep, `_update_master_watchlist()` → `cda_master_universe`. Ist: 5.527 Parquet-Ordner, Queue 5.038.
- **QJM**: MCP-Agenten; PostgREST `127.0.0.1:8001`. `cda_master_universe`: 5.625 / 5.526 `has_parquet=true`, keine Provider-Spalte. `all` = `has_parquet=true`. **`provider_ranking` fehlt live** → Fallback `["IBKR","YFINANCE"]`.

---

## 2. Ziel-Architektur

### 2.1 Prinzip
**Grouped Daily** = 1 Call/Tag für alle; **Snapshot** = 1 Call/Minute für alle; **Merge** hält längere Historien; **Einzel-Calls** nur für Intraday/Hot-Listen/Lücken.

### 2.2 Bausteine (stock-data-node)
~~~~
src/providers/massive/
  __init__.py    # MassiveProvider(BaseProvider)
  client.py      # REST-Client, gzip, Auth, Pagination, Fehlerklassen, request_id
  limiter.py     # Starter: unlimited, aber bounded concurrency + Safety-Margin
  resolver.py    # resolve_ticker
  universe.py    # All-Tickers-Sync (inkl. OTC) + Diff gegen cda_master_universe
  bulk_daily.py  # Grouped-Daily-Backfill (jahr-chunked) → Merge/Write
  merge.py       # Historien-Merge: Massive-Fenster übernehmen, Älteres behalten
  snapshot.py    # Minute-Modus: Full-Market-Snapshot → Today-Bars
  scheduler.py   # Refresh-Loop
src/massive_service.py  # Background-Task
~~~~

### 2.3 Provider-Kette
~~~~
MASSIVE (US, primär) → IBKR (Non-US/Lücken) → YFINANCE (letzter Fallback)
~~~~
`DEFAULT_RANKING → ["MASSIVE","IBKR","YFINANCE"]` + `provider_ranking`-Seed.

### 2.4 Rate/Concurrency (Starter)
- Calls unlimited ⇒ **kein Token-Bucket nötig**.
- **Bounded concurrency** (Default 4, max ~8) + gzip, um API/Netz zu schonen.
- Kein Burst über alle ~10–25k Ticker gleichzeitig.
- Defensive 429-Behandlung trotzdem eingebaut (Pause + Backoff).
- `MASSIVE_ENABLED=false`/`dry_run` nur noch als Startschalter für den ersten Testlauf.

---

## 3. Datenfluss

### 3.1 Backfill (5 J.)
1. `universe.py`: All-Tickers (inkl. OTC) paginieren → Diff → neue Ticker upserten (`has_parquet=false`, `source='massive'`).
2. `bulk_daily.py` **jahr-chunked**: pro Handelstag 1 Grouped-Daily-Call → Bars je Ticker sammeln → **einmal pro Ticker** `merge_and_write`.
3. `sync_master_universe` setzt `has_parquet=true` → **`all` wächst**.
4. Feature-Service-Trigger.

### 3.2 Laufend
- **Jede Minute:** `snapshot.py` → 1 Full-Market-Snapshot-Call, Today-Bars aller Ticker aktualisieren (gebündeltes Flushen).
- **Nach Close:** Grouped Daily als autoritative Tagesbar.
- **Hot-Listen:** optional zusätzlich 15-min-delayed Minutenbars je Ticker (später WebSocket).

### 3.3 Backlog
- Staleness-Sweep enqueued keine Massive-abgedeckten US-Ticker mehr für IBKR.
- Massive-Bulk/Backfill läuft am Downloader/Queue vorbei.
- **Bestehende 5.038 Queue-Einträge werden geleert.**

---

## 4. Komponenten
- `client.py`: `get/paginate`, gzip, Fehlerklassen `AUTH/RATE_LIMIT/PLAN/NOT_FOUND/SERVER/NETWORK`.
- `limiter.py`: Concurrency-Guard + Safety-Margin.
- `resolver.py`: exakter Match ⇒ `ok/resolved/ambiguous/not_found` + Cache.
- `universe.py`: `include_otc=true`, kein Typfilter; Diff + Batch-Upsert; Delisting markieren.
- `bulk_daily.py`: Backfill/Daily; ms→s; Tages-Mitternacht ET; nur abgeschlossene Tage.
- `snapshot.py`: siehe §5.1.
- `scheduler.py`/`massive_service.py`: Status `GET /massive/status`.
- `Downloader`-Branch `provider=="MASSIVE"` + Downgrade IBKR → YF.

---

## 5. Snapshot-Modul & Datenvolumen

### 5.1 Snapshot-Modul
1. Alle `snapshot_every_seconds` (Default 60) **1 Call** Full-Market-Snapshot.
2. Parse in In-Memory-Map „heute" (`day/prevDay/min/updated/todaysChange`).
3. **Persistenz gebündelt** (Default 60–300 s) — nicht 10k Dateien/Minute. Nach Close Grouped Daily als finale Tagesbar.
4. Erweiterungspfad: WebSocket-Minute-Aggregates für Hot-Listen.

### 5.2 Datenvolumen (exakte Messung beim ersten Live-Test)
| Nutzlast | Ticker | unkomprimiert | gzip (~20 %) |
|---|---|---|---|
| Grouped Daily | 10k | ~1,2 MB | ~0,25 MB |
| Grouped Daily | 25k | ~2,8 MB | ~0,6 MB |
| Snapshot | 10k | ~4 MB | ~1 MB |
| Snapshot | 25k | ~9–11 MB | ~2 MB |

**Bandbreite:** Snapshot 1×/min ≈ **0,27 Mbit/s**; 1×/10 s ≈ 1,6 Mbit/s. Backfill 5 J ≈ ~750 MB einmalig. **Jede stabile WLAN-Verbindung reicht.**
**Speicher (nur 1D, 5 J):** ~20–30 KB/Ticker ⇒ 10k ≈ 0,2–0,3 GB; mit OTC ≈ 0,5–0,75 GB.

---

## 6. Historie-Merge & Adjustments

### 6.1 Mechanik
- Coverage `[heute-5J, gestern]`.
- Pro Ticker: bestehende Serie lesen; Massive-Fenster (`adjusted=true`) darüberlegen; **Overlap = Massive**, **ältere Bars bleiben**.
- Batch-Merge (`merge.py`).

### 6.2 Seam: **nur loggen**
- Massive/IBKR split-adjustiert → meist konsistent; YF zusätzlich dividenden-adjustiert → möglicher Sprung.
- **Seam-Check loggt nur** (Verhältnis Pre-Seam-Close / erster Massive-Close). Keine Auto-Korrektur.
- `_assert_same_scale`/Drift-Probe bleiben aktiv.

### 6.3 Intraday
- Bulk nur `1D`. Vorhandene Intraday-Historien bleiben; Auffüllen gezielt/Hot-Listen.

---

## 7. Datenbank-Migrationen (QJM)
- **`011_provider_ranking.sql`**: Tabelle + Seed `MASSIVE=1, IBKR=2, YFINANCE=3`.
- **`012_cda_master_universe_massive.sql`**: `source, provider, type, active, delisted_utc`; Index `has_parquet`/`source`.
- `pca_watchlists` unverändert.

---

## 8. QJM-/MCP-Anpassungen
- `cda.ts`: `SET_PROVIDER` + `"MASSIVE"`; Actions `MASSIVE_STATUS/UNIVERSE_REFRESH/BACKFILL/DAILY_REFRESH/SNAPSHOT/QUEUE_CLEAR`; „5.500+" → dynamisch.
- `agent-pca`: keine Funktionsänderung.
- Optional neues Tool `manage_massive`.

---

## 9. Test-Strategie
- Erster Live-Test **kontrolliert**: 1× Reference, 1× Grouped Daily, 1× Snapshot → exakte Größen/Tickerzahlen.
- Danach Fixtures aus echten Responses (gekürzt) für Unit-Tests; Merge-/Seam-/Parser-Tests.
- Kein Dauer- oder Lasttest nötig (unlimited), aber Concurrency-Guard testen.

---

## 10. Offener Punkt
- **Flat Files** (Starter) als Backfill-Optimierung statt REST Grouped Daily? (Empfehlung: REST zuerst; Flat Files später, falls schnellerer Cold-Backfill nötig.)

---

## 11. Rollout
| Phase | Inhalt | Akzeptanz | Code |
|---|---|---|---|
| **P0** | Key laden, Live-Smoke (Reference/Grouped/Snapshot) | Größen + Tickerzahlen gemessen | nein |
| **P1** | Client/Concurrency-Guard/Resolver/Config | Unit-Tests grün | ja |
| **P2** | Universe-Sync (inkl. OTC) | Diff-Report; Merge in `cda_master_universe` | ja |
| **P3** | Bulk-Backfill + **Merge** | längere Alt-Historie bleibt, Overlap=Massive | ja |
| **P4** | Daily + Snapshot-Scheduler + Queue leeren + IBKR-Sweep stoppen | Snapshot 1/min; `all` wächst | ja |
| **P5** | Downloader-Branch + Fallback-Kette | Massive→IBKR→YF | ja |
| **P6** | DB-Migrationen + MCP-Actions | Ranking MASSIVE zuerst | ja |

---

## 12. Risiken
- Merge-/Seam-Fehler → Seam nur loggen, kein stilles Mischen.
- Queue-Explosion → Bulk am Downloader vorbei, Queue leeren, Sweep stoppen.
- Snapshot-Persistenz-I/O → gebündeltes Flushen.
- OTC-Masse → Metadaten-Kosten; Config-Schalter.
- Docker: `src/` im Image ⇒ Rebuild nötig.

---

## 13. Nächster Schritt
1. Key in `/home/daniel/stock-data-node/.env` ablegen.
2. Live-Smoke (P0) → Messwerte.
3. Live-Smoke ✅ durchgeführt (siehe §14).
4. P1–P3 implementieren.
---

## 14. Live-Messwerte (P0, echter Starter-Key)

Gemessen am 2026-09-17 gegen `api.massive.com`, Auth per `Authorization: Bearer`.

### Universum (`active=true`)
| Markt | Ticker | Seiten |
|---|---|---|
| stocks | **13.203** | 14 × 1000 |
| otc | **17.984** | 18 × 1000 |
| **gesamt inkl. OTC** | **31.187** | 32 |
| ohne OTC | 13.203 | |

Typen (stocks): ETF 5.469 · CS 5.322 · WARRANT 440 · PFD 430 · ADRC 375 · FUND 332 · UNIT 309 · SP 160 · RIGHT 124 · ETS 107 · ETV 90 · ETN 45.
Typen (otc): OS 8.280 · CS 4.539 · ADRC 2.068 · OTHER 1.427 · ETF 719 · PFD 303 · UNIT 259 · WARRANT 194 · GDR 94 · FUND 40 · SP 35 · RIGHT 18 · NYRS 8.

⇒ Aktuelles QJM-Universum 5.625. Massive erweitert auf **~31k inkl. OTC** bzw. **~13k ohne OTC**.

### Payload-Größen (gzip aktiv)
| Endpoint | Treffer | unkomprimiert | gzip |
|---|---|---|---|
| Grouped Daily (2026-09-16, inkl. OTC) | 16.545 | 1,82 MB | **0,55 MB** |
| Full Market Snapshot (inkl. OTC) | 18.269 | 7,46 MB | **2,05 MB** |
| Ticker-Detail (AAPL) | 1 | ~1 KB | – |

Snapshot 1×/min ⇒ **2,05 MB/min ≈ 0,27 Mbit/s**. Backfill 5 J ≈ 1.260 × 0,55 MB ≈ **~700 MB** einmalig. Snapshot-Ø ≈ 408 B/Ticker.

### Felder & Fallstricke
- **Snapshot (Starter):** `ticker, day, prevDay, min, updated, todaysChange, todaysChangePerc`. **`min` (Minutenbar) ist enthalten**, `lastTrade`/`lastQuote` **nicht** (erst Developer+).
- **`count` der Reference-API = Seitengröße**, nicht Gesamtzahl ⇒ **zwingend paginieren**.
- Grouped/Custom-Bars: `o,h,l,c,v,vw,n,t` (`t` = Millisekunden).
- Ticker-Detail enthält u. a. `market_cap`, `weighted_shares_outstanding`, `list_date`, `sic_code`, `type`, `primary_exchange` → für Metadaten nutzbar. **EPS/Revenue sind Advanced-Financials**, nicht enthalten.
- **Zero-Einträge im Snapshot** (`updated: 0`, alle Werte 0) filtern, sonst Null-Bars.
- Metadata-Reconciler für +25k neue Ticker drosseln.

