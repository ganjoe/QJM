# Implementierungsplan — `feature-service` im QJM-Stack

**Erstellt:** 21.09.2026
**Gehört zu:** [`01-migrationsplan.md`](01-migrationsplan.md) (Entscheidungen E1–E7, Legacy-Liste L1–L12)
**Charakter:** Ausführbarer Schritt-für-Schritt-Plan. Reihenfolge ist verbindlich.
**Regel:** Nach jeder Phase wird das jeweilige Abnahmekriterium geprüft, **bevor** die
nächste beginnt. Kein Phase-5-Cutover ohne bestandene Phase 4.

---

## Vorbedingungen (vor Phase 0)

| # | Bedingung | Prüfung |
|---|---|---|
| V1 | Aktueller Lauf ist beendet, kein Batch aktiv | `manage_feature_calculation: GET_STATUS` → „Bereit (Leerlauf)" |
| V2 | Schedule-Status bekannt | dito, Feld `schedule` (heute: `INTERVAL`, 60 min, aktiv) |
| V3 | Backups vorhanden | `dsh_playground/rs_benchmark/backup_features_service_20260918-235022/` |
| V4 | Alt-Image-ID notiert (Rollback) | `docker images openbrain-features-service` → `e2958c77442a` |
| V5 | Offene Punkte O1–O4 beantwortet | `01-migrationsplan.md` §7 |

---

## Phase 0 — Freeze und Beweis (keine Änderung am Ziel)

**Ziel:** Den auszuführenden Code als Artefakt sichern und die Abweichung
Host-Baum ↔ Container **feststellen, nicht glätten** (O1).

**Schritte**

1. **Laufenden Stand aus dem Container ziehen** (Quelle der Wahrheit):
   ```bash
   docker cp openbrain-features-service:/app/src \
     /home/daniel/QJM/dsh_playground/feature_service_migration/container_src
   ```
2. **Host-Baum danebenlegen** und vergleichen:
   ```bash
   cp -r /home/daniel/openBrain/features-service/src \
     /home/daniel/QJM/dsh_playground/feature_service_migration/host_src
   diff -r .../container_src .../host_src | tee .../DIFF_host_vs_container.txt
   ```
3. **Hashtable** über alle `.py`-Dateien beider Bäume erzeugen (`sha256sum`).
4. **Feature-Baseline ziehen** (vor jeder Änderung):
   - `1D_features.parquet` von 200 Tickern nach
     `dsh_playground/feature_service_migration/baseline_parquet/` kopieren
     (breite Streuung: große/liquide, kleine, Auslands-Suffixe, Neu-Listings, `$STATS.MARKET_BREADTH`).
   - Zusätzlich `$STATS.MARKET_BREADTH/{1D.parquet,1D_features.parquet}` vollständig.
5. **API-Baseline protokollieren:** je ein Response von
   `GET /features/status`, `GET /features/schedule`, `POST /features/calculate`
   (Antwortkörper, Feldnamen, HTTP-Codes) als JSON ablegen.

**Abnahme Phase 0**

- [ ] `container_src/` liegt im Playground, Hashtable erstellt.
- [ ] `DIFF_host_vs_container.txt` existiert — Inhalt ist **dokumentiert**
      (erwartet: keine oder erklärte Abweichungen).
- [ ] 200-Ticker-Baseline + `$STATS.MARKET_BREADTH` gesichert.
- [ ] API-Baseline (3 Responses) gesichert.

---

## Phase 1 — Gerüst und Kopie

**Ziel:** Vollständiger, importierbarer Zielbaum unter `services/feature-service/`.

**Schritte**

1. Verzeichnis anlegen: `services/feature-service/{src,config,tests/unit}`.
2. Aus **Phase-0-Artefakt** kopieren: `calculator.py`, `processor.py`,
   `fast_rank.py`, `parquet_io.py`, `market_breadth.py`, `job_manager.py`,
   `scheduler.py`, `config_parser.py`, `logging_setup.py`, `schemas.py`, `main.py`.
3. `config/features.json` und `config/settings.json` aus
   `/home/daniel/openBrain/features-service/config/` übernehmen — **inhaltlich unverändert**
   (Fachparameter; `min_dollar_volume_50` etc. werden **nicht** angefasst, s. §6).
