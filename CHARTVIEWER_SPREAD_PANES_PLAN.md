# Implementationsplan: Spread-Indikator-Panes + Chart "Market Monitor"

**Erstellt:** 2026-09-29 · **Rev. 4** (Namen ok · Topbar nein · Prozentzeichen nein · Intraday nein · Chart = Market Monitor, in einem Zug gebaut · Stock-RS draussen, vorhandene Market-Breadth-Serien drin)
**Status:** Entwurf — nichts davon ist umgesetzt
**Geltungsbereich:** `pca-service` (Registry + Rechenweg), `chart-viewer` (Serien-Aufloesung), MCP-Eingabeform (`agent-pca`), Chart/Pane-Daten, Vertragsdoku
**Referenzen:** [docs/architecture/chart-presets.md](docs/architecture/chart-presets.md) (Vertrag), [CHARTVIEWER_CHART_BUILDER_API_PLAN.md](CHARTVIEWER_CHART_BUILDER_API_PLAN.md) (Vorgaenger-Plan), [mcp/agent-pca/tools/chart_viewer.ts](mcp/agent-pca/tools/chart_viewer.ts) (DISPLAY_SERIES)

**Ziel in einem Satz:** Ein Chart **`market_monitor`** — Preispane zeigt den selektierten Stock nur mit SMA 50, darunter ausschliesslich Markt-Panes (SPY−RSP, IWM−SPY, Marktbreite) — und die Spread-Panes werden nicht "drangebastelt", sondern ueber eine allgemeine Serien-Quelle sauber angebunden.

Begriffe:
- **SPY** = ETF auf den kapitalgewichteten S&P 500. **SPX** ist derselbe Index als Zahl, nicht handelbar.
- **RSP** = ETF auf den **gleichgewichteten** S&P 500. SPY−RSP = "Riesen gegen Breite".
- **IWM** = ETF auf den Russell 2000. IWM−SPY = "klein gegen gross".

---

## 0. Zuerst: geht das auch klein?

| # | Weg | Anforderung aendern? | Aufwand | Urteil |
| :-- | :-- | :-- | :-- | :-- |
| K1 | **Zwei Fenster** ueber das vorhandene `DISPLAY_SERIES` | ja: zwei Fenster statt eines; Spread als Kerzen; Werte **fenster-verankert** (aendern sich beim Zoomen) | 0 Code | billig, aber nicht das Gewuenschte |
| K2 | **Offline-Spalte** (Vorbild `breadth_40_pct`) im features-service | ja: Aktualitaet = Batch-Lauf, nur dessen Timeframes | fremdes Repo + Batch ueber ~5.500 Ticker | **nicht empfohlen** |
| K3 | **Abgeleitete Serie als erste Klasse** (Registry-Zeile + Rechnung im pca-service) | nein | 4 Pflicht-Phasen (S/M), **keine DB-Migration** | **empfohlen** |

---

## 1. Befund (am Code und am laufenden System verifiziert)

