# rs_lab — isoliertes Testlab für ein alternatives RS-Rating (ROC-basiert)

**Kein Produktionscode.** Alles unter `rs_lab/`, alle DB-Objekte mit `lab_`-Präfix.
`rs_lab/` löschen + `teardown.sh` entfernt den gesamten Versuch.

## Pipeline

| Schritt | Skript | Ergebnis |
| :--- | :--- | :--- |
| 1 | `01_build_highcap.py` | Top-1000 nach Market Cap (USD) → `cache/highcap1000.parquet` + Watchlist `lab_highcap1000` |
| 2 | `02_lab_rs.py` | 20 ROC-Varianten (breit), cross-sectional 1..99 |
| 3 | `03_backtest.py` | Phoenix-Trigger × 36 Parameter, 63d-Excess vs. SPY → `output/backtest_results.csv` |
| 4 | `04_optimize.py` | Aggregation, Pareto, Heatmaps → `output/rs_lab_report.html` |
| 5 | `05_display_lab_rs.py TICKER` | RS-Schar im Chart Viewer (Pane `lab_rs`, RAM-only) |
| 6 | `06_fine_walkforward.py` | Feinsuche (63/126/252) um `to=90` + **Walk-Forward über 6 Jahre** |
| — | `verify_backtest.py`, `diagnose_trades.py` | Trades gegen Rohdaten / Verteilung prüfen |
| — | `teardown.sh` | alle `lab_*`-Watchlists löschen |

    ./rs_lab/run_lab.sh 01_build_highcap.py
    ./rs_lab/run_lab.sh 02_lab_rs.py && ./rs_lab/run_lab.sh 03_backtest.py && ./rs_lab/run_lab.sh 04_optimize.py
    ./rs_lab/run_lab.sh 06_fine_walkforward.py
    ./rs_lab/run_lab.sh 05_display_lab_rs.py CRM --bars 400

## Definition
- ROC-Komponenten (close/close[w]−1) gewichten → **Cross-Section-Perzentil 1..99 in der Watchlist**.
- **Datenqualitäts-Gate** (wie Produktion): close > 0,5 · 50d-Dollarvolumen ≥ 100k · kein Scale-Jump.
- Backtest: Entry = Open **Folgetag**, Exit = Close nach **63 Bars**, Benchmark **SPY** (Total Return).

## Befunde A — 2-Jahres-Fenster 2024-09..2026-09 (wie zuvor)
Bestätigt die früheren Zahlen: beste ROC-Varianten ~0,54–0,57 Hit, Ø +10…+16 %; Spitzen-Config
`roc_63_126_252_w112` d40 30→90 mit 0,699 Hit (n=113). Treiber ist `rs_to≥90`.

## Befunde B — Feinsuche (Fenster 2021–2026) um `to=90`
Top-Configs (n≥100): `roc_63_126_189_252_w1111` d10 40→90 (Hit 0,610), `roc_63_126_252_w123`
d40 20→95 (0,586), `roc_63_126_252_w113` d20 40→95 (0,559). Gewichte (1,1,2)/(1,2,2)/(1,1,3)
und `to∈{90,95}` dominieren. Das sind **Gesamt**-Werte über 6 Jahre.

## Befunde C — Walk-Forward (der wichtige Teil)

**Fold-Stabilität der Top-Configs (Hit je Jahr):**

| Reihe | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `roc_63_126_189_252_w1111` d10 40→90 | 0,11 | 0,41 | **0,73** | **0,73** | **0,71** | 1,00 |
| `roc_63_126_252_w123` d40 20→95 | 0,40 | 0,44 | 0,58 | 0,61 | 1,00 | 0,75 |
| `roc_63_126_252_w113` d20 40→95 | 0,27 | 0,40 | 0,50 | **0,79** | 1,00 | 1,00 |

→ **Klar regimeabhängig:** 2021/2022 (Bärenmarkt/Chop) verlieren, 2023–2026 stark.

**Walk-Forward-Regeln (Auswahl auf Vorjahren, OOS 2022–2026):**

| Regel | gehandelte Jahre | n | Hit | Ø Excess |
| :--- | ---: | ---: | ---: | ---: |
| immer handeln (naiv) | 5 | 1577 | **0,462** | +16,0 % |
| expanding, train_n≥100 & train_hit≥0,50 | 3 | 81 | 0,568 | +28,8 % |
| expanding, train_n≥50 & train_hit≥0,50 | 3 | 35 | 0,714 | +39,3 % |
| expanding, train_n≥300 & train_hit≥0,50 | 1 | 17 | 0,765 | +39,0 % |

**Fixed-Config-OOS (Auswahl 2021–2023, Test 2024–2026):**

