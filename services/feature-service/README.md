# feature-service (QJM)

Berechnet die technischen Kennzahlen für den gesamten Aktien-Universumsbestand und
schreibt sie als Spalten in die Parquet-Dateien, aus denen `pca-service`, `mcp-pca`
und der Chart-Viewer lesen.

**Herkunft:** migriert aus `/home/daniel/openBrain/features-service` (Alt-Container
`openbrain-features-service`, Port 8003). Die Migration ist in
[docs/feature-service-migration/](../../docs/feature-service-migration/) dokumentiert;
das Wert-Gleichheitsprotokoll liegt in
[03-aequivalenzprotokoll.md](../../docs/feature-service-migration/03-aequivalenzprotokoll.md).

---

## 1. Was der Dienst tut

| Phase | Inhalt |
|---|---|
| **Pass 0** | liest alle Ticker, berechnet die *cross-sectional* Rohwerte (IBD-RS, RS-ADR-neutral, Breadth-Aggregate) und rankt sie über das Universum |
| **Pass 1** | rechnet je Ticker die lokalen Features (MAs, Bollinger, Stochastik, ADR, Dollar-Volume, Minervini) und schreibt `<TICKER>/1D_features.parquet` |
| **Breadth** | schreibt danach `$STATS.MARKET_BREADTH/{1D,1D_features}.parquet` |

Parallelisierung: `ProcessPoolExecutor` mit `processing_threads` aus
[config/settings.json](config/settings.json) (aktuell 16). Kein Inkrementalmodus —
jeder Lauf ist ein voller Lauf über alle Ticker.

## 2. Endpunkte

| Methode | Pfad | Zweck | Aufrufer |
|---|---|---|---|
| `POST` | `/features/calculate[?priority=<TICKER>][&stream=true]` | vollen Lauf starten; `202` gestartet, `409` läuft schon | `stock-data-node` (nach Downloads), `mcp-pca` (`manage_feature_calculation: TRIGGER`) |
| `GET` | `/features/status` (Alias `/status`) | Live-Fortschritt, letzter Lauf, Zeitplan | `mcp-pca`: `GET_STATUS` |
| `GET` | `/features/schedule` | Zeitplan lesen | `mcp-pca`: `GET_SCHEDULE` |
| `POST` | `/features/schedule` | Zeitplan setzen (persistiert in Supabase) | `mcp-pca`: `SET_SCHEDULE` |
| `GET` | `/health` | Docker-Healthcheck | Compose |

Port: **8003** (bewusst identisch zum Alt-Dienst, damit der Trigger aus
`stock-data-node` unverändert bleibt).

## 3. Datenvertrag

Geschrieben wird **in den Bestandsbaum** `/home/daniel/stock-data-node/data/parquet`
(im Container `/app/data/parquet`):

```
<TICKER>/1D_features.parquet      # Features je Ticker
$STATS.MARKET_BREADTH/1D_features.parquet
```

Spalten (live gemessen, AAPL): `timestamp, open, high, low, close, volume` +
**24 Feature-Spalten**:

`ibd_rs` · `rs_adr_neutral` · `breadth_minervini` · `breadth_minervini_pct` ·
`breadth_40_pct` · `breadth_200_pct` · `ma_sma_10/20/50/100/150/200` ·
`stock_10_1_k` · `stock_10_1_d` · `bb_20_avg/upper/lower/bandwidth` · `daily_range` ·
`dollar_volume` · `ma_sma_50_dollar_volume` · `adr_20` · `minervini_score` ·
`minervini_trend_template`

Gelesen wird dieser Vertrag in `pca-service` an 12 Stellen (`scanners.py`,
`universal_scanner.py`, `chart_data.py`, `indicators.py`, `benchmark_scanner.py`) —
**diese Dateien sind der Vertrag**, nicht die API.

Die Feature-Definitionen liegen in [config/features.json](config/features.json)
(Fachparameter, unverändert aus dem Alt-Dienst übernommen). Neue Features = neue
Zeile dort + Rechenweg in [src/calculator.py](src/calculator.py).

## 4. Betrieb

```bash
# Status / Health
curl -s http://localhost:8003/features/status | python3 -m json.tool
curl -s http://localhost:8003/health

# Lauf starten (identisch zum Trigger aus stock-data-node)
curl -X POST http://localhost:8003/features/calculate

# Zeitplan (persistiert in Supabase: system_settings.features_schedule_config)
curl -s http://localhost:8003/features/schedule

# Logs
docker logs -f qjm-feature-service

# Stack-Befehle (aus llm-gateway/)
docker compose build feature-service && docker compose up -d feature-service
```

`src/` und `config/` sind im Container **bind-gemountet** — Code-Änderungen wirken
nach `docker restart qjm-feature-service`; ein Image-Rebuild ist nur für die
Auslieferung nötig.

## 5. Tests

```bash
cd services/feature-service && python3 -m pytest tests -q
```

20 Tests (Rechenkern, Config-Parser, Parquet-IO, Processor, Job-Manager, Streaming).
Zwei Processor-Tests waren im Alt-Repo rot (Signatur `_process_single_ticker` ohne
`precomputed_cs`) und wurden bei der Migration repariert.

## 6. Bewusst NICHT migriert (Legacy-Friedhof)

| Entfernt | Grund |
|---|---|
| Endpunkte `/features/ma`, `/features/rs`, `/features/minervini`, `/features/cluster`, `/scanners/run` | kein Aufrufer in QJM; die einzigen Aufrufer lagen in gestoppten openBrain-Containern. QJM bedient das über `get_timeseries` / `calculate_indicator` / `run_technical_scanner` |
| `src/scanners/` (MADBO) | fachlich dupliziert im PCA-Scanner-Framework (`services/pca-service/scanners.py`) |
| `src/cluster.py` | schrieb `cluster_*`-Watchlists, die niemand liest |
| MQTT-Publish (`src/mqtt_publisher.py`) | in QJM gibt es keinen Broker-Empfänger (der Alt-Pfad war ein No-op) |
| `src/schemas.py` | nur von den entfernten Endpunkten gebraucht |
| `compute_normalized_roc`, `calculate_minervini_on_the_fly` (calculator) | nur von `/features/rs` bzw. `/features/minervini` aufgerufen |
| Abhängigkeiten `paho-mqtt`, `scikit-learn`, `aiofiles` | kein Nutzer mehr im Baum |
| `test_ffill.py`, `repro_issue.py`, `run_features_scratch.py`, `test_logging_repro.py` | Wegwerf-Dateien des Alt-Repos |

**Nicht** entfernt (schlafend, aber getestet): `_calc_ema` / `_calc_stubs` in
[src/calculator.py](src/calculator.py) — die Config deklariert aktuell kein EMA-Feature,
der Rechenweg bleibt für spätere Feature-Zeilen erhalten.

## 7. Netzwerk- und Umgebungsvariablen

| Variable | Bedeutung |
|---|---|
| `APP_BASE_DIR` | Basisverzeichnis (`/app`); daraus `config/`, `data/parquet/`, `logs/` |
| `FEATURES_API_PORT` | Port des HTTP-Servers (Default 8003) |
| `SUPABASE_URL` | Basis-URL des PostgREST (`http://host.docker.internal:8001`); der Dienst hängt `/rest/v1` selbst an |
| `SUPABASE_SERVICE_ROLE_KEY` | Schreibzugriff auf `system_settings` (nur für den Zeitplan) |

Der Dienst läuft im Bridge-Netz des `llm-gateway`-Stacks (kein `network_mode: host`)
und erreicht den Host über `host.docker.internal`.