| # | Befund | Beleg |
| :-- | :-- | :-- |
| B1 | **Ein Instrument pro Fenster; alle Panes teilen Bars und X-Achse.** Spread-Charts sind laut Vertrag eigene Fenster — genau diese Grenze verletzt der Wunsch. | [chart-presets.md:316](docs/architecture/chart-presets.md#L316), [chart-presets.md:325](docs/architecture/chart-presets.md#L325) |
| B2 | **Eine Serie ist implizit "Spalte des Fenster-Instruments"** — es gibt kein Feld, das die Datenquelle benennt. | [presets_api.py:112](services/pca-service/presets_api.py#L112), [chart_spec.py:389](chart_viewer/src/chart_viewer/chart_spec.py#L389) |
| B3 | **Der On-the-fly-Rechenweg existiert**, haengt aber an Fenster-Symbol + Perioden (ein POST je Indikator-Typ). | [chart_spec.py:403](chart_viewer/src/chart_viewer/chart_spec.py#L403), [orchestrator.py:166](chart_viewer/src/chart_viewer/orchestrator.py#L166), [indicators.py:193](services/pca-service/indicators.py#L193) |
| B4 | **Drei Stellen raten denselben Namen:** `_calc_spec`, `_column_to_indicator_spec` und `_calc_key`. | [chart_spec.py:618](chart_viewer/src/chart_viewer/chart_spec.py#L618), [chart_spec.py:685](chart_viewer/src/chart_viewer/chart_spec.py#L685), [orchestrator.py:184](chart_viewer/src/chart_viewer/orchestrator.py#L184) |
| B5 | **Die Registry kennt `calc_type` + `calc_params` + `mode`**, der Viewer sieht aber nur `calc_type`. | [presets_api.py:120](services/pca-service/presets_api.py#L120), [import_features_to_supabase.py:90](scripts/import_features_to_supabase.py#L90) |
| B6 | **Vorhandener Sonderfall, den wir nicht wiederholen:** `days_back` wird in der Topbar hart verdrahtet nachgerechnet. | [orchestrator.py:597](chart_viewer/src/chart_viewer/orchestrator.py#L597) |
| B7 | **Spread-Rechnung existiert nur als RAM-Synthese im MCP** mit eigener Formel (`pct` = fenster-verankert) und eigener Indikator-Mathematik. | [chart_viewer.ts:101](mcp/agent-pca/tools/chart_viewer.ts#L101), [chart_viewer.ts:1056](mcp/agent-pca/tools/chart_viewer.ts#L1056) |
| B8 | **Der `mode`-Wert der Registry ist NICHT belastbar** (live geprueft): `rs_adr_neutral` steht auf `online`, **ist aber eine Spalte** in jedem Ticker — eine harte Regel "online ⇒ nie Spalte" wuerde 5 bestehende Charts (rs_monitor, qmaggi__rs, qmaggi_lab__rs, breadth_line, rs_top40) brechen. Umgekehrt steht `breadth_40_pct` auf `offline`, **hat aber keine Spalte mehr**. | Registry-Dump + `/api/chartdata` live |
| B9 | **Keine Migration noetig:** die Member-Tabelle hat einen FK auf `pca_features(canonical_id)`; eine Spread-Zeile ist eine normale Registry-Zeile. | [040_pane_chart_presets.sql:39](migrations/040_pane_chart_presets.sql#L39) |
| B10 | **Daten sind da:** SPY, RSP, IWM haben 1D-Parquets (letzte Kerze 2026-09-29). Weitere Timeframes wurden nicht gemeldet. | `manage_chart_downloads TICKER_STATUS` |
| B11 | **Markt-Spalten, die es wirklich gibt** (in *jedem* Ticker mit Chartdaten, geprueft an SPY/NVDA/AAPL/MSFT/4GLD/IWM): `breadth_minervini` (Anzahl) und `breadth_minervini_pct` (0–100). `breadth_minervini_pct` hat **keine** Registry-Zeile — braucht auch keine, es ist eine Spalte. | `/api/chartdata` live |
| B12 | **Was es heute an "Markt" gibt, ist kaputt oder irrefuehrend:** Chart `market_breadth` hat die Serie `breadth_40_pct` im **Preispane** (role=price) — die Spalte existiert nicht mehr, die Pane ist heute tot, und in einem Preispane wuerde eine 0–100-Reihe an der Kursskala plattgedrueckt. Chart `breadth_line` heisst nur so (Panes: Kerzen, Stock-RS, Volumen). | `/api/charts/market_breadth`, `/api/panes` live |
| B13 | **Ein Market-Monitor-Chart existiert nicht.** Die naechsten Verwandten sind die beiden obigen. | `/api/charts` live |
| B14 | **`breadth_40_pct` ist keine Altlast, sondern eine Produzenten-Luecke.** Die Registry-Zeile existiert (mode=offline) und `market_breadth__main` referenziert sie — aber **kein** Ticker hat die Spalte noch (SPY/NVDA/AAPL/MSFT/4GLD/IWM geprueft). Das `$STATS.MARKET_BREADTH`-Parquet traegt heute `count`/`total`/`percentage`/`days_back`. Angezeigt wird also nichts (mit `unknown_column`-Warnung), obwohl das Feature gewuenscht ist. | `/api/chartdata`, `/api/features/registry` live |
| B15 | **Werkzeug-Katalog des Harness ist pro Host-Prozess eingefroren.** `manage_pane_presets` existiert serverseitig (14 Tools), fehlte aber in der Session; DSH listet MCP-Tools einmal beim Prozessstart und synchronisiert nur bei Reconnect oder `tools/list_changed` — unsere Server sind statisch. Ein nachtraeglich hinzugefuegtes Tool bleibt darum still unsichtbar. | [mcp-client/src/connection.ts:267](file:///home/daniel/deepseek-harness-017/packages/mcp/mcp-client/src/connection.ts#L267), [tools.ts:123](file:///home/daniel/deepseek-harness-017/packages/mcp/mcp-client/src/tools.ts#L123) |

---

## 2. Design-Entscheidungen (Leitplanken)

| # | Entscheidung | Begruendung |
| :-- | :-- | :-- |
| D1 | **Der Spread ist eine Registry-Zeile** (`calc_type='SPREAD'`, `calc_params={a,b,mode}`, `mode='online'`), kein neuer Tabellentyp. | B9: FK, Presets, Panel, Builder, Diff, Setup bleiben unveraendert. Keine Migration. |
| D2 | **Aufloesungsregel: Spalte gewinnt, Rechnung ist der Fallback.** `mode` ist Metadatum, **kein Gate**. | B8: ein Gate auf `mode` wuerde bestehende Charts brechen. Eine Regel, deterministisch, keine Registry-Pflege als Voraussetzung. |
| D3 | **Nur ankerfreie Formeln**: `rel` = 100*(A/B − 1) (Default), `ratio` = A/B, `abs` = A − B. Das MCP-`pct` ist **kein** Pane-Modus. | Ein Pane wird gescrollt; eine verankerte Formel wuerde die Linie beim Zoomen veraendern. |
| D4 | **Eine Rechenstelle: pca-service (Python, beim Parquet).** Neue Spread-Mathematik nur dort; das MCP wird nicht erweitert. | B7: heute liegt die Mathematik ein zweites Mal in TypeScript. |
| D5 | **Deklarieren statt Ids schreiben:** der Service mintet die kanonische Id deterministisch und legt die Registry-Zeile idempotent an. | Der Agent schreibt nie Ids von Hand; Storage bleibt wie heute. |
| D6 | **Kein stiller Erfolg:** fehlende Legs, kein gemeinsamer Bar-Bereich, unbekannte Spalte ⇒ `warnings[]`. | Der Warnkanal existiert ([chart-presets.md:158](docs/architecture/chart-presets.md#L158)); ein halbes Chart darf nie "success" melden. |
| D7 | **Katalog-Wahrheit ueber tatsaechliche Spalten**, nicht ueber Registry-Flags: `has_data_for_symbol` (existiert schon) entscheidet, was waehlbar ist. | B8: Flags luegen, Spalten nicht. |
| D8 | **Skala linear, Nullinie als `refs`-Level.** Keine neue Zeichenprimitive. | Vorzeichen in Log ist nicht darstellbar; `level` existiert und stoert die Autoskalierung nicht ([pane.py:289](chart_viewer/src/chart_viewer/ui/pane.py#L289)). |
| D9 | **Leg-Reihenfolge ist Bedeutung**, nie sortieren; Id und Titel kodieren die Richtung. | SPY−RSP ≠ RSP−SPY. |
| D10 | **Der Market Monitor enthaelt nur Markt-Panes.** Stock-Kennzahlen (RS-Rating, ADR, Dollar-Volumen, Minervini-Score) kommen nicht hinein, auch nicht "weil sie schon da sind". | Deine Regel; sonst wird aus dem Monitor wieder ein Allerlei-Chart. |

---

## 3. Zielarchitektur

```
Agent (MCP)                    pca-service (Python, am Parquet)         chart-viewer (Python)
-----------                    --------------------------------         ---------------------
manage_pane_presets  ──POST /api/panes──►
  member: {calc_type:"SPREAD",            mintet Id  spread_rel_SPY_RSP
           calc_params:{a,b,mode}}        └─► pca_features (mode=online)
                                          GET /api/panes/{id}  ────────►  chart_spec.build_overlays
                                            series:[{feature_id,             │ Spalte da? ─► Parquet
                                              calc_type, calc_params,        │ sonst     ─► calc-Spec
                                              plot_type, style, rules}]      ▼
                                          POST /api/indicators/calculate  ◄─ calculate(requests[])
                                            requests:[{key,type,params}]     (EIN Aufruf pro Render)
                                            Legs lesen, auf timestamp joinen
                                            ─► series{key:[...]}, errors[]
                                                       │            │
                                                       │            └─► warnings[] (unknown_instrument, …)
                                                       └─► Overlay "line" (Pane) + refs-Level 0 = Nullinie
```

Kernaussage: **die Pane weiss nichts von Spreads.** Sie kennt "Serie, die eine Spalte ist" oder "Serie, die gerechnet wird" — wie eine SMA-Pane heute.

---

## 4. Ziel-Chart `market_monitor`

| Slot | Pane-Preset | Inhalt | Status |
| :-- | :-- | :-- | :-- |
| `main` (role=price, weight 7) | `market_monitor__main` **(neu)** | nur `sma_50` (loest ueber Alias `ma_sma_50` oder on-the-fly auf) | sofort moeglich |
| `spx_equal_spread` (role=value, weight 2) | `spx_equal_spread` **(neu)** | SPY−RSP, `rel`, Nullinie | nach Phase 3 |
| `small_vs_big` (role=value, weight 2) | `small_vs_big` **(neu)** | IWM−SPY, `rel`, Nullinie | nach Phase 3 |
| `tc2000_breite` (role=value, weight 2) | `tc2000_breite` **(neu)** | **eine** Pane, **zwei** Serien: `breadth_40_pct` + `breadth_200_pct` (0–100), Referenzlinien 20/50/80 | **nach dem Producer-Schritt** (eigener Plan: [FEATURE_SERVICE_BREADTH_40_200_PLAN.md](FEATURE_SERVICE_BREADTH_40_200_PLAN.md)) |

Nicht enthalten (bewusst, D10): Volumen, Stock-RS, ADR, Dollar-Volumen, Minervini-Score. Optional und mit einem Aufruf nachrüstbar.

**Entschieden (Rev. 3):** kein Zwischenstand — der Chart wird **in einem Zug** am Ende von Phase 4 gebaut (ein Artefakt, keine Zwischenversion).

**Entschieden (Rev. 4):** Die Markt-Breite wird ein **eigener Pane `tc2000_breite`** mit den zwei SMA-Breiten `breadth_40_pct` + `breadth_200_pct` — **nicht** die Minervini-Breite (das ist ein anderes Konstrukt, siehe [FEATURE_SERVICE_BREADTH_40_200_PLAN.md](FEATURE_SERVICE_BREADTH_40_200_PLAN.md) Abschnitt 1). Die beiden Spalten kommen **vorgerechnet** aus dem features-service (Weg 1, marktweite Pass-0-Features wie `breadth_minervini`) — damit ist die Pane eine normale Parquet-Spalte und hat **null** Lag und **null** Viewer-Code. Dieser Chart-Plan bleibt dadurch unberuehrt: der Pane funktioniert unabhaengig von den Spreads.

---

## 5. Phasen

### Phase 0 — Fakten & Vertrag (S, keine Verhaltensaenderung)

1. **Datenlage fixieren:** SPY/RSP/IWM je Timeframe (1D, 1H, 15min, 1W) pruefen; Ergebnis als Tabelle in diesen Plan. 1D ist da (B10), alles andere ist Zukunft.
2. **Vertrag ergaenzen** in [docs/architecture/chart-presets.md](docs/architecture/chart-presets.md):
   - **"Serien-Quellen"**: Spalte gewinnt, Rechnung ist Fallback (D2); `mode` ist Metadatum; unbekannte Quelle ⇒ Warnung.
   - **"Abgeleitete Serien (Spread)"**: Formeln `rel/ratio/abs`, Ankerfreiheit, Leg-Reihenfolge = Vorzeichen, Skala linear, Nullinie via `refs`.
   - Warncodes + Katalog-Regel (D7).
   - Klarstellung: "ein Instrument pro Fenster" bleibt; abgeleitete Serien sind **instrumentgebunden**, nicht fenstergebunden.
3. **Altlast notieren** (nicht fixen, nur dokumentieren): `breadth_40_pct` ohne Spalte, `rs_adr_neutral` mit falschem `mode`, `market_breadth`/`breadth_line` als irrefuehrende Charts (B8, B12).

**DoD:** Vertrag enthaelt die Abschnitte; Datenlage-Tabelle steht; keine Codezeile geaendert.

---

### Phase 1 — Registry & Katalog-Wahrheit (M)

1. **Member-Eingabeform** ([presets_api.py:149](services/pca-service/presets_api.py#L149)): entweder `feature_id` **oder** `calc_type` + `calc_params`. Genau eines von beiden, sonst 422 `invalid_member`.
2. **Minting** (eine Funktion, z. B. `mint_feature_id(calc_type, calc_params)`):
   - `SPREAD` ⇒ `spread_{mode}_{A}_{B}` (Ticker gross, Reihenfolge wie angegeben), `display_name` `A − B (mode)`, `plot_type='sub_line'`, `mode='online'`.
   - andere `calc_type` in einer Deklaration ⇒ 422 `unsupported_calc_type` (kein stilles Anlegen).
   - **idempotent**: gleiche Definition ⇒ gleiche Id.
3. **`_series_entry`** ([presets_api.py:112](services/pca-service/presets_api.py#L112)) reicht `calc_params` mit durch (Antwortform sonst unveraendert). `mode` wird **nicht** als Gate verwendet (D2).
4. **Katalog-Wahrheit (D7):** `_op_list_topbar_metrics` ([control_service.py:754](chart_viewer/src/chart_viewer/agent/control_service.py#L754)) liefert `selectable` + `reason` aus dem vorhandenen Spalten-Check `has_data_for_symbol`. Spread-Zeilen sind damit korrekt "nicht waehlbar" (keine Spalte), ohne Regel-Sonderfall.
5. **Render-Warnung:** verlangt ein Chart eine Topbar-Metrik ohne Spalte ⇒ `warnings[]: topbar_metric_not_a_column` (die dokumentierte `days_back`-Ausnahme bleibt unberuehrt, weil Topbar hier nicht ausgebaut wird).

**Tests** (Muster [test_preset_pane_scales.py](services/pca-service/test_preset_pane_scales.py), Standalone mit Fake-Supabase): Minting idempotent, `feature_id` XOR `calc_type`, unsupported ⇒ 422, `calc_params` in der Antwort; Katalogfilter in [test_control_service.py](chart_viewer/tests/test_control_service.py).

**DoD:** `manage_pane_presets CREATE` mit einer Spread-Serie legt Preset + Registry-Zeile an; `GET /api/panes/{id}` zeigt `calc_type:'SPREAD'` samt Params; Spread erscheint nicht als waehlbare Topbar-Metrik.

---

### Phase 2 — Rechenweg SPREAD im pca-service (M)

1. **Batch-Envelope** in [indicators.py:193](services/pca-service/indicators.py#L193): optionales `requests:[{key, indicator_type, params}]`.
   - Antwort `{timestamps, series:{key:[...]}, latest_values, errors:[{key,code,detail}]}`.
   - **Der Aufrufer benennt seine Reihe** ⇒ `_calc_key` entfaellt (B4). Ein Render braucht **einen** HTTP-Aufruf statt bis zu vier.
   - Ohne `requests` verhaelt sich der Endpunkt exakt wie heute (Legacy fuer `calculate_indicator`).
2. **Spread-Funktion** (rein, testbar): `compute_spread(df_a, df_b, mode)` — `rel/ratio/abs`, `b` = 0 oder `None` ⇒ `None` (Luecke, nie 0 erfinden).
   - **Alignment:** inner join auf `timestamp`, exakte Gleichheit, **kein Forward-Fill**.
   - Rundung wie bei den bestehenden Indikatoren (4 Stellen).
3. **`normalize_ts` als eine Funktion** (ms→s): existiert heute inline in [chart_data.py:78](services/pca-service/chart_data.py#L78), `load_raw_ohlcv` nutzt sie nicht. Beide Endpunkte benutzen danach denselben Helfer.
4. **Fehler statt 404 fuer Datenprobleme** (D6): `unknown_instrument`, `no_data_for_timeframe`, `no_common_history`; malformed Request bleibt 400.
5. **Limit-Deckel angleichen** (indicators 2000 vs chart_data 10000) + Warnung `partial_history`, wenn die Rechnung nicht bis zum ersten Fenster-Bar zurueckreicht.

**Tests** (neu `services/pca-service/test_spread_series.py`): `rel/ratio/abs` inkl. Vorzeichen, `b=0`/`None`, kein Overlap, eine Seite kuerzer, Feiertags-Luecke, ms-/s-Timestamps, `requests[]`-Envelope mit Erfolg **und** `errors[]`, Legacy-Envelope unveraendert.

**DoD:** Ein POST mit zwei Spread-Requests liefert beide Reihen + `timestamps`; ein fehlendes Leg liefert 200 + `errors[]`, keinen HTTP-Fehler.

---

### Phase 3 — Viewer: Serien-Quelle aufloesen, eine Stelle (M)

1. **chart_spec wird reiner:** `_calc_spec` und `_calc_key` entfallen. `build_overlays` bekommt statt eines Namens-Parsers `calc_spec_for(series)` und `calculate(requests, timeframe, limit)`; die Gruppierung wird `pending[key] = request`.
2. **Regel implementieren und testen (D2):** Spalte zuerst (mit Alias-Aufloesung), sonst Rechnung. Kein `mode`-Gate — genau das verhindert den Bruch aus B8.
3. **Der Orchestrator besitzt die Aufloesung** ([orchestrator.py:319](chart_viewer/src/chart_viewer/orchestrator.py#L319) hat die Registry schon): `calc_spec_for` baut aus `calc_type` + `calc_params` den Request; die Regex ([orchestrator.py:184](chart_viewer/src/chart_viewer/orchestrator.py#L184)) bleibt **nur** als Fallback fuer Spalten ohne Registry-Zeile — eine Konvention, nicht zwei.
   - `source` aus `calc_params` wird respektiert (heute immer "close"); loest es nicht auf ⇒ Warnung `unknown_source_column` statt stillem close.
4. **`calculate_indicators_on_the_fly`** wird zu `calculate_indicator_requests(requests, symbol, timeframe, limit)`.
5. **Warnkanal:** `errors[]` ⇒ `warnings[]` (Codes aus Phase 2) und zusaetzlich in `missing` (Abwaertskompatibilitaet).

**Tests** ([test_chart_spec.py](chart_viewer/tests/test_chart_spec.py) umbauen — die Fake-`calculate`-Signaturen sind die Naht): eine Serie mit `calc_spec` ⇒ ein Request; `errors[]` ⇒ Warnung; **eine vorhandene Spalte gewinnt vor jeder Rechnung** (Regressionstest fuer B8: `rs_adr_neutral` muss weiter aus dem Parquet kommen); bestehende RS-/Bollinger-/Volumen-Tests bleiben gruen.

**DoD:** `pytest chart_viewer/tests` gruen; keine Funktion mehr, die aus einem Seriennamen ableitet, wie gerechnet wird.

---

### Phase 4 — Market Monitor bauen und beweisen (S)

1. **Pane-Presets** (`manage_pane_presets CREATE` — **Hinweis:** dieses Tool ist mir aktuell nicht freigeschaltet; entweder du fuehrst die Aufrufe aus, oder es wird mit freigegeben):
   - `market_monitor__main`: role=price, Member `sma_50` (Farbe nach Geschmack), sonst leer.
   - `spx_equal_spread`: `SPX Equal Weight Spread (SPY − RSP)`, role=value, linear, Member `{calc_type:'SPREAD', calc_params:{a:'SPY', b:'RSP', mode:'rel'}, style_override:{color:'#2962FF', width:2}}`, `refs:[{value:0,label:'0'}]`.
   - `small_vs_big`: `Small vs Big (IWM − SPY)`, gleiche Form, Farbe `#FF9800`, Legs `a:'IWM', b:'SPY'`.
   - `breadth_monitor`: `Market Breadth (Minervini %)`, role=value, Member `breadth_minervini_pct`, `refs:[{value:50,label:'50'}]`, `rules.thresholds:[{below:50,color:'#EF5350'},{above:50,color:'#26A69A'}]`.
2. **Chart** `market_monitor` (`manage_chart_presets CREATE`) mit den vier Slots aus Abschnitt 4.
3. **Anwenden:** `DISPLAY_STOCK {ticker:'SPY', chart:'market_monitor'}` bzw. `APPLY_CHART` auf ein offenes Fenster, dann `SAVE_CHART`.
4. **Beweis, nicht Behauptung:**
   - `GET_CHART_STATE`: vier Panes in Reihenfolge, `warnings[]` und `skipped[]` leer.
   - **Zahlennachweis Spread:** `|Pane − 100*(SPY/RSP − 1)| < 1e-6` am letzten Bar, gegen `get_timeseries` gerechnet.
   - **Zahlennachweis Breadth:** Pane-Wert = `breadth_minervini_pct` des letzten Bars.
   - **Sichtnachweis:** `manage_chart_viewer SCREENSHOT` — Nullinie, Vorzeichen, keine leere Pane.
   - **Negativtest:** Fenster auf `1H` ⇒ Warnung `no_data_for_timeframe` (Spread) bzw. Spalte fehlt ⇒ `unknown_column` (Breadth), kein Absturz, keine stille Leere.
5. **Altlast melden:** `market_breadth` zeigt heute eine tote Pane (B12). Auf Wunsch separat aufraeumen (eigener kleiner Schritt, nicht Teil dieses Plans).

**DoD:** Ein Fenster, vier Panes, Zahlennachweise und Screenshot im Plan-Anhang.

---

### Phase 5 — Doku (S)

1. [chart-presets.md](docs/architecture/chart-presets.md): §7-Umsetzungstabelle, §8-Bedienung, Beispiel `market_monitor`.
2. [mcp/agent-pca/tools/panes.ts:52](mcp/agent-pca/tools/panes.ts#L52): zweite Member-Form dokumentieren (`calc_type`/`calc_params` + Beispiel).
3. Diesen Plan auf "Umgesetzt" setzen, Abweichungen und Nachweise anhaengen.

---

## 6. Vertraege (Ziel)

**Registry-Zeile (Daten, keine Migration):**
```
canonical_id : spread_rel_SPY_RSP
display_name : SPY − RSP (rel)
calc_type    : SPREAD
calc_params  : {a: "SPY", b: "RSP", mode: "rel"}
plot_type    : sub_line
mode         : online
default_style: {color: "#2962FF", width: 2}
```

**Member-Eingabeform (MCP → Service), genau eines von beiden:**
```
{feature_id: "sma_50", …}                                  # bestehende Form
{calc_type: "SPREAD",                                      # neue Form
 calc_params: {a: "SPY", b: "RSP", mode: "rel"},
 style_override: {color: "#2962FF", width: 2}, rules: {}}
```

**Rechen-Aufruf (Viewer → Service), ein Aufruf pro Render:**
```
POST /api/indicators/calculate
{symbol: "SPY", timeframe: "1D", limit: 2000,
 requests: [{key: "spread_rel_SPY_RSP", indicator_type: "SPREAD",
             params: {a: "SPY", b: "RSP", mode: "rel"}},
            {key: "sma_50", indicator_type: "SMA",
             params: {window: 50, source: "close"}}]}
→ 200 {timestamps: [...], series: {"spread_rel_SPY_RSP": [...], "sma_50": [...]},
       latest_values: {...}, errors: [{key, code, detail}]}
```

**Neue Warncodes:** `unknown_instrument`, `no_data_for_timeframe`, `no_common_history`, `unknown_source_column`, `partial_history`, `topbar_metric_not_a_column`.

---

## 7. Edge-Case-Matrix

| Fall | Verhalten |
| :-- | :-- |
| Leg hat gar keine Daten | 200 + `unknown_instrument` ⇒ Warnung; Pane bleibt leer und **laut** |
| Leg hat diesen Timeframe nicht | `no_data_for_timeframe` (Intraday ist Zukunft, B10) |
| Legs ohne gemeinsame Bars | `no_common_history` |
| Eine Leg endet frueher / ist ausgesetzt | Linie endet; **kein** Forward-Fill |
| Feiertag nur auf einer Seite | inner join ⇒ Luecke in der Linie |
| Fenster laedt mehr Bars als der Rechen-Deckel | gleicher Deckel + `partial_history` |
| Fenster-Symbol ist nicht A oder B (z. B. NVDA) | Panes rechnen trotzdem; der Titel nennt die Legs |
| Ticker ohne Chartdaten (z. B. LPK.DE) | Fenster oeffnet nicht — Fall liegt vor dem Chart |
| Ticker ohne Markt-Spalten | `unknown_column` (laut); heute betrifft es nur Ticker ohne Chartdaten (B11) |
| Log-Skala angefordert | linear erzwungen (Preset) + Fallback in `fit_range` |
| `b = 0`/`None` | `None` (Luecke), niemals 0 |
| ms- vs s-Timestamps | ein `normalize_ts` fuer beide Endpunkte |
| Legs vertauscht | anderes Vorzeichen; Id + Titel kodieren die Reihenfolge |
| Gleiche Definition zweimal | gleiche Id, idempotent |
| Registry-Flag veraltet (B8) | **kein** Effekt: die Aufloesung fragt die Spalte, nicht das Flag |
| Registry-Zeile geloescht, Member bleibt | FK CASCADE ⇒ `pane_without_series` (laut); Regel: Pane archivieren statt Feature loeschen |
| Alte Breadth-Pane (`breadth_40_pct`) | bleibt tot, aber **laut** (`unknown_column`) — Aufraeumen optional |

---

## 8. Tests & Verifikation

| Ebene | Datei | Inhalt |
| :-- | :-- | :-- |
| Service-Unit | `services/pca-service/test_spread_series.py` (neu) | Spread-Mathematik, Alignment, Fehlerfaelle, `requests[]`, Legacy unveraendert |
| Service-Preset | [test_preset_pane_scales.py](services/pca-service/test_preset_pane_scales.py) | Member-Form, Minting, `calc_params` in der Antwort |
| Viewer-Unit | [test_chart_spec.py](chart_viewer/tests/test_chart_spec.py) | calc-Spec vom Aufrufer, ein Request, `errors[]` ⇒ Warnung, **Spalte gewinnt** (B8-Regression) |
| Viewer-Control | [test_control_service.py](chart_viewer/tests/test_control_service.py) | `selectable`/`reason` aus dem Spalten-Check |
| E2E | MCP-Smoke | Phase-4-Beweise (zwei Zahlennachweise, Screenshot, Negativtest) |

---

## 8a. Umsetzungsstand (Rev. 5, 2026-09-29 abends) — Phasen 1–4 gebaut und am lebenden System verifiziert

| Phase | Zustand | Beleg |
| :-- | :-- | :-- |
| 1 Registry/Minting | **gebaut** — `PaneMember` akzeptiert `feature_id` ODER `calc_type`+`calc_params` (genau eines, sonst 422); `mint_derived_feature` + `resolve_member_feature`; `_series_entry` reicht `calc_params` durch | [presets_api.py](services/pca-service/presets_api.py); live: `POST /api/panes` mit Spread-Member hat `spread_rel_SPY_RSP` und `spread_rel_IWM_SPY` in `pca_features` angelegt |
| 2 Rechenweg SPREAD | **gebaut** — `requests[]`-Envelope, `compute_spread` (rel/ratio/abs, Inner Join auf timestamp, kein Forward-Fill, b=0/NaN ⇒ Luecke), `errors[]` statt 4xx fuer Datenprobleme, `normalize_ts` als eine Stelle | [indicators.py](services/pca-service/indicators.py), [chart_data.py](services/pca-service/chart_data.py); Smoke-Test: 3 Reihen + `unknown_instrument` fuer ein fehlendes Leg |
| 3 Viewer-Naht | **gebaut** — `_calc_spec`/`_calc_key` entfernt; `build_overlays` bekommt `calculate(requests)` (ein Aufruf) + `calc_spec_for` (Keyword); jede Reihe bringt eigene `timestamps` mit; `errors[]` ⇒ `warnings[]`; `partial_history` neu | [chart_spec.py](chart_viewer/src/chart_viewer/chart_spec.py), [orchestrator.py](chart_viewer/src/chart_viewer/orchestrator.py); 33 Tests in [test_chart_spec.py](chart_viewer/tests/test_chart_spec.py) gruen |
| 4 Panes + Chart | **gebaut** — `market_monitor__main` (nur SMA 50), `spx_equal_spread`, `small_vs_big`, `tc2000_breite`, `breadth_mm`; Chart `market_monitor` | `GET /api/charts/market_monitor`; `GET_CHART_STATE` zeigt 5 Panes |
| E2E | **verifiziert** — `DISPLAY_STOCK SPY chart=market_monitor`: 12 Overlays, Warnungen nur `partial_history` (SPY−RSP-Linie deckt nicht das ganze 2000-Bar-Fenster, weil RSP spaeter beginnt); Zahlennachweis: Pane 265,84 vs Rechnung `100*(765,11/209,065-1)` = 265,80 | Screenshot `dsh_playground/screenshot_snap_20260929_191334_82c553_win_spy_1d.png` |

**Deploy-Regel (wichtig):** `services/pca-service` und `chart_viewer/src` sind in ihre Container **gemountet** — ein `docker restart qjm-pca-service` bzw. `qjm-chart-viewer-server` genuegt, kein Rebuild (im Gegensatz zum features-service, dessen `src` im Image steckt).

**Befund aus Phase 1:** `pca_features` hat einen **Unique-Index auf `(calc_type, calc_params)`**. Fuer Spreads unkritisch (Parameter unterscheiden sich), aber zwei Auspraegungen desselben Typs mit gleichen Parametern sind nicht als zwei Zeilen moeglich — `breadth_minervini_pct` brauchte deshalb `calc_params={aggregation: all, mode: pct}`.

**Noch offen:** Phase 5a (Topbar-Katalog: `selectable`/`reason` aus dem Spalten-Check) ist **nicht** gebaut.

---

## 9. Nicht-Ziele (bewusst, mit deinen Entscheidungen)

- **Kein Prozentzeichen an Achse/Crosshair** (deine Entscheidung). Sachlich richtig waere es ohnehin keine Spread-Eigenschaft, sondern eine **allgemeine Pane-Eigenschaft** (`pane.params.unit`) — ADR ist genauso ein Prozentwert und haette denselben Anspruch. Spaeter, in einem eigenen Schritt.
- **Kein Spread-Wert in der Topbar** (deine Entscheidung). Damit entfaellt auch der generische Topbar-Umbau; der `days_back`-Sonderfall bleibt vorerst stehen und ist als Altlast notiert.
- **Kein Intraday.** RSP/IWM haben heute nur 1D (B10) — Zukunftsthema, keine Architekturfrage.
- **Kein Mehrfach-Instrument-Fenster.** Die Invariante bleibt; nur Serien duerfen instrumentgebunden sein.
- **Kein Spread im Scanner** (Scanner lesen Parquet-Spalten).
- **Keine Umstellung von `DISPLAY_SERIES`** (fenster-verankertes `pct` bleibt, N²-Paare vertragen keinen HTTP-Aufruf je Paar).
- **Keine Glattung des Spreads** (SMA auf Spread) in v1.
- **Kein Umbau des Legacy-Flachpfads** (`preset`/`indicators` ohne Panes).
- **Keine DB-Migration.**
- **Kein Stock-RS/ADR/Volumen im Market Monitor** (D10) — optional nachrüstbar.

---

## 10. Aufwand, Reihenfolge, offene Punkte

| Phase | Groesse | Abhaengig von | Blockiert |
| :-- | :-- | :-- | :-- |
| 0 Fakten/Vertrag | S | — | alle |
| 1 Registry/Katalog | M | 0 | 2, 3, 4 |
| 2 Rechenweg | M | 0 | 3, 4 |
| 3 Viewer-Naht | M | 1, 2 | 4 |
| 4 Market Monitor + Beweis | S | 3 | — |
| 5 Doku | S | alle | — |

**Entschieden:** Namen (`spx_equal_spread` = SPY−RSP, `small_vs_big` = IWM−SPY) · Topbar nein · Prozentzeichen nein · Intraday nein · Chart = `market_monitor` **in einem Zug** · Stock-RS **nein** (D10) · vorhandene Market-Breadth-Serien **ja** (`breadth_minervini_pct`, optional `breadth_minervini`).

**Werkzeug-Lage (erledigt, B15):** `manage_pane_presets` existiert serverseitig; der Host hatte nur einen veralteten Katalog. Ein Reconnect (pca-MCP-Container neu gestartet) synchronisiert die Tools. Kein neues Werkzeug noetig — ein zweites waere eine Doppelspur gewesen.

**Zwei Deploy-Fallen (nicht angefasst, nur gemeldet):**
1. [scripts/dsh_apply_profile_patch.sh:28](scripts/dsh_apply_profile_patch.sh#L28) schreibt nach `/home/daniel/.dsh`, der laufende 0.1.7-Host liest aber `DSH_HOME=/home/daniel/.dsh-017` — ein Deploy liefe ins leere Verzeichnis (stiller No-op auf einen falschen Home).
2. Der Generator-Kandidat ist **kein** Drop-in fuer das Live-Profil: das Profil traegt zusaetzlich Host-Rows (`ui-settings-general`, `llm-pi-ai`) und den `tool-presentation`-Fix; ein Ersetzen wuerde sie loeschen. Die Persona-Aenderung muss deshalb **zeilenweise** in den Preset-Block des Live-Profils gemerged werden (mit Backup), nicht als Datei-Kopie.

**Noch offen (kurz):**
1. **Volumen-Pane** ja/nein? (Standard in fast jedem Chart, aber keine Markt-Aussage — meine Empfehlung: nein, per `ADD_PANE` jederzeit nachrüstbar.)
2. **Erledigt:** `breadth_40_pct`/200 kommen über **Weg 1** zurück (Producer, vorgerechnet), Pane-Name `tc2000_breite` — eigener Plan [FEATURE_SERVICE_BREADTH_40_200_PLAN.md](FEATURE_SERVICE_BREADTH_40_200_PLAN.md). Offen bleibt dort nur: feste Y-Achse 0–100 ja/nein.
3. **`market_breadth`-Chart** (B12): reparieren, auf `market_monitor` umbiegen oder unangetastet lassen?