| Auswahl | Config | OOS n | OOS Hit | OOS Ø Excess |
| :--- | :--- | ---: | ---: | ---: |
| bestes feines ROC | `roc_63_126_252_w122` d40 30→95 | 87 | 0,609 | +37,6 % |
| Produktions-`ibd_rs` | `ibd_rs_prod` d40 40→90 | 575 | 0,593 | +14,7 % |

## Interpretation
1. **Der Effekt ist real, aber regimeabhängig.** In trendenden Bullenphasen (2023–2026) liegt die
   Hit-Rate bei 0,6–1,0; 2021/2022 fällt sie auf 0,0–0,55. Mechanisches Handeln über alle Jahre
   ergibt OOS ~0,46.
2. **Ein Regime-Gate ist Pflicht:** nur handeln, wenn die jüngste/rollierende Hit-Rate > 50 % ist.
   Damit steigt die OOS-Hit auf 0,57–0,76 (allerdings kleinere n).
3. **Lange Fenster (63/126/252), Gewichte (1,1,2)/(1,2,2)/(1,1,3) und `to∈{90,95}`** sind robust.
4. **Kein Beleg, dass das MA-basierte RS das ROC schlägt** – ROC bleibt besser.
5. Die Edge ist rechtsschief (Median +5…15 %, 5 %-Quantil −33 %).

## Einschränkungen
Survivorship-Bias (heutige Top-1000 rückwärts), SPY = Total Return, keine Kosten, OTC-Daten nur
grob gefiltert, 2026 nur 9 Monate.

## Nächste Experimente
- Regime-Gate systematisch: rollierende 6-Monats-Hit-Rate, SPX über/unter SMA200, Breadth.
- Point-in-Time-Marktkapitalisierung; Full-Universe-Ranking vs. Highcap.
- ETFs vs. Stocks getrennt; Sektor-/Themen-Cluster.

## Befunde D — MADBO + Phoenix (Chart-Suche, ab 2023, 63d Excess vs. SPY)

MADBO hier identisch zur Produktion (MA-Fächer komprimiert < ATR(1) UND Dollarvolumen
> 2x 50d-Ø). Combo = Phoenix-Trigger mit MADBO innerhalb der letzten N Bars.

| Signal | Phoenix | MADBO-Fenster | n | Hit | Ø Excess | Median |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| MADBO only | – | – | 6.036 | 0,424 | +2,3 % | −2,0 % |
| Phoenix only | `roc_63_126_189_252_w1111` d10 40→90 | – | 83 | 0,723 | +33,5 % | +16,9 % |
| **Combo** | dieselbe | 126 | 75 | **0,733** | **+37,0 %** | **+17,7 %** |
| Phoenix only | `roc_63_126_252_w122` d40 30→95 | – | 180 | 0,550 | +24,0 % | +7,4 % |
| **Combo** | dieselbe | 126 | 148 | **0,574** | +25,2 % | +7,9 % |
| Phoenix only | `ibd_rs_prod` d20 30→80 | – | 1.254 | 0,456 | +10,7 % | +0,4 % |
| **Combo** | dieselbe | 126 | 891 | **0,501** | +11,6 % | +2,5 % |

**Deutung:** MADBO allein outperformt den Markt NICHT (Hit 42 %, Median −2 %). Phoenix trägt
die Edge. MADBO als Vorbedingung hebt Phoenix moderat an (+2…5 pp), am deutlichsten bei der
Produktions-`ibd_rs` (0,456 → 0,501). MADBO ist damit ein **Kandidaten-Filter**, keine
eigenständige Bestätigung.

**MU-Fallstudie:** MADBO 2024-12-19 (87 $), Phoenix 2025-06-10 (114 $, RS 29→82) → danach
150 $ (Sep), 224 $ (Okt), 285 $ (Dez), 415 $ (Jan 2026). Ohne Phoenix saß man ab Dez 2024
bis April 2025 im Minus (77 $); die Phoenix-Bestätigung lieferte den Einstieg mitten in
der laufenden Bewegung.

## Anwendung (Chart-Suche)
- `08_combo_scan.py` → `output/combo_results.csv`, `combo_candidates.csv`, Watchlist `lab_combo_recent` (103 Kandidaten der letzten 180 Tage).
- `09_display_signals.py TICKER --bars 700` → Chart mit MADBO-Markern (orange "M"), Phoenix-Markern (grün "PHX") und RS-Schar in Pane `lab_rs`. Beispiel: MU (`win_signals_mu`).

## Lab-RS als natives Chart-Feature (vorberechnet)

Damit die Lab-RS in **jedem Chartfenster** der Highcap-1000 sichtbar ist, wurde sie additiv
mit `lab_`-Präfix in die Feature-/Preset-Pipeline des Viewers integriert:

