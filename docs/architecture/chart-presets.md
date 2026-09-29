# Chart-Viewer: Pane-Presets, Chart-Definitionen, Builder

Status: verbindlicher Vertrag (implementiert in Migration 040 ff.)
Kurzfassung der Diskussion: Presets zerfallen in zwei Ebenen. Ein **Pane-Preset**
beschreibt, was in *einem* Pane steht. Eine **Chart-Definition** ist ein Fenster-
Inhalt: geordnete Panes + Topbar. Ein **Setup** ist die Anordnung der Fenster und
referenziert die Chart-Definition je Fenster.

```
Setup        = Fenster-Slots + Geometrie + Monitor + (chart_id, symbol, timeframe)
Chart        = geordnete Panes (Slot -> Pane-Preset) + topbar_metrics + x_axis_pane
Pane-Preset  = Serien (Features + Style + Regeln) + Skala + Referenzlinien + Zonen
```

Rollen: `price` (Kerzen, Instrument, Crosshair-Besitzer; Slot-Id **main**),
`value` (eigene Y-Achse, geteilte X), `volume` (Histogramm des Fensters).
Invariante: ein Instrument pro Fenster; alle Panes teilen Bars und X-Achse.

## 1. Tabellen (Source of Truth)

`pca_pane_presets`
  id text pk, display_name text, description text,
  role text default 'any'          -- price | value | volume | any
  kind text default 'indicator'    -- indicator | custom (custom = spaeterer Renderer)
  renderer text null, params jsonb default '{}',
  default_scale text default 'linear',
  refs jsonb default '[]'          -- [{"value":80,"label":"80","style":{...}}]
  derives jsonb default '[]'       -- [{"id":"revival","fn":"cross_window","series":"ibd_rs","low":30,"high":80,"within":20}]
  zones jsonb default '[]'         -- [{"from":"revival","style":{"color":"#26A69A","alpha":38},"label":"30->80"}]
  archived bool default false, created_at, updated_at

`pca_pane_preset_members` (pk: pane_preset_id, feature_id)
  pane_preset_id text fk, feature_id text fk -> pca_features(canonical_id),
  sort_order int default 0, style_override jsonb default '{}',
  rules jsonb default '{}'         -- {"thresholds":[{"above":80,"color":"#26A69A"},{"below":30,"color":"#EF5350"}]}

`pca_chart_presets`
  id text pk, display_name text, description text,
  topbar_metrics text[] default '{}', x_axis_pane text null,
  created_at, updated_at

`pca_chart_preset_panes` (pk: chart_preset_id, pane_id)
  chart_preset_id text fk, pane_id text,          -- Slot-Id im Fenster ("main","rs","volume")
  pane_preset_id text fk -> pca_pane_presets(id),  -- "builtin:volume" = eingebautes Volumen-Pane
  sort_order int default 0, weight int default 2, scale text default 'linear',
  overrides jsonb default '{}'

Regeln:
- Genau ein Pane hat role=price und pane_id='main'. Fehlt eines, wird das erste
  Pane zum Preispane (Kompatibilitaet zu Altdaten).
- pane_id ist innerhalb eines Charts eindeutig; fehlt sie, wird die Pane-Preset-Id
  genommen, bei Kollision mit Suffix `_2`.
- `builtin:volume` ist ein virtuelles Pane (kein DB-Eintrag), das der Orchestrator
  mit dem Volumen-Histogramm des Fensters fuellt.
- **Die Pane-Liste ist vollstaendig.** Sie beschreibt genau die Panes des Fensters;
  es wird kein Volumen-Pane ergaenzt. `builtin:volume` wird nur gezeichnet, wenn
  die Definition eine Pane mit role=volume enthaelt. Migration 040 hat es fuer
  Altdaten einmalig explizit eingetragen - seitdem ist es eine normale, entfernbare
  Pane. Zwei Stellen, die es frueher stillschweigend ergaenzten (Aufloesung und
  Speichern), machten "Volumen entfernen" unmöglich und sind entfernt.