4. `Dockerfile` an `services/pca-service/Dockerfile` anlehnen:
   `python:3.12-slim`, `WORKDIR /app`, Requirements-Install, `COPY . .`,
   `EXPOSE 8003`, `CMD ["python", "src/main.py"]`,
   Healthcheck auf `/health`.
5. `requirements.txt` übernehmen und **bereinigen**: `paho-mqtt` (kein MQTT mehr),
   `scikit-learn` (nur Clustering) entfernen. `pytest` bleibt.
6. `.dockerignore` anlegen (`__pycache__`, `*.pyc`, `logs/`, `data/`).

**Abnahme Phase 1**

- [ ] Zielbaum vollständig; `python -m compileall services/feature-service/src` fehlerfrei.
- [ ] `requirements.txt` enthält weder `paho-mqtt` noch `scikit-learn`.
- [ ] Keine Datei aus der Nicht-Portieren-Liste (`scanners/`, `cluster.py`,
      `mqtt_publisher.py`, `src/test_ffill.py`) vorhanden.

---

## Phase 2 — Legacy-Entkopplung

**Ziel:** Der Baum enthält keinen einzigen Bezug auf `openbrain`-Infrastruktur.

**Schritte**

1. **`main.py` verschlanken** auf: `/features/status`, `/features/calculate`,
   `/features/schedule` (GET+POST), `/health`, `/status` (Alias, Kompatibilität).
   Entfernen (L9): `/features/ma`, `/features/rs`, `/features/cluster`, `/scanners/run`
   samt zugehöriger Imports (`cluster`, `scanners.*`, `MARequest`/`RSRequest`/
   `ClusterRequest`/`ScannerRequest`), sowie den ungenutzten Import `re` (L10).
2. **`scheduler.py` entkoppeln:** `_get_postgrest_url()` → `SUPABASE_URL`
   (Default `http://host.docker.internal:8001`), Header-Aufbau unverändert.
   Kein `POSTGREST_URL` mehr im Baum (L4).
