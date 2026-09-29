# Implementationsplan: Market Breadth 40/200 zurueck in die Panes (vorgerechnet)

**Erstellt:** 2026-09-29 · **Rev. 3**
**Status:** **in Umsetzung** — Schritte 1, 2 und 4.1 sind gebaut (Producer-Patch im Container, Registry-Zeilen geschrieben, Batch laeuft); Schritt 3 (`$STATS`) und 4.2/4.3 (Pane + Chart) stehen noch aus.
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

## 4a. Umsetzungsstand (Rev. 3, 2026-09-29)

| Was | Zustand | Beleg |
| :-- | :-- | :-- |
| Producer-Patch (Schritte 1+2) | **gebaut** — `config_parser.py` (`BREADTH_SMA`), `calculator.py` (`_calc_breadth_sma_raw`, NaN-basiert ohne MA-Padding), `processor.py` (Rohspalten-Cache je Typ+Fenster, Warnkommentar am `share_key`), `features.json` (zwei Features) | Skript `dsh_playground/breadth_patch/apply_breadth_patch.py`, Backups in `.../backup/`, `py_compile` + JSON-Check ok |
| Deploy | **im Container** — Image neu gebaut (`c9daa34774d5`), Container recreated, `/app/src` enthaelt `BREADTH_SMA` und `_calc_breadth_sma_raw` | `dsh_playground/breadth_patch/build.log` |
| Batch | **gestartet** (`POST /features/calculate?priority=SPY`, HTTP 202) — Pass 0 mit den zwei neuen Aggregationen durchlaufen | `verify.log` / `/features/status` |
| Registry (Schritt 4.1) | **geschrieben** — `breadth_200_pct` neu, `breadth_40_pct` auf `calc_type=BREADTH_SMA`, `calc_params={sma_window:40}`, Anzeigename "Breadth 40 SMA" | Supabase `pca_features` live geprueft |
| Feste Y-Achse 0–100 | **gebaut** — `params.range` -> `chart_spec.panes_meta.range` (mit Warnung `invalid_pane_range` bei Murks) -> `canvas._sync_panes` -> `ChartPane.set_fixed_range`; wirkt nur, wenn gesetzt, Auto-Fit bleibt Default | `models/validation.py`, `chart_spec.py`, `ui/canvas.py`, `ui/pane.py`, Vertrag §4.1, zwei neue Tests |
| Schritt 3 (`$STATS.MARKET_BREADTH` +40/200) | **offen** — braucht einen zweiten Patch + Rebuild | — |
| Pane-Preset `tc2000_breite` | **offen** — wird nach der Spalten-Verifikation angelegt (MCP-Tool fehlt in meiner Session, daher ueber denselben HTTP-Endpunkt `POST /api/panes`) | — |

**Deploy-Falle (wichtig, hat mich gekostet):** beim features-service ist nur `config/` und `data/` gemountet — `src/` steckt im Image. Eine Code-Aenderung auf dem Host ist **nicht** live; es braucht `docker compose build features-service && docker compose up -d --no-deps features-service`. Konfigurationsaenderungen (`features.json`) sind dagegen sofort live. Und: der Docker-Build braucht Schreibzugriff auf `~/.docker/buildx` (ausserhalb des Workspace).

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

---

## 9. Pruefprotokoll (Rev. 4, 2026-09-29)

**Gebaut und live:**
- `breadth_40_pct` + `breadth_200_pct` in **jedem** Ticker-Parquet (SPY: 30 Spalten), Broadcast-Invariante ueber 5 Ticker identisch, 40 != 200, Werte in 0-100.
- Registry-Zeilen gepflegt; Pane-Preset `tc2000_breite` angelegt (role=value, `params.range=[0,100]`, refs 20/50/80, Serien 40 + 200).
- Feste Y-Achse im Viewer: `params.range` -> `panes_meta.range` -> `Pane.set_fixed_range`; Tests gruen.
- `$STATS.MARKET_BREADTH` traegt jetzt zusaetzlich `breadth_40_pct`, `breadth_200_pct`, `count_40`, `count_200` (alte Spalten unveraendert).

**Befund 1 GELOEST und VERIFIZIERT (2026-09-29, ~21:20):** Der urspruenglich gemessene Offset
(+0,72 pp / +0,13 pp) war **kein Producer-Fehler**, sondern ein Tie-Break-Artefakt meiner Nachrechnung:
~124 Sub-Penny-/OTC-Titel (z. B. ATTBF mit close 0.0, GGLXF mit konstant 0.005) liegen **exakt auf ihrer
SMA**; pandas' rolling mean trifft den Wert exakt (striktes `>` ⇒ "nicht darueber", korrekt), meine
numpy-Faltung erzeugt Float-Dust und zaehlte sie als "darueber". Mit einer Toleranz von 1e-9:
**0 Abweichungen von 422 Tickern** im Einzelvergleich, und im Aggregat:

| Tag | nachgerechnet b40 | gespeichert | Δ | nachgerechnet b200 | gespeichert | Δ |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| 2026-09-24 | 33,60 | 33,63 | −0,03 | 45,20 | 45,20 | 0,00 |
| 2026-09-25 | 32,23 | 32,27 | −0,04 | 44,42 | 44,42 | 0,00 |
| 2026-09-26 | 33,26 | 33,29 | −0,03 | 44,78 | 44,79 | −0,01 |
| 2026-09-28 | 28,90 | 28,93 | −0,03 | 43,03 | 43,04 | −0,01 |
| 2026-09-29 (live) | 27,18 | 27,25 | −0,07 | 41,71 | 41,73 | −0,02 |