- Altdaten `pca_feature_sets`/`pca_feature_set_members` werden einmalig migriert:
  je Feature-Set ein Chart-Preset (gleiche id), je (Set, pane-String) ein
  Pane-Preset mit id `<set_id>__<pane>`; identische Inhalte werden geteilt.

## 2. HTTP-API (PCA-Service, Prefix /api)

Neu:
- GET  /api/panes                 -> {panes:[{id,display_name,description,role,kind,member_count,summary}]}
- GET  /api/panes/{id}            -> Pane-Preset mit aufgeloesten Serien
- POST /api/panes  PUT /api/panes/{id}  DELETE /api/panes/{id} (soft: archived=true)
- GET  /api/charts                -> {charts:[{id,display_name,description,pane_count,summary}]}
- GET  /api/charts/{id}           -> Chart mit Panes inkl. aufgeloester Serien
- POST /api/charts PUT /api/charts/{id} DELETE /api/charts/{id}
- PATCH /api/charts/{id}/pane_scales  {pane_scales:{pane_id:"log"}}

Kompatibilitaet (bleibt funktionsfaehig, liest/schreibt ueber die neuen Tabellen):
- GET  /api/presets               -> Chart-Liste (id, display_name, description, indicator_count)
- GET  /api/presets/{id}          -> flache Altform: {name, display_name, description,
                                     indicators:[{column,canonical_id,calc_type,mode,plot_type,pane,style,rules}],
                                     topbar_metrics, pane_scales}
- POST/PUT/DELETE /api/presets... -> Adapter: flache Mitgliederliste (mit pane-Strings)
                                     wird in Pane-Presets + Chart-Struktur uebersetzt.
                                     Die Altform kennt keinen Zustand "ohne Volumen"
                                     (der alte Viewer zeichnete es immer); bei der
                                     Uebersetzung wird `builtin:volume` deshalb -
                                     wie in Migration 040 - explizit angehaengt.
                                     Ein Request mit `panes` schreibt exakt die Liste.
- PATCH /api/presets/{id}/pane_scales -> Alias auf /api/charts/{id}/pane_scales

## 3. Snapshot (Server -> Client, msg_type=snapshot.full) - NEU

```json
{
  "symbol": "NVDA", "timeframe": {...}, "bars": [...],
  "chart": {"id": "rs_chart", "display_name": "RS-Chart", "draft": false},
  "x_axis_pane": "rs",
  "panes": [
    {"pane_id":"main","role":"price","title":"SMA 10-200","weight":7,"scale":"log","preset_id":"qmaggi__main"},
    {"pane_id":"rs","role":"value","title":"RS-Monitor","weight":2,"scale":"linear","preset_id":"rs_monitor"}
  ],
  "pane_scales": {"main":"log","rs":"linear"},
  "overlays": [...], "annotations": [...], "topbar": {...}
}
```

Overlay-Erweiterungen (abwaertskompatibel, msgspec ignoriert Unbekanntes):
- `OverlayPoint.t2: int | None` - Endzeit fuer Zonen.
- `Overlay.rules: dict` - Regeln (Thresholds), informativ.
- `values[].color_override` wird jetzt auch fuer Linien und Histogramme
  ausgewertet: die Linie wird an Farbwechseln in Segmente geteilt.
- Neue Typen:
  `zone`  - Rechteck im Datenraum: t..t2 x value..value2; value/value2 null =
             volle Pane-Hoehe. Wird VOR allem anderen gezeichnet.
  `level` - horizontale Referenzlinie ueber die volle Breite (values[0].value),
             optional style.label. Wird nach den Zonen gezeichnet.
Zonen und Levels erscheinen NICHT in der Crosshair-Wertbox.

## 4. Control-Ops (Viewer-Client -> ControlService)

