# Äquivalenzprotokoll — feature-service alt vs. neu

**Datum:** 2026-09-29, 21:27 Uhr · **Phase 4** des Migrationsplans
**Ergebnis:** ✅ **bit-identisch** — 201 Dateien, 9.008.042 Zellen, keine Abweichung,
alle 201 Dateien sogar byte-identisch (gleiche SHA-256).

---

## 1. Fragestellung

Ändert der Legacy-Schnitt (Phase 2: entfernte Endpunkte, entfernte tote Funktionen,
entfernte Legacy-Imports) irgendeinen **berechneten Wert**? Wenn ja, ist die Migration
gescheitert — unabhängig davon, ob der Container startet.

## 2. Vorgehen

Beide Varianten laufen über **dieselben Eingabedaten** und werden Zelle für Zelle
verglichen. Toleranz: **0**.

| Aspekt | Alt (Referenz) | Neu (Ziel) |
|---|---|---|
| Code | `dsh_playground/feature_service_migration/legacy_reference/src` (eingefrorener Schnappschuss) | `services/feature-service/src` |
| Config | `legacy_reference/config` (sha256 `5bff3df7…`) | `services/feature-service/config` (sha256 **identisch**) |
| Image | `openbrain-features-service:latest` (ID `5ee907a26ca9`) | `qjm-feature-service:latest` (ID `7477e2e9f01e`) |
| Python / pandas / numpy / pyarrow | 3.11.16 / 3.0.1 / 2.4.3 / 23.0.1 | **identisch** |
| Orchestrierung | `equivalence/runner.py` → `run_feature_pipeline()` | derselbe Runner |

**Datensatz:** 199 Ticker (deterministische Stichprobe + handverlesene Großwerte,
ETFs, Auslands-Suffixe, Neu-Listings; Liste: `baseline/TICKERS.txt`) in **zwei
identischen Kopien** — je nur `1D.parquet` als Eingang, plus
`$STATS.MARKET_BREADTH/1D.parquet` als Breadth-Historie.

**Laufzeiten:** Alt 8 s (21:27:38–21:27:46), Neu 6 s (21:27:18–21:27:24) für 199 Ticker.

## 3. Ergebnis

```json
{
  "ticker_verglichen": 199,
  "zellen_verglichen": 9008042,
  "status": { "IDENTISCH": 201 },
  "byte_identische_dateien": 201,
  "dateien_gesamt": 201
}
```

Verglichen wurden je Ticker alle Spalten und Zeilen von `1D_features.parquet`
(Schema, Datentypen, Werte inkl. `NaN`-Gleichheit) sowie beide Dateien von
`$STATS.MARKET_BREADTH`.

**Wiederholung:** Nach der letzten Textänderung am ausgelieferten Baum (Docstring in
`market_breadth.py`) wurde der Vergleich **mit dem tatsächlich ausgelieferten Baum**
wiederholt — gleiches Ergebnis: 9.008.042 Zellen, 201/201 Dateien byte-identisch.

**Rohdaten des Vergleichs:** `dsh_playground/feature_service_migration/equivalence/aequivalenz_ergebnis.json`
(je Datei: Zeilen, Spalten, SHA-256 alt/neu, Status).

## 4. Reproduktion

```bash
cd /home/daniel/QJM/dsh_playground/feature_service_migration/equivalence
python3 compare.py          # exit 0 = identisch, exit 1 = Abweichung
```

## 5. Grenzen (ausdrücklich benannt)

1. **Stichprobe, keine Vollerhebung.** 199 von 41.575 Tickern. Der vollständige Lauf
   über das ganze Universum ist Teil des Cutovers und wird dort über den
   `last_run`-Report (0 Fehler) belegt.
2. **Breadth-Werte sind universumsabhängig.** In der Stichprobe wird über 199 Ticker
   gerankt/aggregiert; die absoluten `ibd_rs`-/`breadth_*`-Werte der Stichprobe sind
   deshalb **nicht** mit der Produktion vergleichbar. Der Test prüft die Gleichheit
   **alt gegen neu auf gleichem Universum** — genau das ist die Fragestellung.
3. **Kein Look-ahead-Test.** Ob die Rechenlogik fachlich richtig ist, prüft dieses
   Protokoll nicht; es prüft, dass der Umzug nichts verändert.

## 6. Zusätzlich geprüft

- `python -m compileall src` fehlerfrei.
- `pytest tests -q` → **20 passed** (2 zuvor rote Processor-Tests repariert:
  `_process_single_ticker` braucht seit dem Alt-Refactoring `precomputed_cs`).
- Kein Treffer für `openbrain|nexus|mqtt|postgrest:3000|paho|sklearn` im ausführbaren
  Baum (`src/`, `config/`, `Dockerfile`, `requirements.txt`, `conftest.py`).
- Endpunktliste des neuen `main.py`: `calculate`, `status`, `features/status`,
  `features/schedule` (GET+POST), `health` — sonst nichts.