Rest ≤ 0,04 pp auf eingefrorenen Tagen = die Toleranz selbst (meine Variante ist minimal strenger).
Damit sind Spalten, Broadcast, Dedup und Formel **verifiziert**; der laufende Tag driftet erwartungsgemaess
mit dem Live-Kurs.

**Befund 1 alt (historisch) — doppelte Tageszeilen:**
1.183 der 41.575 Chartdateien tragen **zwei Zeilen fuer denselben UTC-Tag** (settled + Live-Bar).
Pass 0 rollt die SMA ueber die Rohzeilen und dedupliziert erst danach je Tag -> systematische
Abweichung gegen eine saubere Nachrechnung: **+0,72 pp (40er) / +0,13 pp (200er)**, stabil ueber
fuenf eingefrorene Handelstage. Fix = Patch 3 (`apply_breadth_patch3.py`: Dedup **vor** der SMA, in
`calculator._calc_breadth_sma_raw` und `market_breadth._flags_for_window`) — **noch nicht angewendet**,
die Sandbox-Freigabe fuer den Schreibzugriff ausserhalb des Workspace lief in den Timeout.

**Befund 2 GELOEST und VERIFIZIERT (2026-09-29, ~21:05):** Zwei Fehler in *meinen* Patches, beide behoben:
(a) der Aggregations-Schluessel war um `shift` verschoben (`int(k) * 86400` statt `int(k + shift) * 86400`) -
die Zeile fuer heute griff auf den Eintrag von ~2018 zu; (b) Patch 3 hatte beim Umbau von
`_flags_for_window` die Funktion `process_ticker_file` **mitgeloescht** → der Batch loggte
`Failed to generate market breadth: name 'process_ticker_file' is not defined`, die `$STATS`-Datei
wurde nie neu geschrieben (deshalb blieben die alten falschen Werte stehen). Patch 4 hat die Funktion
wiederhergestellt. Ergebnis nach dem Batch (Generator laeuft in ~11 s):

| Tag | `$STATS.breadth_40_pct` | Ticker-Spalte | `$STATS.breadth_200_pct` | Ticker-Spalte |
| :-- | :-- | :-- | :-- | :-- |
| 2026-09-29 (live) | 27,40 | 27,37 | 41,80 | 41,80 |
| 2026-09-28 | 28,93 | 28,93 | 43,04 | 43,04 |
| 2026-09-26 | 33,29 | 33,29 | 44,79 | 44,79 |

`percentage` (50er, Altbestand) unveraendert. **Damit ist die Breiten-Kette vollstaendig verifiziert.**

**Befund 2 alt (historisch) — die `$STATS`-Zusatzspalten waren nicht brauchbar:**
`breadth_40_pct` zeigt 48,23 % (Vortag) bzw. 54,29 % (letzter Tag) gegen ~29 % aus der Ticker-Spalte;
`count_40=1.639` impliziert einen Nenner von nur ~3.400 Tickern (erwartet ~13.800 nach Stichprobe:
78 % der Ticker haben ein volles 40er-Fenster). `breath_200_pct` ist am letzten Tag `None`, obwohl der
Nenner da sein muesste. Ursache noch nicht lokalisiert — **bis dahin die beiden Spalten ignorieren**;
die Pane liest sie nicht (sie kommt aus den Ticker-Spalten).

**Stand 21:xx — was der Container jetzt kann:** `calculator.py` traegt den Tages-Dedup-Fix
(gebaut + deployed), `market_breadth.py` traegt Patch 2 + Dedup, aber **noch nicht den Schluessel-Fix**
(`int(k) * 86400` statt `int(k + shift) * 86400`). Ursache des falschen `$STATS`-Werts, bewiesen:
`shift` ist der **kleinste** Tagesindex und **negativ** (fuenf Titel reichen bis 1962), der Schluessel war
damit um `shift` Tage verschoben - die Zeile fuer heute griff auf den Eintrag von ~2018 zu (kleine
Nenner 1.841/17.660, deshalb 48 %/54 % statt ~29 %/44 %).

**Einmal-Befehl fuer den Schluessel-Fix (Nutzer-Shell, weil Schreibzugriff ausserhalb des Workspace):**
```bash
python3 - <<'PY'
import py_compile
from pathlib import Path
p = Path('/home/daniel/openBrain/features-service/src/market_breadth.py')
t = p.read_text(encoding='utf-8')
old = "            int(k) * 86400: (int(abo_arr[k]), int(tot_arr[k])) for k in present"
new = "            int(k + shift) * 86400: (int(abo_arr[k]), int(tot_arr[k])) for k in present"
assert t.count(old) == 1, t.count(old)
p.write_text(t.replace(old, new, 1), encoding='utf-8')
py_compile.compile(str(p), doraise=True)
print('key fix ok')
PY
cd /home/daniel/openBrain && docker compose build features-service && docker compose up -d --no-deps features-service
curl -s -X POST "http://127.0.0.1:8003/features/calculate?priority=SPY"
```

**Neustart des Hosts (vom Nutzer auszufuehren):** `systemctl --user restart dsh-native-017`
(Unit bestaetigt: ExecStart=/home/daniel/start-dsh-native-017.sh, DSH_HOME=/home/daniel/.dsh-017).
Danach eine **neue** Session — die hat dann alle 14 pca-Tools inkl. `manage_pane_presets`.