Es gibt bewusst nur EINE Implementierung der Chart-Lifecycle-Logik: alle Ops laufen
durch `ControlService`. Das Control Panel spricht sie ueber `control.request` an, der
MCP ueber die Bridge in `agent/command_api.py::handle_command` (Aktionen
`GET_CHART_STATE`, `LIST_WINDOWS`, `SAVE_CHART`, `APPLY_CHART`, `DIFF_CHART`,
`LIST_TOPBAR_METRICS`).

- list_panes  {}                          -> {panes:[...]}  (Katalog, mit summary)
- list_charts {}                          -> {charts:[...]}
- list_windows {}                         -> {windows:[{window_id,kind,symbol,timeframe,chart_id,
                                              display_name,draft,pane_count,topbar_metric_count}]}
- get_chart_state {window_id}             -> {window_id, chart_id, draft, symbol, timeframe,
                                              panes:[{pane_id,pane_preset_id,scale,weight,overrides}]}
- compose_chart {window_id, panes:[{pane_id?, pane_preset_id, scale?, weight?, overrides?}],
                 topbar_metrics?, base_chart_id?, draft?}  -> rendert sofort (Draft)
- apply_chart {window_id, chart_id}       -> rendert gespeichertes Chart
- save_chart {window_id, chart_id, display_name?, description?, overwrite?}
                                          -> persistiert den Draft; overwrite=false =>
                                             Fehler already_exists statt stillem PUT
- diff_chart {window_id, chart_id}        -> Strukturvergleich Fenster/Chart ohne Rendern
- list_topbar_metrics {symbol?}           -> {metrics:[{id,alias,display_name,calc_type,
                                              has_data_for_symbol}]}
- update_pane_preset {pane_preset_id, ...} (optional, Phase 2)

Ohne `topbar_metrics` erbt `compose_chart` den Metrik-Streifen und die X-Achsen-Pane
vom `base_chart_id` bzw. vom Chart des Fensters - ein Pane-Edit leert den Streifen
also nicht mehr.

Ziel des Builders ist IMMER das fokussierte Fenster (klebrig: zuletzt aktives
Chartfenster). Anwenden = Vorschau: jede Aenderung rendert sofort, gespeichert
wird nur auf Wunsch.

Die Liste "GESPEICHERTES CHART ANWENDEN" im Panel ist der **Chart-Katalog**
(`list_charts`), nicht der Alt-Endpunkt `/api/presets`: anwendbar ist genau das,
was der Builder als Chart kennt und in Panes zerlegen kann. Die Liste wird nach
jedem Speichern neu geladen; ohne eigenen Override zeigt sie das Chart, das das
Zielfenster gerade rendert. Ein nicht gespeichertes Chart traegt draft=true und wird im
Panel als "• unbenannt" markiert; es stirbt mit dem Fenster.

### 4.1 Antwort-Envelope und Fehlercodes

Bridge-Antworten sind `{ok:true, data:{...}}` bzw. `{ok:false, error:{code,message}}`.
Render-Antworten (DISPLAY_STOCK/COMPOSE_CHART/APPLY_CHART) tragen zusaetzlich
`warnings[]` und `skipped[]` - ein Render meldet nie "success", waehrend Panes
fehlen oder leer bleiben:

```json
{"status":"ok","window_id":"win_nvda_1d","bars":250,"overlays":9,
 "warnings":[{"code":"unknown_column","pane_id":"rs","detail":"gibt_es_nicht"}],
 "skipped":[{"pane_preset_id":"ghost","reason":"unknown_preset"}]}
```

Warncodes: `unknown_column`, `series_without_data`, `pane_without_series`,
`unknown_override`, `invalid_override`, `unknown_series_override`.
Fehlercodes: `invalid_request`, `unknown_window`, `unknown_pane`, `unknown_preset`,
`protected_pane`, `pane_set_mismatch`, `already_exists`, `not_found`, `chart_in_use`,
`backend_unreachable`.

### 4.2 Pane-Overrides (Chart-Ebene)

`overrides` sind ein Whitelist-Merge auf das referenzierte Pane-Preset. Das Preset
bleibt unveraendert (deepcopy); unbekannte Schluessel werden als Warnung gemeldet,
nicht still verworfen.

