# Permanentes Feature: `rs_adr_neutral`

## Definition
- **Komponenten:** ROC (close/close[w]-1) über **20 / 40 / 80 Handelstage**, Gewichte **1-1-1**.
- **Ranking:** Cross-Section-Perzentil **1–99** je Datum, aber **innerhalb von 3 ADR20-Terzilen**
  (ADR-neutral) statt global.
- **Universum:** alle Ticker des Feature-Service mit Buchstaben-Prefix; `$STATS.*` und
  Ticker mit Nicht-Buchstaben-Prefix (z. B. `093370`) werden ausgeschlossen.
- **Gates:** wie `ibd_rs` (Preis > 0,5; 50d-Dollarvolumen ≥ 100k; Integritäts-/Scale-Jump-Gate).

## Implementierung (nativ im Feature-Service)
- **Service:** `/home/daniel/openBrain/features-service`
  - `src/config_parser.py`: neuer `FeatureType.RS_ADR_NEUTRAL`
  - `src/calculator.py`: `_calc_rs_adr_neutral_raw` (ROC 20/40/80 + Gates) und `_adr_series` (ADR20)
  - `src/fast_rank.py`: `rank_adr_neutral` (Perzentil innerhalb ADR-Terzile)
  - `src/processor.py`: neuer Typ im cross-sectional Pass 0 + ADR-Hilfsspalte + Ausschluss im Pass 1
  - `config/features.json`: Eintrag `rs_adr_neutral` (periods [20,40,80], weights [1,1,1], adr_window 20, buckets 3)
- **Schreibt** die Spalte direkt in `<TICKER>/1D_features.parquet` – zusammen mit allen anderen Features,
  wird also **automatisch bei jedem Feature-Lauf** neu erzeugt (Default: stündlich).
- **Chart-API:** `chart_data.py` liefert die Spalte nativ mit (keine Sidecar-Datei mehr).
- **Registry/Presets:** `pca_features.canonical_id='rs_adr_neutral'` (Anzeige "RS neutral (ADR 20/40/80)",
  Farbe `#82B1FF`); in allen Presets in **Pane `rs`** direkt neben `ibd_rs`.

## Update des Feature-Service
- Quelle patchen (siehe `dsh_playground/features_patch/apply_patch.py`), dann
  `DOCKER_BUILDKIT=0 docker build -t openbrain-features-service /home/daniel/openBrain/features-service`
  und `docker compose -f /home/daniel/openBrain/docker-compose.yml up -d --no-build features-service`,
  danach `POST /features/calculate`.

## Grenzen
- Das Ranking-Universum ist das des Feature-Service (nicht die Highcap-1000). Werte weichen daher von
  der früheren Highcap-1000-Version ab.
