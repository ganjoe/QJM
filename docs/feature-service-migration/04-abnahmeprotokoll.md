# Abnahmeprotokoll — Cutover `feature-service` (QJM)

**Datum:** 2026-09-29, 21:27–21:5x · **Phasen 4–6** des Migrationsplans
**Ergebnis:** bestanden. Der Dienst läuft im `llm-gateway`-Stack, der Alt-Container ist gestoppt.

---

## 1. Ablauf (gemessene Zeiten)

| Zeit | Schritt | Ergebnis |
|---|---|---|
| 21:27:18–21:27:46 | Äquivalenzläufe alt/neu auf Zwillingsdaten | beide fertig, 199 Ticker |
| 21:27:5x | Zellvergleich | **201 Dateien / 9.008.042 Zellen identisch** ([03-aequivalenzprotokoll.md](03-aequivalenzprotokoll.md)) |
| 21:28:02 | `docker stop openbrain-features-service` | `Exited (0)`, Port 8003 frei |
| 21:28:02 | `docker compose up -d feature-service` | `qjm-feature-service` **healthy** |
| 21:28:35 | Scheduler lädt Config aus Supabase | `enabled: true`, `INTERVAL 60 min`, nächster Lauf `19:50:03Z` |
| 21:28:4x | `POST /features/calculate` (Trigger-Pfad von `stock-data-node`) | **HTTP 202** |
| 21:33:07 | Voller Lauf fertig | **41.576 Ticker, 0 Fehler**, 255,44 s, 35.436.519 Datenpunkte |
| 21:35 | QJM-Gegenprobe `get_timeseries(AAPL)` | 24 Feature-Spalten, `features_stale: false`, `lag_bars: 0` |
| 21:36–21:38 | **Rollback-Probe** | Alt-Dienst übernimmt (health ok), danach neuer Dienst wieder healthy |
| 21:5x | 60-Minuten-Zeitplan feuert von selbst | (siehe §3) |

**Laufzeitvergleich:** Alt 259,18 s / 41.575 Ticker (21:04) ↔ Neu 255,44 s / 41.576 Ticker —
gleiche Größenordnung, keine Regression.

## 2. Abnahmekriterien

| ID | Kriterium | Status | Nachweis |
|---|---|---|---|
| A1 | Dienst läuft im QJM-Stack, Endpunkte formgleich | ✅ | `docker ps` → `qjm-feature-service (healthy)`; `/health`, `/features/status`, `/features/schedule` antworten wie der Alt-Dienst |
| A2 | Feature-Spalten über QJM-Tools sichtbar | ✅ | `get_timeseries(AAPL)` → 24 Spalten inkl. `breadth_40_pct`, `breadth_200_pct`, `rs_adr_neutral` |
| A3 | Werte bit-identisch (Stichprobe) | ✅ | 9.008.042 Zellen, 201/201 Dateien byte-identisch |
| A4 | `$STATS.MARKET_BREADTH` identisch | ✅ | beide Dateien im Äquivalenztest enthalten |
| A5 | Zeitplan aktiv | ✅ | aus Supabase geladen; Selbstauslösung siehe §3 |
| A6 | Trigger aus `stock-data-node` erreicht den Dienst | ✅ | `POST http://localhost:8003/features/calculate` → 202 und Lauf gestartet |
| A7 | Kein Legacy-Treffer im ausführbaren Baum | ✅ | `grep` über `src/`, `config/`, `Dockerfile`, `requirements.txt`, `conftest.py` → 0 Treffer |
| A8 | Voller Lauf ohne Fehler | ✅ | 41.576 / 41.576 OK, 0 Fehler |
| A9 | Rollback dokumentiert und geprobt | ✅ | §4 |

## 3. Zeitplan-Selbstauslösung

Der eingebaute 60-Minuten-Takt hat **von selbst** gefeuert — der Beweis, dass der
Scheduler im neuen Dienst inklusive Supabase-Lese- **und** Schreibpfad funktioniert:

| Feld | Wert |
|---|---|
| Auslösung | `2026-09-29T19:50:14Z` (21:50:14 CEST) — exakt der aus der DB geladene Termin |
| Dauer | 253,69 s |
| Ticker | **41.577, 0 Fehler** |
| Datenpunkte | 35.436.567 |
| Danach in Supabase geschrieben | `last_run_at = 19:50:14Z`, `next_run_at = 20:50:14Z` (in der Live-Antwort sichtbar) |

Damit ist auch der Schreibpfad (`POST`-Äquivalent nach `system_settings`) belegt, der
im manuell ausgelösten Lauf nicht berührt wird.

## 4. Rollback (geprobt am 21:36)

```bash
docker stop qjm-feature-service
docker start openbrain-features-service     # Alt-Image 5ee907a26ca9 bleibt getaggt
# ... Kontrolle: /health und /features/status antworten, danach zurück:
docker stop openbrain-features-service
cd /home/daniel/QJM/llm-gateway && docker compose up -d feature-service
```

Beide Richtungen funktionierten im Test. Der Rollback berührt **keine Daten**:
beide Dienste schreiben dasselbe Format in dieselben Pfade.

## 5. Änderungen am QJM-Repo

| Datei | Änderung |
|---|---|
| `services/feature-service/` | **neu** (aus dem eingefrorenen Alt-Schnappschuss, Legacy-Schnitt) |
| `llm-gateway/docker-compose.yml` | neuer Service `feature-service` (Port 8003, Mounts, `SUPABASE_URL`) |
| `docs/feature-service-migration/03-aequivalenzprotokoll.md` | **neu** |
| `docs/feature-service-migration/04-abnahmeprotokoll.md` | **neu** (dieses Dokument) |

Analyseartefakte (nicht Produktion): `dsh_playground/feature_service_migration/` mit
`legacy_reference/` (eingefrorener Alt-Code), `baseline/` (API- und Parquet-Baseline),
`equivalence/` (Zwillingsdaten, Vergleichsskript, Ergebnis-JSON).

## 6. Phase 6 — Alt-Stack abgeklemmt ✅

| Schritt | Ergebnis |
|---|---|
| Alt-Container | `openbrain-features-service` → `Exited (0)`, bleibt als Rollback-Netz stehen |
| Alt-Eintrag im openBrain-Compose | **entfernt** am 29.09. 22:04 (22 Zeilen, Zeile 591–612) |
| Backup der Compose-Datei | `/home/daniel/openBrain/docker-compose.yml.bak-feature-migration-20260929-220420` |
| Validierung | `docker compose config --quiet` → OK; `docker compose config --services` listet **kein** `features-service` mehr |
| Restsuche | kein Treffer für `features-service` mehr in `openBrain` (außer Alt-Baum und Backup) |

Damit existiert **kein Wiederbelebungspfad** mehr: Ein `docker compose up` im openBrain-Projekt
kann den Alt-Dienst nicht mehr starten. Nur ein direktes `docker start openbrain-features-service`
(Rollback) oder ein Rebuild aus dem Alt-Baum würde ihn zurückholen.

### Reste, die liegen bleiben dürfen (toter Code, nicht kritisch)

- **Alt-Container/-Image:** erst löschen, wenn der neue Dienst ein paar Tage stabil lief;
  `openbrain-features-service:latest` (ID `5ee907a26ca9`) ist das Rollback-Netz.
- **Alt-Baum** `/home/daniel/openBrain/features-service/` (inkl. `scanners/`, `cluster.py`,
  `mqtt_publisher.py`, `schemas.py`, `test_ffill.py`) — im README als Friedhof dokumentiert.
- **Doppelgänger** `/home/daniel/stock-data-features/` — alter 1:1-Klon, läuft nirgends.