| Schluessel | Typ | Wirkung |
| :-- | :-- | :-- |
| `title` | str | Pane-Titel |
| `scale` | linear\|log | Default-Skala des Panes (Slot-Scale gewinnt) |
| `series` | `{canonical_id: {style:{...}, rules:{...}}}` | Deep-Merge; Style-Keys: `color`, `width`, `alpha`, `type` |
| `hide_series` | [canonical_id] | Serie nicht zeichnen |
| `refs` | Liste | ersetzt die Referenzlinien |
| `zones` | Liste | ersetzt die Zonen |

### 4.3 Topbar-Vorrang

- `chart.topbar_metrics` bestimmt den Metrik-Streifen; `COMPOSE_CHART`,
  `APPLY_CHART` und `DISPLAY_STOCK` setzen ihn aus Chart/Basis.
- `SET_TOPBAR`-Bloecke sind fensterlokal (eigene `block_id`s im Ledger) und werden
  von einem Re-Render NICHT geloescht.
- Wer den Streifen aendern will, aendert das Chart (`COMPOSE_CHART` + `SAVE_CHART`),
  nicht `SET_TOPBAR`.
- `LIST_TOPBAR_METRICS` liefert die waehlbaren Metriken (Feature-Spalten).

## 5. Server-Ledger

`layout_ledger[window_id]` erhaelt:
  `chart` = {"id": str|None, "display_name": str, "draft": bool,
              "panes": [{"pane_id","pane_preset_id","scale","weight","overrides"}],
              "topbar_metrics": [...], "x_axis_pane": str|None}
Symbol-/Timeframe-Wechsel rendert die gespeicherte Chart-Definition erneut
(kein Preset-Parameter noetig).

**Geometrie gehoert dem Client.** Der Ledger merkt sich die zuletzt gemeldete
Position/Groesse je Fenster (Quelle: `window.geometry_changed`; der Client meldet
sie auch nach jedem Oeffnen einmal aktiv). Daraus folgt:

- **Ein Re-Render schickt keine Geometrie.** `build_display_stock` erfindet keine
  Werte (kein `position or {...}`), `_render_window` und der Symbolwechsel haengen
  keine an, und `open_window` nimmt `position`/`size` nur in die Nachricht, wenn
  sie ausdruecklich angefordert wurden oder das Fenster neu angelegt wird.
- **Platzieren darf der Agent** beim Anlegen eines Fensters (Startwerte
  100,100 / 900x600) und auf ausdruecklichen Wunsch (`LOAD_SETUP`, `layout.restore`,
  ein Kommando mit `position`/`size`).
- Der Client setzt `move()`/`resize()` nur bei echter Aenderung (kein Flackern, ein
  maximiertes Fenster wird nicht restauriert).

Grund: Ein Default in der Aufloesung (100,100 / 1100x750) hat bei jedem Anwenden
jedes Fenster auf dieselbe Stelle gezogen - unabhaengig davon, wo es stand.

## 6. Setup v2

`windows[]`-Eintraege erhalten optional `chart_id`, `symbol`, `timeframe`.
Alt-Eintraege ohne diese Felder laden weiter (Geometrie-only). Beim Laden wird
ein Fenster mit chart_id + symbol + timeframe geoeffnet.

`SAVE_SETUP` (MCP/Bridge) **schreibt immer**. Die Dedup-Bremse in
`ChartAgent.save_setup` vergleicht nur die Geometrie (Fenster, Positionen,
Monitore) und ist fuer den entprellten Autosave gedacht; ein explizites Speichern
mit `force=True` umgeht sie, sonst waere "jetzt als default speichern" nach einem
reinen Chart-/Symbolwechsel ein stiller No-op.

