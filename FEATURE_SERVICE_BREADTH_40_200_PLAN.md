# Implementationsplan: Market Breadth 40/200 zurueck in die Panes (vorgerechnet)

**Erstellt:** 2026-09-29 · **Rev. 1**
**Status:** Entwurf — nichts umgesetzt
**Geltungsbereich:** features-service (Producer) -> Parquet -> Pane-Presets/Chart
**Betroffene Repos:** [openBrain/features-service](/home/daniel/openBrain/features-service) (Code), QJM (Registry-Zeile, Panes, Chart, Doku)

**Ziel in einem Satz:** `breadth_40_pct` und `breadth_200_pct` sollen wieder als **vorgerechnete Spalten in jedem Ticker-Parquet** stehen, damit eine Indikator-Pane sie ohne Lag anzeigen kann — und zwar ueber denselben Ausspielweg wie `breadth_minervini_pct`.

---

## 1. Zuerst die fachliche Klaerung (deine Frage)

**Ja, das sind funktional komplett verschiedene Dinge.** Ich habe sie vorher sprachlich in einen Topf geworfen — das war falsch:

| Serie | Definition | Aussage |
| :-- | :-- | :-- |
| `breadth_minervini` / `_pct` | Anteil (Anzahl) der Aktien, die das **Minervini-Trend-Template** erfuellen (6 Kriterien: 200-SMA steigend, Preis ueber 150/200-SMA, 50-SMA ueber 150/200, >=25 % ueber 52-Wochen-Tief, <=25 % unter 52-Wochen-Hoch) | "Wie viele Titel sind in einer Stage-2-Struktur?" — **strukturelle** Breite |
| `breadth_40_pct` | Anteil der Aktien **ueber ihrer 40-SMA** | kurzfristige Breite / kurzer Trend |
| `breadth_200_pct` | Anteil der Aktien **ueber ihrer 200-SMA** | langfristige Breite / Bären-/Bullen-Regime |
| `$STATS.MARKET_BREADTH.percentage` | Anteil der Aktien **ueber ihrer 50-SMA** (heute live) | kurzfristige Breite |

Die drei SMA-Breiten sind untereinander verwandt (gleiche Rechenart, anderer Zeitraum), die Minervini-Breite ist ein anderes Konstrukt. Zwei Breiten nebeneinander sind informativ (z. B. 40 vs. 200 = kurzfristig vs. langfristig), Minervini-Pct ersetzt sie nicht.

---

## 2. Befund (verifiziert)