1. `11_precompute_lab_rs.py` schreibt je Highcap-Ticker `1D_lab_rs.parquet` mit den Spalten
   `lab_rs`, `lab_rs_112`, `lab_rs_123`, `lab_rs_1111`, `lab_rs_212` und den **exakten
   Timestamps** der `1D_features.parquet` (968 Ticker).
2. `chart_data.py` merged diese Datei additiv in `/api/chartdata` (Block "Lab-RS (Testlab ...)").
   Damit liefert auch `get_timeseries` die Spalten.
3. `12_register_lab_rs.py` registriert die 5 Features in `pca_features` (unterscheidbar über
   `calc_params.variant`) und hängt sie an **alle 11 Presets** als Pane `lab_rs`.
4. Ergebnis: Beim Öffnen eines Charts (Preset `default`, `qmaggi`, `rs_top40`, …) erscheinen die
   Lab-RS-Kurven automatisch in eigener Pane. Bei Nicht-Highcap-Tickern fehlt die Spalte und wird
   sauber übersprungen. Verifiziert: MU-Chart hat 5 `lab_rs`-Overlays.

Aktualisieren: `11_precompute_lab_rs.py` → Dateien in den Parquet-Ordner kopieren (root-Container)
→ `12_register_lab_rs.py` → `docker restart qjm-pca-service`.

### Teardown dieses Teils
`./rs_lab/teardown.sh` löscht `lab_*`-Watchlists, `pca_features`/`pca_feature_set_members` mit
`lab_%` sowie alle `1D_lab_rs.parquet`. Danach den markierten Merge-Block in `chart_data.py`
entfernen und `qjm-pca-service` neu starten.

### Preset `qmaggi_lab` (QMaggi + Lab RS + DollarVol)
Über `manage_chart_presets` erstellt/aktualisiert (`preset_id=qmaggi_lab`). **Ohne ADR**, dafür Dollarvolumen:

- 6 SMAs in Pane `main`
- `ibd_rs` in Pane `rs` (width 2)
- **5 Lab-RS-Kurven in Pane `lab_rs` mit `width: 1`** (dünnste Linie)
- `dollar_volume` (Histogramm) + `ma_sma_50_dollar_volume` (Linie) in Pane `dollar_volume`, width 1
- **9 Lab-RS-Kurven** in Pane `lab_rs` (width 1): harmonische Leiter ``lab_rs_20_40_80``, ``lab_rs_30_60_120``, ``lab_rs_40_80_160``, ``lab_rs_50_100_200`` plus lang (63/126/252)
- Topbar: `ibd_rs`, `lab_rs`, `lab_rs_10_20_50`, `dollar_volume`
- Verifiziert: MU-Chart mit `qmaggi_lab` → 19 Overlays

Anwenden im Viewer: `DISPLAY_STOCK` mit `preset: 'qmaggi_lab'`.

## ADR-neutrale Rangfolge (Option A)

Statt global zu ranken, wird pro Datum **innerhalb von 3 ADR-Terzilen** gerankt
(`14_adr_neutral.py`). Neue Spalten: `lab_rs_n`, `lab_rs_n_20_40_80`, `lab_rs_n_30_60_120`,
`lab_rs_n_50_100_200` – in allen Presets in Pane **`lab_rs_n`** (die rohen weiter in `lab_rs`).

**APGE am Event (raw → neutral):**

| Datum | raw20 | neut20 | raw63 | neut63 |
| :--- | ---: | ---: | ---: | ---: |
| 2025-10-07 | 49 | 33 | 21 | 24 |
| 2025-10-08 | 82 | 59 | 61 | 45 |
| 2025-10-09 | 93 | 83 | 74 | 54 |
| 2025-10-17 | 95 | 89 | 76 | 60 |

**Wirkung (20/40/80, letzter Tag):**
- Korrelation raw↔neutral **0,97** – die Grundordnung bleibt.
- **Top-Dezil: 92 vs. 93 Namen, aber nur 45 überlappen.** Aus dem Top-Dezil fallen u. a.
  AMD, ARM, BE, CRM, DOCU, FTNT, HPQ; hinein kommen Low-ADR-Namen wie ABBV, BDX, DXCM, BNS, CACI.
- APGE bleibt mit 87–89 klar oben (echter News-Move überlebt), wird aber nicht mehr
  allein durch seinen hohen ADR auf 95+ getragen.

**Deutung:** A tut genau, was es soll – es nimmt den systematischen High-ADR-Bias heraus und
macht das Ranking über Volatilitätsregime vergleichbar. **Aber** es stuft auch die explosiven
High-Beta-Leader herunter. Es ist daher ein **Komplement** zum rohen RS, kein Ersatz.

**Livestellen:** `qmaggi_lab` zeigt roh (Pane `lab_rs`) und neutral (Pane `lab_rs_n`) parallel.
Nächste Stellschraube: Buckets 3 → 5 (feiner) oder Neutralisierung nur auf die kurzen Fenster.