`LIST_SETUPS` liefert je Variante die Bindungen mit (`windows`:
`window_id/chart_id/symbol/timeframe`). Daran haengt der **DELETE_CHART-Schutz**:
ohne `force` wird ein Chart nicht geloescht, das in einem gespeicherten Setup
referenziert wird - ist die Pruefung nicht moeglich (Agent nicht erreichbar),
wird ebenfalls nicht geloescht.

---

## 7. Umsetzungsstand

| Baustein | Ort |
| :-- | :-- |
| Tabellen + Backfill + RS-Monitor-Beispiel | `migrations/040_pane_chart_presets.sql` |
| Pane-/Chart-API + Alt-Kompatibilitaet | `services/pca-service/presets_api.py` |
| Aufloesung Chart -> Overlays (netzwerkfrei, testbar) | `chart_viewer/src/chart_viewer/chart_spec.py` |
| Orchestrator (Snapshot mit panes/chart/x_axis_pane) | `chart_viewer/src/chart_viewer/orchestrator.py` |
| Ledger + Setup v2 + Setup-Slot-Binding | `chart_viewer/src/chart_viewer/agent/agent_client.py` (u.a. `assign_setup_slot`), `run_server.py` |
| Builder-Ops (Fassade `execute`) | `chart_viewer/src/chart_viewer/agent/control_service.py` |
| HTTP-/MCP-Dispatch + Control-Bridge | `chart_viewer/src/chart_viewer/agent/command_api.py` |
| Pane-Overrides, Warnkanal, Diff | `chart_viewer/src/chart_viewer/chart_spec.py`, `orchestrator.py` |
| Pane-Identitaet, Rollen, Zeichenprimitive | `chart_viewer/src/chart_viewer/ui/canvas.py`, `ui/pane.py`, `core/state_manager.py` |
| Chart-Builder im Panel | `chart_viewer/src/chart_viewer/ui/control_panel.py`, `ui/app.py` |
| MCP | `mcp/agent-pca/tools/panes.ts`, `presets.ts`, `chart_viewer.ts`, `chart_builder.ts` (Lifecycle + Komfort) |
| Tests | `tests/test_command_dispatch.py`, `test_chart_warnings.py`, `test_topbar_precedence.py`, `test_setup_binding.py`, `test_chart_spec.py`, `test_control_service.py`, `test_control_panel_builder.py` |

### Bedienung

**Aus dem Agenten (MCP):**
- `manage_pane_presets` LIST/GET/CREATE/UPDATE/DELETE/ARCHIVE - Inhalt eines Panes.
- `manage_chart_presets` LIST/GET/CREATE/UPDATE/DELETE/SET_PANE_SCALE - Fensterinhalt aus Panes.
  Der Altweg (`members` ohne `panes`) funktioniert weiter und wird serverseitig uebersetzt.
- `manage_chart_viewer` DISPLAY_STOCK/OPEN_WINDOW mit `chart=<id>` bzw. `preset=<id>`;
  `LIST_CHARTS`; `COMPOSE_CHART` (Ad-hoc-Panes, Symbol kommt aus dem Ledger).
- Lifecycle (Bridge): `LIST_WINDOWS` (was ist offen?), `GET_CHART_STATE` (was steckt im
  Fenster?), `SAVE_CHART` (Draft persistieren, `overwrite:false` schuetzt), `APPLY_CHART`
  (gespeichertes Chart auf ein offenes Fenster), `DIFF_CHART` (Was wuerde sich aendern?).
- Inkrementell: `ADD_PANE`, `REMOVE_PANE`, `REORDER_PANES` (bauen auf
  `GET_CHART_STATE` + `COMPOSE_CHART` auf; Ergebnis ist ein Draft).
- Varianten: `DUPLICATE_CHART` (Kopie mit neuer Id, optional direkt anwenden),
  `DELETE_CHART` (Referenzpruefung gegen Setups, sonst Fehler; `force` uebergeht sie).
- Topbar: `LIST_TOPBAR_METRICS` (Feature-Spalten, optional symbolbezogen).
- Setups: `SETUP_ASSIGN` setzt Chart/Symbol/Timeframe eines Slots; `SAVE_SETUP`
  akzeptiert `windows`, um Bindungen direkt mitzuschreiben, `LOAD_SETUP` warnt bei
  Waisen (geloeschtes Chart).