| # | Befund | Beleg |
| :-- | :-- | :-- |
| B1 | Die SMA-Breite wird **heute** bereits vorgerechnet — aber nur als **50-SMA-Variante** und nur in den **einen** virtuellen Ticker `$STATS.MARKET_BREADTH` (Spalten `count`, `total`, `percentage`, `days_back`). | [market_breadth.py:83](/home/daniel/openBrain/features-service/src/market_breadth.py#L83), [market_breadth.py:119](/home/daniel/openBrain/features-service/src/market_breadth.py#L119) |
| B2 | Der **Ausspielweg fuer marktweite Spalten in jeden Ticker** existiert schon: Features mit `aggregation: "all"` werden in Pass 0 pro Tag aggregiert und in Pass 1 in **jedes** Ticker-Parquet geschrieben (so leben `breadth_minervini`/`_pct` in allen 41.574 Tickern). | [processor.py:101](/home/daniel/openBrain/features-service/src/processor.py#L101), [processor.py:157](/home/daniel/openBrain/features-service/src/processor.py#L157), [processor.py:186](/home/daniel/openBrain/features-service/src/processor.py#L186) |
| B3 | In `1D_features.parquet` eines normalen Tickers liegen 28 Spalten — `breadth_minervini`, `breadth_minervini_pct`, `ibd_rs`, `adr_20` … aber **kein** `breadth_40_pct`/`_200_pct`. | `/api/chartdata?symbol=SPY` live |
| B4 | Die Registry kennt `breadth_40_pct` (mode=offline, plot_type=subchart) — eine **verwaiste Zeile**: das Pane-Preset `market_breadth__main` referenziert sie, die Spalte fehlt aber, also rendert die Pane leer (mit `unknown_column`-Warnung). `breadth_200_pct` fehlt sogar in der Registry. | `/api/features/registry`, `/api/charts/market_breadth` live |
| B5 | Der alte Standalone-Rechner hat die Semantik definiert: 40/200-SMA auf `close`, ein Ticker zaehlt nur an Tagen mit gueltiger SMA, `pct = count/total*100`, Tage mit `total < BREADTH_MIN_STOCKS` (Default 100) entfallen. Er laeuft **nirgends** mehr (kein cron/systemd/Timer). | [generate_market_breadth.py:42](scripts/generate_market_breadth.py#L42), [generate_market_breadth.py:209](scripts/generate_market_breadth.py#L209) |
| B6 | **Keine historische Kopie** der alten 40/200-Reihe vorhanden (nur die lebende `$STATS.MARKET_BREADTH`; das Korrupt-Backup vom 14.09. enthaelt kein `$*`-Verzeichnis). Die Historie ist also **rekonstruierbar, nicht restaurierbar**. | Dateisuche live |
| B7 | Der Batch laeuft **stuendlich** und dauert **2m47s fuer 41.574 Ticker** (1,4 ms/Ticker). Die marktweite Aggregation ist der teure Teil, nicht das Lesen: 14 s DataFrame-Aufbau + 3 s `sum(axis=1)` **pro Ausgabespalte**, gemessen. Der Breiten-Scan selbst braucht mit 16 Prozessen **3,6 s**. | Job-Status live, [processor.py:158](/home/daniel/openBrain/features-service/src/processor.py#L158), [market_breadth.py:156](/home/daniel/openBrain/features-service/src/market_breadth.py#L156) |

---

## 3. Entscheidungen

| # | Entscheidung | Begruendung |
| :-- | :-- | :-- |
| D1 | **Vorrechnen in Pass 0 als `aggregation: "all"`-Features** — nicht on-the-fly. | Genau dein Lag-Argument; und es ist der existierende Weg (B2). Eine On-demand-Rechnung muesste bei jedem Render 41.574 Parquets scannen. |
| D2 | **Eigener Feature-Typ `BREADTH_SMA` mit `window`** (40 / 200), nicht als `BREADTH_MINERVINI` deklariert. | Der Aggregations-Cache teilt Rohspalten **pro Typ** ([processor.py:163](/home/daniel/openBrain/features-service/src/processor.py#L163)). Unter dem falschen Typ waeren 40 und 200 **still identisch** — der klassische Parallel-Edges-Fall. |
| D3 | **SMA wird im Rohschritt gerechnet**, nicht als neue Parquet-Spalte `ma_sma_40`. | Haelt `1D_features.parquet` schlank; `ma_sma_40` kann spaeter als normale SMA-Feature-Zeile nachkommen, wenn du die Linie auch im Chart willst. |
| D4 | **`$STATS.MARKET_BREADTH` additiv erweitern** (`breadth_40_pct`, `breadth_200_pct`, `count_40`, `count_200`) — bestehende Spalten `count`/`total`/`percentage`/`days_back` bleiben **unveraendert**. **Kein** `breadth_50_pct`. | TC2000 kennt nur 40 und 200; die heutige 50-SMA-Breite ist ein Artefakt des alten Producers. Sie bleibt als bestehende Spalte `percentage` stehen (Bestands-Charts brechen nicht), wird aber nicht als neue Serie ausgespielt — eine zusaetzliche 50er-Spalte waere eine Doppelspur. |
| D5 | **Semantik des alten Rechners 1:1 uebernehmen** (B5), inkl. `NaN`-Behandlung: ein Ticker ohne gueltige SMA zaehlt **nicht** als "darunter". | Sonst waere die fruehe Historie systematisch zu niedrig. |
| D6 | **Keine Aenderung an der Minervini-Breite.** | Sie ist live und referenziert; ein Rechenwechsel waere eine stille Zahlenaenderung. |

---

## 4. Umsetzung (4 Schritte, alle klein)

### Schritt 1 — Feature-Typ + Rohberechnung
1. [config_parser.py:7](/home/daniel/openBrain/features-service/src/config_parser.py#L7): `BREADTH_SMA = "BREADTH_SMA"` in `FeatureType`.
2. [calculator.py:274](/home/daniel/openBrain/features-service/src/calculator.py#L274) (Muster `_calc_breadth_minervini_raw`): neu `_calc_breadth_sma_raw(df, config)` -> Spalte `<feature_id>_raw` mit `1.0` / `0.0` / `NaN`:
   - `window` aus `config.additional_params` (Fallback 40),
   - `ma = close.rolling(window).mean()`, gueltig = `close.notna() & ma.notna()`,
   - sonst `NaN`.
3. [processor.py:59](/home/daniel/openBrain/features-service/src/processor.py#L59) — **der kritische Punkt**: der Rohspalten-Cache `breadth_raw_col` muss auf einen **Schluessel aus (Typ, window)** umgestellt werden, sonst kopiert `breadth_200_pct` die 40er-Werte.

### Schritt 2 — Features deklarieren
[features.json:171](/home/daniel/openBrain/features-service/config/features.json#L171) ergaenzen (Muster `breadth_minervini_pct`):
```
"breadth_40_pct":  {"window": 1, "period": "D", "type": "BREADTH_SMA", "sma_window": 40,
                    "aggregation": "all", "mode": "pct_abs", "color": "#2962FF",
                    "style": "solid", "pane": "pct_abs", "chart_type": "line", "thickness": 2},
"breadth_200_pct": {"window": 1, "period": "D", "type": "BREADTH_SMA", "sma_window": 200,
                    "aggregation": "all", "mode": "pct_abs", "color": "#FF9800",
                    "style": "solid", "pane": "pct_abs", "chart_type": "line", "thickness": 2}
```
(`sma_window` wird in Schritt 1 gelesen; `window` bleibt aus Konsistenz zu den Nachbarzeilen 1.)

### Schritt 3 — `$STATS.MARKET_BREADTH` erweitern (D4)
[market_breadth.py:83](/home/daniel/openBrain/features-service/src/market_breadth.py#L83): `process_ticker_file` liefert statt eines Flags eine kleine Matrix je MA-Spalte (`ma_sma_40`, `ma_sma_50`, `ma_sma_200`) — die 40er/200er MAs werden dort direkt aus `close` gerechnet, damit der Scan nicht von neuen Spalten abhaengt; `generate_market_breadth` aggregiert die drei in **einem** Durchlauf (`bincount` je Spalte) und schreibt die neuen Spalten additiv dazu.

### Schritt 4 — Registry + Panes + Chart
1. Registry: `breadth_200_pct` neu anlegen (Muster `breadth_40_pct`, `mode='offline'`, `plot_type='subchart'`); `breadth_40_pct`-Zeile auf den neuen Stand bringen (display_name "Breadth 40 SMA").
2. **Ein** Pane-Preset `tc2000_breite` (role=value, weight 2, linear) mit **zwei** Serien: `breadth_40_pct` (Farbe `#2962FF`) und `breadth_200_pct` (Farbe `#FF9800`), dazu Referenzlinien.
   - Referenzlinien: `refs: [{value: 20}, {value: 50, style: {width: 2}}, {value: 80}]` — 50 als Neutrallinie (TC2000-Konvention), 20/80 als Extreme.
   - Optional (separat entscheiden): feste Y-Achse 0–100 statt Auto-Fit — dafuer fehlt heute ein Pane-Feld (`params.range`), siehe offener Punkt 2.
3. Chart `market_monitor`: Panes `main` / `spx_equal_spread` / `small_vs_big` / `tc2000_breite`.
4. Danach ist auch das alte Pane-Preset `market_breadth__main` automatisch wieder funktionsfaehig (es referenziert `breadth_40_pct`).

---

## 5. Kosten & Messlatten

| Posten | Aufwand | Quelle |
| :-- | :-- | :-- |
| Batch-Dauer heute | 2m47s / 41.574 Ticker (1,4 ms je Ticker) | Job-Status live |
| Rohberechnung je Ticker | eine `rolling(window).mean()` je Fenster (~µs) | neu, vernachlaessigbar |
| Aggregation je Ausgabespalte | ~14 s DataFrame-Aufbau + ~3 s Summe (gemessen) | [processor.py:158](/home/daniel/openBrain/features-service/src/processor.py#L158) |
| Erwartete Mehrdauer | **~+35 s je Ausgabespalte** -> mit zwei Spalten ca. **+70 s** pro Batch (also ~3m55s statt 2m47s) | Hochrechnung aus obigen Messwerten |
| Alternative (nur `$STATS`) | +3,6 s (der Breiten-Scan laeuft fuer alle drei MAs in einem Durchlauf) | [market_breadth.py:156](/home/daniel/openBrain/features-service/src/market_breadth.py#L156) |
| Chart-Anzeige (Pane) | **0 ms** — es ist eine Parquet-Spalte | Architektur |

Wenn dir +70 s pro Stunde zu viel sind, gibt es genau eine saubere Optimierung: **beide Fenster in einem Aggregationslauf** rechnen (eine Rohspalten-Matrix mit zwei Zeilenbloecken und ein gemeinsamer Summenlauf) — dieselbe Änderung, nur andere Innerei, kein zweiter Pfad. Ich wuerde erst messen, dann entscheiden.

---

## 6. Edge-Case-Matrix

| Fall | Verhalten / Regel |
| :-- | :-- |
| 40 und 200 im selben Aggregations-Cache | Cache-Schluessel **(Typ, window)** — sonst still identische Reihen (D2). Regressionstest: beide Reihen muessen sich unterscheiden. |
| Ticker mit < 200 Bars | zaehlt an diesen Tagen **nicht** in `total` (NaN, nicht 0) — sonst ist die fruehe Historie zu niedrig. |
| Nur ein Fenster verfuegbar | `breadth_200_pct` beginnt spaeter als `breadth_40_pct` (200 Bars Warm-up). Das ist korrekt und sichtbar. |
| Rekonstruierte Historie | nutzt das **heutige** Universum; die alte Reihe (vor Sep 14) hatte das damalige. Erwartete Abweichung in frueheren Jahren, in aktuellen Tagen praktisch null. Ehrlich dokumentieren. |
| Wochenend-Buckets | `$STATS` filtert Sa/So (B1), Pass 0 bucketed nur nach UTC-Tag. In einer Pane im Aktien-Fenster rendert ein Wochenend-Bucket ohnehin nicht (kein Bar) — Unterschied dokumentieren, Minervini-Breite **nicht** anfassen (D6). |
| `total < BREADTH_MIN_STOCKS` | nur fuer `$STATS` relevant (dort beibehalten); die Broadcast-Spalten folgen dem Pass-0-Verhalten ihrer Geschwister. |
| Registry-Zeile fehlt | Die Pane funktioniert auch ohne Registry-Zeile (Spalte gewinnt), nur Katalog/Topbar-Metadaten fehlen -> Zeile anlegen. |
| Rollout | Spalten existieren erst nach dem naechsten vollen Batch (~4 min). Bis dahin: bestehende `unknown_column`-Warnung, keine stille Leere. |
| Rueckwaerts | Rein additiv: keine bestehende Spalte aendert sich; Rollback = Feature-Zeilen zuruecknehmen + Batch, die Spalten verschwinden wieder. |

---

## 7. Verifikation

1. **Unabhaengige Nachrechnung** fuer die letzten 3 Handelstage: aus allen `1D_features.parquet` (bzw. `1D.parquet` + selbst gerechneter SMA) `count`/`total`/`pct` bestimmen und gegen die neuen Spalten stellen — Toleranz 0,01 pp.
2. **Plausibilitaet:** 0 <= pct <= 100; `breadth_40_pct >= breadth_200_pct` in der Mehrheit der Tage; Niveau der 50-SMA-Breite (`$STATS.percentage`) liegt zwischen beiden.
3. **Pane-Test:** `market_monitor` mit `tc2000_breite` rendern — beide Linien sichtbar, `warnings[]` leer, Crosshair-Wert == Spaltenwert am selben Tag (fuer 40 **und** 200), Screenshot.
4. **Alt-Pane:** `market_breadth` anwenden -> die frueher leere Pane zeigt jetzt eine Linie (kein `unknown_column` mehr).
5. **Batch-Kosten** protokollieren (Dauer vor/nach) und im Plan-Anhang festhalten.

---

## 8. Offene Punkte

1. **Umsetzen oder erst nur planen?** Der Producer liegt ausserhalb meines Workspace (/home/daniel/openBrain) — Schreibzugriffe dort brauchen deine Freigabe. Der Code-Eingriff selbst ist klein (4 Dateien, ~60 Zeilen), der Batch danach ~4 min.
2. **Feste Y-Achse 0–100 fuer `tc2000_breite`?** Entschieden ist die Kombi-Pane. Offen ist nur, ob die Achse **fix 0–100** sein soll (TC2000-Lesart, Vergleichbarkeit ueber Tage) oder weiter automatisch skaliert. Fix braucht ein Pane-Feld `params.range: [0, 100]` -> `panes_meta` -> Client (eine Stelle, keine Serien-Sonderregel). Empfehlung: machen, es ist klein und macht den Pane erst vergleichbar.
3. **`sma_window`-Namen**: ich nehme `sma_window` (40/200) im Feature-Config; sag Bescheid, wenn du im Producer ein anderes Feld bevorzugst.
4. **Erledigt:** kein `breadth_50_pct` — Artefakt (TC2000 hat 40 und 200). Die 50er-Breite bleibt nur dort, wo sie schon ist (`$STATS.MARKET_BREADTH.percentage`), ohne neue Spalte.

**Entschieden (Rev. 2):** Pane `tc2000_breite` mit 40+200 · feste Y-Achse 0–100 (Feld `params.range`) · Minervini-Breite als eigene 5. Pane im `market_monitor` · Umsetzung startet beim Producer.