3. **`processor.py`:** `from mqtt_publisher import publish_features_complete` und
   den Publish-Block am Priority-Ticker entfernen (L1). Der Priority-Pfad selbst
   („Ticker zuerst verarbeiten") **bleibt** — er wird von QJM genutzt.
   Docstring entsprechend korrigieren.
4. **`market_breadth.py`:** Docstring-Referenz „for openBrain features-service"
   auf den QJM-Kontext umschreiben.
5. **`calculator.py`:** tote RS-Dubletten entfernen (L11) — **nur** die nachweislich
   ungenutzten Varianten; `compute_normalized_roc` (vom Pipeline-Pfad gebraucht) bleibt.
6. **`schemas.py`:** auf die von den verbleibenden Endpunkten benötigten Modelle kürzen.
7. **Testdateien** aus `tests/` übernehmen, die zu den verbleibenden Modulen passen
   (`test_calculator`, `test_config_parser`, `test_parquet_io`, `test_processor`,
   `test_job_manager`). Nicht übernehmen: `test_scanners`, `test_cluster`.
   Importpfade auf die Zielstruktur anpassen.
8. **README.md** schreiben: Zweck, Endpunkte, Datenvertrag
   (`<parquet>/<TICKER>/1D_features.parquet`), Config-Ort, Betriebs-Kommandos,
   sowie explizit die Liste der **bewusst nicht** portierten Legacy-Bestandteile.

**Abnahme Phase 2**

- [ ] **A7:** Kein Treffer über den ganzen Baum für:
      `openbrain|openBrain|nexus|mqtt|MQTT|postgrest:3000|/home/daniel/openBrain`
- [ ] `python -m compileall` fehlerfrei; `pytest services/feature-service/tests` grün.
- [ ] Kein Endpunkt mehr, der beim Import auf ein entferntes Modul zugreift.
- [ ] README nennt Legacy-Abgrenzung und Datenvertrag.

---

## Phase 3 — Stack-Integration

**Ziel:** Der Dienst läuft im `llm-gateway`-Stack wie jeder andere QJM-Dienst.

**Schritte**

1. **Compose-Eintrag** in `llm-gateway/docker-compose.yml` ergänzen — Muster von
   `pca-service` (E1/E2/E7):

   ```yaml
   feature-service:
     build:
       context: ../services/feature-service
       dockerfile: Dockerfile
     image: qjm-feature-service:latest
     container_name: qjm-feature-service
     restart: unless-stopped
     environment:
       - APP_BASE_DIR=/app
       - FEATURES_API_PORT=8003
       - SUPABASE_URL=${SUPABASE_URL:-http://host.docker.internal:8001}
       - SUPABASE_SERVICE_ROLE_KEY=${SUPABASE_SERVICE_ROLE_KEY}
       - OMP_NUM_THREADS=1
       - MKL_NUM_THREADS=1
       - NUMEXPR_NUM_THREADS=1
     volumes:
       # Code hot-editierbar wie pca-service
       - ../services/feature-service/src:/app/src
       - ../services/feature-service/config:/app/config
       # Datenvertrag: lesen 1D.parquet, schreiben 1D_features.parquet
       - /home/daniel/stock-data-node/data:/app/data
       - ../services/feature-service/logs:/app/logs
     ports:
       - "8003:8003"
     extra_hosts:
       - "host.docker.internal:host-gateway"
     networks:
       default:
         aliases:
           - feature-service
   ```

2. **`logs/`-Verzeichnis** im Repo mit `.gitkeep` anlegen (kein Fremdpfad mehr).
3. **Konfiguration syntaktisch prüfen:**
   `docker compose -f llm-gateway/docker-compose.yml config > /dev/null`.
4. **`mcp-pca`-Umgebungsvariable** belassen wie sie ist
   (`FEATURES_SERVICE_URL: http://host.docker.internal:8003`) — sie zeigt bereits
   korrekt auf den publizierten Port (E7).
5. **`stock-data-node`-Trigger** verifizieren, nicht annehmen: In dessen Compose-Eintrag
   existiert **kein** `FEATURE_SERVICE_URL`-Env, der Default ist
   `http://localhost:8003/features/calculate`. Da `stock-data-node` selbst im
   Host-Netz läuft, trifft das das Port-Mapping. → In Phase 5 **explizit testen**
   (R6), nicht vorher umkonfigurieren.

**Abnahme Phase 3**

- [ ] `docker compose config` fehlerfrei.
- [ ] Der neue Eintrag enthält **keine** Referenz auf `openbrain`, `nexus-broker`,
      `network_mode: host`, `postgrest:3000`.
- [ ] Container startet (`docker compose up -d feature-service`) und `/health` → `{"status":"ok"}`.
      *Hinweis: In dieser Phase bewusst **ohne** Port 8003 parallel zum Alt-Dienst —
      falls der Port belegt ist, Phase 3 mit `-p 8004:8003` als Smoke-Test fahren.*

---

## Phase 4 — Offline-Äquivalenz (der Prüfstein)

**Ziel:** Beweisen, dass die Rechenergebnisse **bit-identisch** sind — bevor
irgendetwas umgeschaltet wird. Kein Cutover ohne diese Phase (R1).

**Vorgehen: auf einer Kopie, nicht auf den Produktivdaten.**

1. **Datenkopie anlegen:** `1D.parquet` der 200 Baseline-Ticker nach
   `.../equivalence/data/parquet/` kopieren (nur `1D.parquet`, **keine** `_features`).
2. **Neuen Code gegen die Kopie laufen lassen** (`APP_BASE_DIR` auf die Kopie zeigen).
3. **Vergleich** Alt-Baseline ↔ Neu-Ergebnis:
   - alle 21 Spalten, alle Zeilen, alle 200 Ticker — Toleranz **0** für `ibd_rs`,
     `minervini_score`, `minervini_trend_template`, `breadth_*`;
     für Gleitkomma-Spalten (`bb_*`, `ma_*`, `adr_20`) Toleranz `1e-12` (bit-identisch erwartet).
   - `$STATS.MARKET_BREADTH`: beide Dateien (`1D.parquet`, `1D_features.parquet`) vollständig.
4. **Abweichungen einzeln erklären.** Eine unerklärte Abweichung ist ein
   **Abbruchkriterium**, kein Schönheitsfehler.
5. **Ergebnis protokollieren:** `docs/feature-service-migration/03-aequivalenzprotokoll.md`
   mit Tickerliste, Spaltenmatrix, Abweichungen, Erklärungen.

**Abnahme Phase 4**

- [ ] A3: 21 Spalten über 200 Ticker ohne unerklärte Abweichung.
- [ ] A4: `$STATS.MARKET_BREADTH` identisch.
- [ ] Protokoll geschrieben und mit Zahlen belegt (**keine** Prosa-Behauptung).
- [ ] Explizit benannt: Dies ist eine **Stichprobe**, keine vollständige
      Rückwärtsrechnung über alle 41.500 Ticker (R7).

---

## Phase 5 — Cutover (kurzes Fenster)

**Ziel:** Umschalten in einem Fenster, ohne je zwei Writer auf demselben Parquet-Baum zu haben.

**Schritte (in dieser Reihenfolge)**

1. **Lauf abwarten:** `manage_feature_calculation: GET_STATUS` muss „Bereit (Leerlauf)" zeigen.
2. **Alt-Dienst stoppen** (gibt Port 8003 frei):
   `docker stop openbrain-features-service`
3. **Neuen Dienst starten:**
   `cd /home/daniel/QJM/llm-gateway && docker compose up -d feature-service`
4. **Erreichbarkeit prüfen:** `/health`, `/features/status`, `/features/schedule`.
5. **Schedule übernehmen/prüfen** — erwartet `INTERVAL` / 60 min / enabled.
   Nach dem Umzug über `manage_feature_calculation: GET_SCHEDULE` und
   `: SET_SCHEDULE` gegenprüfen, dass der Takt **nicht** verloren ging.
6. **Vollen Lauf auslösen:** `manage_feature_calculation: TRIGGER`.
   Erwartung: 41.500+ Ticker, **0 Fehler**, Laufzeit in der Größenordnung der Baseline
   (heute ~1 m 46 s; Sandbox-/CPU-Abweichungen sind zu protokollieren).
7. **A6 — Trigger aus `stock-data-node` testen** (nicht annehmen):
   einen Download anstoßen bzw. `POST http://localhost:8003/features/calculate`
   aus dem Host-Kontext absetzen und im Log des neuen Dienstes den Eingang belegen.
8. **A2 — Live-Gegenprobe in QJM** (nun liest PCA die vom neuen Writer erzeugten Dateien):
   - `get_timeseries("AAPL", limit: 5, features: true)` → alle 21 Spalten vorhanden, Werte
     unauffällig.
   - `get_timeseries("$STATS.MARKET_BREADTH", limit: 5)` → Prozentzahl und `days_back` plausibel.
   - `run_technical_scanner(scanners: ["minervini_trend"], tickers: ["NVDA"])` → läuft (liest `1D_features.parquet`).

**Abnahme Phase 5**

- [ ] A1: Neuer Dienst healthy im QJM-Stack, Antworten formgleich zur Phase-0-Baseline.
- [ ] A2: `get_timeseries` liefert die 21 Spalten; Breadth plausibel.
- [ ] A5: Zeitleiste zeigt den 60-Minuten-Takt, nächster Lauf gesetzt.
- [ ] A6: Trigger aus `stock-data-node` belegt.
- [ ] Voller Lauf: 0 Fehler (A8).

**Rollback (jederzeit in Phase 5)**

```bash
docker stop qjm-feature-service
docker start openbrain-features-service     # Image e2958c77442a bleibt getaggt
```
Danach `manage_feature_calculation: GET_STATUS` zur Kontrolle. Rollback berührt
**keine** Daten: beide Writer erzeugen dieselben Dateien im selben Format.

---

## Phase 6 — Alt-Stack abklemmen

**Ziel:** Den Legacy-Weg dauerhaft schließen (Auftrag: „keinen Legacy-Code mitschleppen").

**Schritte**

1. **`features-service`-Eintrag** aus `/home/daniel/openBrain/docker-compose.yml`
   entfernen (Host-Eingriff **außerhalb** der Agenten-Sandbox → manuell oder mit Freigabe).
2. **Alt-Container und Alt-Image entfernen**, nachdem ein Lauf im neuen Dienst
   erfolgreich war: `docker rm openbrain-features-service`,
   `docker rmi openbrain-features-service:latest` (Rollback-Image `e2958c77442a`
   erst löschen, wenn Phase 5 über einen Tag stabil ist).
3. **Veraltete Datei kennzeichnen:** `features-service/docker-compose.yml` im Altpfad
   als verworfen markieren (nicht ins QJM-Repo portieren!).
4. **Aufräumen im Playground:** `dsh_playground/rs_benchmark/patches/`,
   `container_src/`, `_drift/` sind Analyseartefakte — als solche kennzeichnen,
   damit sie nicht später für Produktionsquellen gehalten werden (R4).
5. **Kein Eintrag bleibt doppelt:** `grep -rn "8003" llm-gateway/docker-compose.yml`
   → genau **ein** Treffer (der neue Service) plus der `FEATURES_SERVICE_URL`-Eintrag in `mcp-pca`.

**Abnahme Phase 6**

- [ ] `openbrain`-Stack enthält keinen Feature-Service mehr.
- [ ] Genau ein Startmechanismus für den Dienst existiert.
- [ ] Analyseartefakte sind als solche gekennzeichnet.

---

## Phase 7 — Doku und Übergabe

1. `services/feature-service/README.md` finalisieren (aus Phase 2).
2. `docs/feature-service-migration/03-aequivalenzprotokoll.md` und
   `04-abnahmeprotokoll.md` (Ergebnisse Phase 5, inkl. gemessener Laufzeit) ablegen.
3. **Rollback einmal proben** (Stop neu → Start alt → Stop alt → Start neu) und
   die Probe dokumentieren. Ein ungeprüftes Rollback ist kein Rollback.
4. **Betriebshinweise** ergänzen: Schedule-Ort (`system_settings.features_schedule_config`),
   Log-Ort, wie ein Lauf manuell gestartet wird, wo die Config liegt.

**Abnahme Phase 7**

- [ ] README vollständig; Rollback-Probe dokumentiert.
- [ ] Abnahmeprotokoll mit **gemessenen** Zahlen (keine Schätzungen).

---

## Abnahmekriterien (Gesamtübersicht)

| ID | Kriterium | Phase | Nachweis |
|---|---|---|---|
| A1 | Dienst läuft im QJM-Stack, Endpunkte formgleich | 3/5 | `docker ps`, curl |
| A2 | 21 Feature-Spalten über QJM-Tools sichtbar und plausibel | 5 | `get_timeseries` |
| A3 | Feature-Werte stichprobenartig bit-identisch | 4 | Äquivalenzprotokoll |
| A4 | `$STATS.MARKET_BREADTH` identisch | 4 | dito |
| A5 | 60-Minuten-Schedule aktiv, nächster Lauf gesetzt | 5 | `GET_SCHEDULE` |
| A6 | `stock-data-node`-Trigger erreicht den neuen Dienst | 5 | Dienst-Log |
| A7 | Kein `openbrain`/`nexus`/`mqtt`/`postgrest:3000` im Zielbaum | 2 | `grep -rn` |
| A8 | Voller Lauf ohne Fehler | 5 | `GET_STATUS` → 0 Fehler |
| A9 | Rollback dokumentiert **und** geprobt | 7 | Probe-Protokoll |

---

## Risiko-Gegenmaßnahmen (Kurzform)

| Risiko | Gegenmaßnahme im Plan |
|---|---|
| R1 Werte-Drift | Phase 4 als harter Blocker vor Phase 5 |
| R2 zwei Writer | E6: Cutover in einem Fenster, Schritt 2 vor Schritt 3 |
| R3 Sandbox beim Kopieren | Quelle per `docker cp` (Phase 0), Ziel im Workspace |
| R4 alte Compose-Datei | nicht portieren + Kennzeichnung (Phase 6) |
| R5 Port belegt | Smoke-Test auf 8004 in Phase 3; Alt-Container zuerst stoppen in Phase 5 |
| R6 Trigger ins Leere | expliziter Test A6 statt Annahme |
| R7 Stichprobe statt Vollerhebung | im Äquivalenzprotokoll ausdrücklich benannt |

---

## Was dieser Plan bewusst **nicht** tut

- Keine fachlichen Änderungen an den Features (Gates, Inflator, Bars-Kriterium).
- Kein Inkrementalmodus, keine Umstellung auf Event-Trigger.
- Kein Umzug der `1D_features.parquet`-Dateien.
- Kein Zusammenlegen der Minervini-Implementierungen.
- Keine Analysewerkzeuge aus `dsh_playground/` als Produktionscode.

Diese Punkte stehen in `01-migrationsplan.md` §6 mit Begründung.