**Im Viewer (Control Panel):** Abschnitt CHART-BUILDER.
Ziel ist das fokussierte Chartfenster (klebrig). Preispane-Preset waehlen, mit "+" weitere
Panes aus dem Dropdown anhaengen (Enter im Feld oder "+"), sortieren, LIN/LOG schalten,
Volumen zu-/abschalten (das Volumen-Pane ist eine normale Pane: was entfernt wird,
bleibt entfernt - die Definition ist die Wahrheit). Das Hinzufuegen-Dropdown listet nur Presets ohne role=price - genau
ein Preispane (Abschnitt 1) wechselt man oben; getippte Preispane-Namen werden abgewiesen.
Pro markierter Pane: **Preset** (nur Nicht-Preispane; die Slot-Id bleibt, Gewicht und
Titel-Override ebenso, die Skala zieht nur mit, wenn sie noch der Default des alten Presets
war), **Hoehe** (Gewicht) und **Titel** (landet als Override nur in diesem Chart und schlaegt
in der Liste den Preset-Namen), **⧉ Pane** dupliziert sie auf einen eigenen Slot; das
Preispane ist geschuetzt. Die Builder-Zeilen teilen eine feste Label-Spalte
(Preispane/Hinzufuegen/Preset/Hoehe/Titel).
Jede Aenderung rendert sofort (Draft, im Panel als "• unbenannt"), **Speichern** schreibt
in das aktuelle Chart, "Speichern unter..." legt ein neues an. Warnungen und
uebersprungene Panes stehen in der Statuszeile. Ohne Fensterfokus ist der Builder
deaktiviert.

**Ablauf-Synchronitaet:** Der Builder liest seinen Stand aus `get_chart_state` und
gilt nur so lange wie das Fenster. Er liest neu (a) nach dem Anwenden eines Charts
ueber die Anwenden-Liste und (b) immer dann, wenn der Agent einen neuen Snapshot
fuer das Zielfenster schickt - das deckt MCP-`APPLY_CHART`/`COMPOSE_CHART`,
Symbolwechsel, `LOAD_SETUP` und Resync ab. Doppelte Anfragen fuer dasselbe Fenster
werden zusammengefasst. Ohne diesen Abgleich zeigt der Builder die Bestandteile
des vorherigen Charts, waehrend das Fenster schon ein anderes rendert.

**Beispiel RS-Monitor:** Pane-Preset `rs_monitor` (Serien ibd_rs + rs_adr_neutral mit
Schwellenfarben 30/80, Zonen aus der 30->80-Phase in 20 Bars, Referenzlinien 30 und 80)
und Chart `rs_monitor_chart` (Preispane qmaggi SMAs, RS-Pane, Volumen).

### Bewusste Grenzen

- Ein Instrument pro Fenster; alle Panes teilen Bars und X-Achse. Spread-/Paar-Charts
  bleiben eigene Fenster (DISPLAY_SERIES).
- `kind=custom` mit `renderer` ist im Modell vorgesehen, aber noch nicht implementiert -
  das RS-Monitor-Beispiel kommt ohne eigenen Renderer aus (Serien + Regeln + Zonen).
- Pane-Presets werden referenziert, nicht kopiert; Anpassungen landen im Chart-Draft.
  Ein Pane-Preset aendert sich nur ueber `manage_pane_presets`.
- Pane-Hoehen sind Gewichte (Splitter-Stretch) im Chart; im Panel editierbar.
  Ein Ziehen im Fenster bleibt lokal; das Uebernehmen der Ziehhoehen ins Chart-Gewicht
  braucht einen eigenen Viewer->Agent-Kanal und ist offen.
- `kind=custom`-Renderer, Mehrfach-Instrumente pro Fenster und Spread-Charts bleiben
  eigene Fenster (DISPLAY_SERIES).

