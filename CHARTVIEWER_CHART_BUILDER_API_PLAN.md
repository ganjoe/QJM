# Implementationsplan: Chart-Builder API & Lifecycle

Status: **Umgesetzt (2026-09-28)** - siehe Abschnitt 11 fuer Artefakte, Abweichungen und die Verifikation am lebenden System
Geltungsbereich: Chart-Agent (`chart-viewer-server`), Control-Service, MCP-Tool `openbrain-pca/manage_chart_viewer`, Control-Panel-Anbindung
Referenz: [docs/architecture/chart-presets.md](docs/architecture/chart-presets.md) (Vertrag), [CHARTVIEWER_CONTROL_PANEL_PLAN.md](CHARTVIEWER_CONTROL_PANEL_PLAN.md) (Panel), [mcp/agent-pca/tools/chart_viewer.ts](mcp/agent-pca/tools/chart_viewer.ts)

Ziel in einem Satz: Der Agent soll ein Chartfenster genauso vollständig **bauen, lesen, ändern, speichern, klonen, vergleichen und verwalten** können wie das Control Panel – ohne Bruchstellen und ohne stille Fehler.

---

## 1. Befund (am Code verifiziert)

| # | Lücke | Ist-Zustand | Beleg |
| :-- | :-- | :-- | :-- |
| B1 | Lifecycle-Ops nicht erreichbar | `get_chart_state`, `apply_chart`, `save_chart`, `list_panes`, `list_charts` existieren vollständig im `ControlService`, werden aber nur über `control.request` (WebSocket/Qt-Panel) ausgeführt. Der MCP-Pfad läuft über `POST /api/command` mit handgeschriebenem Dispatch, der für Chart-Inhalte nur `DISPLAY_STOCK`/`COMPOSE_CHART` kennt. | [control_service.py:127-155](chart_viewer/src/chart_viewer/agent/control_service.py#L127-L155), [run_server.py:143](chart_viewer/src/chart_viewer/run_server.py#L143), [run_server.py:285](chart_viewer/src/chart_viewer/run_server.py#L285), [chart_viewer.ts:1305](mcp/agent-pca/tools/chart_viewer.ts#L1305) |
| B2 | Brücke ist billig | `run_server` erzeugt den `ChartAgent` im **selben Prozess** – `agent.control_service` ist vom HTTP-Handler direkt erreichbar. | [run_server.py:48](chart_viewer/src/chart_viewer/run_server.py#L48) |
| B3 | `overrides` ist ein toter Vertrag | MCP-Schema bewirbt `overrides`, `compose_chart` und Orchestrator reichen sie durch, `normalize_chart_spec` baut den Pane-Eintrag **ohne** `overrides` → sie werden verworfen. Die DB kann sie bereits (kein Migrationsbedarf). | [chart_viewer.ts:504](mcp/agent-pca/tools/chart_viewer.ts#L504), [control_service.py:612](chart_viewer/src/chart_viewer/agent/control_service.py#L612), [orchestrator.py:136](chart_viewer/src/chart_viewer/orchestrator.py#L136), [chart_spec.py:58-66](chart_viewer/src/chart_viewer/chart_spec.py#L58-L66), [presets_api.py:341](services/pca-service/presets_api.py#L341) |
| B4 | Stille Pane-Verluste | Unbekanntes Pane-Preset wird geloggt und übersprungen; der MCP meldet trotzdem `success`. Vorbild ist `DISPLAY_SERIES` mit `skipped`. | [orchestrator.py:124-129](chart_viewer/src/chart_viewer/orchestrator.py#L124-L129), [chart_viewer.ts:1323](mcp/agent-pca/tools/chart_viewer.ts#L1323), [chart_viewer.ts:1255](mcp/agent-pca/tools/chart_viewer.ts#L1255) |
| B5 | Warnkanal fehlt | `build_overlays` sammelt `missing` nur metrikbezogen und gibt es nicht strukturiert an den Agenten weiter. | [chart_spec.py:254](chart_viewer/src/chart_viewer/chart_spec.py#L254), [chart_spec.py:307-308](chart_viewer/src/chart_viewer/chart_spec.py#L307-L308), [chart_spec.py:408-413](chart_viewer/src/chart_viewer/chart_spec.py#L408-L413) |
| B6 | Topbar: Discovery + Vorrang | Metrik = Feature-Spalte; `SET_TOPBAR` ist ein fensterlokaler Side-Channel mit eigenem Ledger-Speicher, `COMPOSE_CHART`/`APPLY` setzen `topbar_metrics` aus Chart/Basis. Vorrang ist nirgends festgelegt. Feature-Katalog existiert. | [orchestrator.py:562](chart_viewer/src/chart_viewer/orchestrator.py#L562), [run_server.py:251-271](chart_viewer/src/chart_viewer/run_server.py#L251-L271), [control_service.py:730-746](chart_viewer/src/chart_viewer/agent/control_service.py#L730-L746), [chart_data.py:185](services/pca-service/chart_data.py#L185), [chart_data.py:314](services/pca-service/chart_data.py#L314) |
| B7 | Setup v2 ist weiter als angenommen | `SAVE_SETUP` persistiert pro Fenster `chart_id`/`chart_name`/`chart_panes`/`symbol`/`timeframe`/`topbar_metrics`, `LOAD_SETUP` rendert sie wieder. Es fehlt nur **explizites** Zuweisen/Ändern. | [agent_client.py:605-622](chart_viewer/src/chart_viewer/agent/agent_client.py#L605-L622), [run_server.py:574-595](chart_viewer/src/chart_viewer/run_server.py#L574-L595) |
| B8 | Kein Schutz gegen stilles Überschreiben | `save_chart` entscheidet per `GET` → `PUT`/`POST` ohne Revisionsprüfung; parallele Läufe überschreiben sich lautlos. | [control_service.py:783-791](chart_viewer/src/chart_viewer/agent/control_service.py#L783-L791) |
| B9 | Kein Testnetz für den HTTP-Dispatch | Der Dispatch steckt in `ControlHandler.do_POST` (verschachtelt, Socket-gebunden) und ist damit nicht ohne Server testbar. | [run_server.py:142-722](chart_viewer/src/chart_viewer/run_server.py#L142-L722) |

### 1.1 Abdeckung der Anwender-Analyse

| Analyse-Punkt | Phase | Anmerkung |
| :-- | :-- | :-- |
| 1 SAVE_CHART | 1 | Fachlogik existiert ([control_service.py:763](chart_viewer/src/chart_viewer/agent/control_service.py#L763)) |
| 2 APPLY_CHART | 1 | Fachlogik existiert ([control_service.py:753](chart_viewer/src/chart_viewer/agent/control_service.py#L753)) |
| 3 GET_CHART_STATE | 1 | Fachlogik existiert ([control_service.py:681](chart_viewer/src/chart_viewer/agent/control_service.py#L681)) + `LIST_WINDOWS` neu |
| 4 ADD/REMOVE/REORDER_PANE | 2 | Read-Modify-Write im MCP, kein neuer Server-Op |
| 5 DUPLICATE_CHART | 2 | GET + POST mit Id-Remap |
| 6 DELETE_CHART im Viewer-Tool | 2 | Wrapper auf vorhandenen PCA-Endpoint |
| 7 Preview/Diff | 2 | hochgestuft: gleiche Datenform ⇒ reiner Strukturvergleich |
| 8 Topbar-Metriken | 5 | Discovery + dokumentierter Vorrang |
| 9 Setup-Slot-Binding | 6 | Persistenz existiert (B7), nur Zuweisung fehlt |
| — `overrides` | 3 | zusätzlicher Befund |
| — Warnungen/skipped | 4 | zusätzlicher Befund |
| — Panel-Ausbau (Runde 1) | 7 | Kern-Funktionen, Rest als Follow-up |

---

## 2. Design-Entscheidungen (Leitplanken)

| # | Entscheidung | Begründung |
| :-- | :-- | :-- |
| D1 | **Single Facade**: `ControlService` bleibt die einzige Stelle mit Chart-Lifecycle-Fachlogik. Neu: öffentliche, synchrone Methode `ControlService.execute(op, params) -> dict`, die dieselben Handler benutzt wie `handle()`. `run_server` delegiert nur. | verhindert eine zweite Implementierung, die auseinanderläuft (heute schon zwei Pfade) |
| D2 | **Dünner Server, Komfort im MCP**: Server bekommt `list_windows` + die Bridge; ADD/REMOVE/REORDER/DUPLICATE/DIFF leben im MCP-Tool aus GET + COMPOSE. | kein Protokollwachstum, kleine testbare Einheiten, Fensterinhalt ist klein |
| D3 | **`overrides` mit Whitelist** statt Freiform: definierte Schlüssel, Deep-Merge, unbekannte Schlüssel ⇒ Warnung (kein stilles Verwerfen). | der Vertrag muss halten, was das Schema verspricht |
| D4 | **Erfolg ist nie still**: jede Compose-/Apply-/Display-Antwort trägt `warnings[]` und `skipped[]`. | Agentenfehler sollen sichtbar sein, nicht als „success" durchgehen |
| D5 | **Additiv & abwärtskompatibel**: bestehende Actions, Ops und Antwortfelder bleiben; neue Felder sind optional. | Panel und Alt-Tools dürfen nicht brechen |
| D6 | **Keine DB-Migration**: `overrides` (jsonb) und Setup v2 Felder existieren bereits. | [migrations/040_pane_chart_presets.sql](migrations/040_pane_chart_presets.sql) |

---

## 3. Phasen

### Phase 0 — Vertrag & Testnetz (Größe S, keine Verhaltensänderung)

**Ziel:** Die neuen Verträge stehen, bevor Code entsteht; der HTTP-Dispatch wird testbar.

1. **Dispatch extrahieren** (Voraussetzung für alle Tests): neue Datei `chart_viewer/src/chart_viewer/agent/command_api.py` mit
   ```python
   def handle_command(cmd: dict, agent: ChartAgent) -> dict: ...
   ```
   Der komplette Rumpf aus [run_server.py:142-706](chart_viewer/src/chart_viewer/run_server.py#L142-L706) zieht dorthin um; `ControlHandler.do_POST` liest nur noch den Body, ruft `handle_command` und schreibt die Antwort. **Verhaltensgleich**, kein Logikwechsel.
2. **Vertrag erweitern** in [docs/architecture/chart-presets.md](docs/architecture/chart-presets.md):
   - §4 Control-Ops: `list_windows`, Response-Envelope inkl. `warnings`/`skipped`, Fehlercodes.
   - neuer Abschnitt „Pane-Overrides" (Whitelist aus Phase 3).
   - neuer Absatz „Topbar-Vorrang" (Phase 5).
3. **Rote Tests** anlegen (siehe §5) – sie dokumentieren den Zielvertrag und bleiben bis zur jeweiligen Phase rot.

**DoD:** `pytest chart_viewer/tests` bleibt grün (Refactor verhaltensgleich); neue Tests existieren und schlagen aus dem erwarteten Grund fehl.

---

### Phase 1 — Window-Lifecycle-Brücke (Größe M) · Analyse-Punkte 1, 2, 3

**Ziel:** `GET_CHART_STATE`, `SAVE_CHART`, `APPLY_CHART`, `LIST_WINDOWS` sind vom MCP aus erreichbar.

**Server**
1. `ControlService.execute(op, params) -> dict` in [control_service.py](chart_viewer/src/chart_viewer/agent/control_service.py) (bei `handle`, ~:144):
   ```python
   def execute(self, op: str, params: Any) -> Dict[str, Any]:
       """Synchroner Fassadeneinstieg (MCP/HTTP). Gleiche Handler wie handle()."""
       handler = self._ops.get(op)
       if handler is None:
           return {"ok": False, "error": {"code": "invalid_request", "message": f"Unknown control op '{op}'"}}
       try:
           return {"ok": True, "data": handler(params if isinstance(params, dict) else {})}
       except ControlError as e:
           return {"ok": False, "error": {"code": e.code, "message": str(e)}}
       except SupabaseError as e:
           return {"ok": False, "error": {"code": "backend_unreachable", "message": str(e)}}
   ```
   `handle()` wird auf denselben Fehlermapping-Pfad gezogen (kein dupliziertes Mapping).
2. **Neuer Op `list_windows`** (`_op_list_windows`, registriert im `_ops`-Dict :127): pro Ledger-Eintrag
   ```json
   {"window_id":"win_nvda_1d","symbol":"NVDA","timeframe":"1D","chart_id":"rs_monitor_chart",
    "display_name":"RS-Monitor","draft":false,"pane_count":3,"topbar_metric_count":4,"kind":"chart"}
   ```
   Watchlist-Fenster werden mit `"kind":"watchlist"` und ohne Chart-Felder gelistet (kein Fehler).
3. **Dispatch** in `command_api.handle_command`:
   ```python
   if action in ("GET_CHART_STATE", "SAVE_CHART", "APPLY_CHART", "LIST_WINDOWS"):
       op = action.lower()
       params = {k: v for k, v in cmd.items() if k not in ("action",)}
       result = agent.control_service.execute(op, params)
   ```
   HTTP-Status bleibt 200; Fehler stehen als `{"ok": false, "error": {...}}` im Body (MCP wirft daraus einen Tool-Fehler).

**MCP** ([chart_viewer.ts](mcp/agent-pca/tools/chart_viewer.ts))
4. Drei Aktionen + Parameter ergänzen; Action-Enum :425-443, Schema :499-507, Doku-Text :404-423:
   - `GET_CHART_STATE {window_id}` → JSON (identisch zur Panel-Antwort; `panes` in Reihenfolge).
   - `LIST_WINDOWS {}` → Tabelle `window_id | symbol | timeframe | chart_id | draft | panes`.
   - `APPLY_CHART {window_id, chart_id}` → rendert gespeichertes Chart auf das offene Fenster.
   - `SAVE_CHART {window_id, chart_id, display_name?, description?, overwrite?}` → persistiert den Draft.
     `overwrite: false` + existierendes Chart ⇒ Fehler `already_exists` statt stillem PUT (B8).
5. Antwortformat: bei `ok:false` `isError: true` mit `error.code`/`error.message`; bei Erfolg kompakter Text + `result`.

**Tests** (siehe §5)
- `test_command_dispatch.py`: alle vier Aktionen gegen `FakeAgent` + `FakeControlService`.
- `test_control_service.py`: `execute()`-Fehlermapping; `list_windows` mit Chart- und Watchlist-Fenster; `save_chart` mit `overwrite=false`.

**DoD:** Ein Agent kann ein offenes Fenster inspizieren, seinen Draft speichern und ein anderes Chart darauf anwenden – je ein Aufruf, ohne Vorwissen.

---

### Phase 2 — MCP-Komfortops (Größe M) · Analyse-Punkte 4, 5, 6, 7

**Ziel:** Inkrementelles Bauen und Vergleichen ohne Full-Replace-Denken.

Alle Ops sind **MCP-seitig** implementiert (D2) und benutzen ausschließlich Phase-1-Aktionen.

| Action | Parameter | Verhalten |
| :-- | :-- | :-- |
| `ADD_PANE` | `window_id, pane_preset_id, after_pane_id?, pane_id?, weight?, scale?` | GET_CHART_STATE → Liste an Position einfügen → COMPOSE_CHART. Positionierung: hinter `after_pane_id`, sonst vor dem Volumen-Pane, sonst ans Ende. |
| `REMOVE_PANE` | `window_id, pane_id` | Pane `main` ist nicht entfernbar (`error: protected_pane`); fehlende `pane_id` ⇒ `error: unknown_pane`. |
| `REORDER_PANES` | `window_id, pane_ids[]` | Menge muss exakt der aktuellen entsprechen (sonst `error: pane_set_mismatch`); `main` muss auf Position 0 bleiben. |
| `DUPLICATE_CHART` | `source_chart_id, new_chart_id, display_name?, apply_to_window?` | `GET /api/charts/{src}` → `POST /api/charts` mit neuer Id; optional direkt auf ein Fenster anwenden. |
| `DELETE_CHART` | `chart_id, force?` | `DELETE /api/charts/{id}`. Ohne `force` vorher Referenzprüfung gegen gespeicherte Setups; Treffer ⇒ Fehler mit Liste statt Löschen. |
| `DIFF_CHART` | `window_id, chart_id` | Strukturvergleich Window-State vs. Chart-Definition, **ohne** zu rendern. |

**DIFF_CHART Antwortform**
```json
{"same": false,
 "added":   [{"pane_id":"adr","pane_preset_id":"adr_pane"}],
 "removed": [{"pane_id":"vol2","pane_preset_id":"builtin:volume"}],
 "reordered": [{"from":2,"to":1,"pane_id":"rs"}],
 "changed": [{"pane_id":"main","fields":{"scale":{"window":"linear","chart":"log"},
                                           "weight":{"window":7,"chart":5}}}],
 "topbar_metrics": {"window":["close","ibd_rs"],"chart":["close"]},
 "x_axis_pane": {"window":"main","chart":"main"}}
```

**Tests:** `test_chart_builder_ops.ts` entfällt (keine TS-Testinfra) → stattdessen MCP-Smoke-Skript unter `scripts/` (siehe §5.3) plus Python-Tests für die reine Diff-Logik, falls sie nach `services/pca-service` oder eine kleine Helper-Datei wandert. **Empfehlung:** Diff-Berechnung als reine Funktion in `chart_viewer/chart_spec.py` (`diff_chart_specs(window_panes, chart_panes)`) implementieren und via neuem Control-Op `diff_chart` erreichbar machen – dann ist sie pytestbar und der MCP-Teil bleibt dünn.

**DoD:** „Füge unter dem RS-Monitor einen ADR-Pane hinzu" ist **ein** Aufruf; „was ändert sich, wenn ich Chart X anwende" ist ein Aufruf ohne Seiteneffekt.

---

### Phase 3 — `overrides` wirklich anwenden (Größe M) · Befund B3

**Ziel:** Pane-Anpassungen pro Chart funktionieren und sind dokumentiert; Presets bleiben unangetastet.

1. `normalize_chart_spec` ([chart_spec.py:45-107](chart_viewer/src/chart_viewer/chart_spec.py#L45-L107)) behält `overrides` im Pane-Eintrag (heute verworfen).
2. Neue reine Funktion:
   ```python
   def apply_pane_overrides(preset: dict, overrides: dict) -> tuple[dict, list[dict]]:
       """Whitelist-Merge; gibt (preset', warnings) zurueck."""
   ```
3. **Whitelist** (Vertrag, §3 Phase 0 dokumentiert):

| Schlüssel | Typ | Wirkung |
| :-- | :-- | :-- |
| `title` | str | Pane-Titel im Snapshot (`panes_meta.title`) |
| `scale` | "linear"\|"log" | überschreibt Preset-Default |
| `series` | `{canonical_id: {style: {...}, rules: {...}}}` | Deep-Merge auf die Serie; erlaubte Style-Keys: `color, width, alpha, type` |
| `hide_series` | `[canonical_id]` | Serie wird nicht gezeichnet |
| `refs` | Liste | **ersetzt** die Referenzlinien des Presets |
| `zones` | Liste | **ersetzt** die Zonen des Presets |

   Alles andere ⇒ `warning{code:"unknown_override", pane_id, detail}`, kein Crash, kein stilles Verwerfen.
4. `build_overlays` wendet `apply_pane_overrides` je Pane an, bevor Serien/Refs/Zonen gelesen werden ([chart_spec.py:258-405](chart_viewer/src/chart_viewer/chart_spec.py#L258-L405)).
5. Speichern: `save_chart` reicht `overrides` bereits durch (`_clean_pane_specs` :612) und `presets_api` persistiert sie (:341/:412/:487/:562) → kein weiterer Schritt nötig, aber **Round-Trip-Test** ergänzen.

**Tests:** `test_chart_spec.py` – Farbe/Threshold per Override, `hide_series`, `refs`-Ersetzung, unbekannter Key ⇒ Warnung; Round-Trip `save → load → render` erhält Overrides.

**DoD:** „Nimm den RS-Monitor, aber zeichne `ibd_rs` grau und verstecke `rs_adr_neutral`" funktioniert über `overrides` – sichtbar im Snapshot, ohne das Preset zu ändern.

---

### Phase 4 — Warnungen & `skipped` (Größe S–M) · Befunde B4, B5

**Ziel:** Kein `success` für ein halbes Chart.

1. `build_overlays` liefert zusätzlich `warnings: [{code, pane_id?, detail}]`:
   - `unknown_column` (heute nur `missing`),
   - `series_without_data`,
   - `unknown_override` (Phase 3),
   - `pane_without_series` (Pane würde leer bleiben).
   `missing` bleibt als Feld erhalten (Abwärtskompatibilität, wird im Topbar verwendet).
2. `build_inline_chart_spec` / `build_display_stock` ([orchestrator.py:113-146](chart_viewer/src/chart_viewer/orchestrator.py#L113-L146)): unbekanntes Pane-Preset wandert nach `skipped: [{pane_preset_id, reason:"unknown_preset"}]` statt nur ins Log.
3. Transport: `command_api.handle_command` gibt `warnings`/`skipped` im Ergebnis zurück; Snapshot trägt sie optional mit (`chart.warnings`).
4. MCP: eigener Block „⚠︎ Warnungen" in der Antwort, **auch** bei `status: success`; `skipped` bei `DISPLAY_STOCK`, `COMPOSE_CHART` und `APPLY_CHART`.

**Tests:** Chart mit einem unbekannten und einem gültigen Pane ⇒ Fenster rendert, Antwort enthält `skipped` mit genau einem Eintrag; Serie ohne Daten ⇒ `warnings`.

**DoD:** Kein Renderpfad meldet Erfolg, während Teile fehlen.

---

### Phase 5 — Topbar: Discovery + Vorrang (Größe S) · Analyse-Punkt 8, Befund B6

1. **`LIST_TOPBAR_METRICS`** (MCP-Action, agent-seitig über PCA): Quelle `GET /api/features/registry` (+ `/api/features/schema`), optional `symbol` ⇒ je Metrik `{id, display_name, has_data_for_symbol}`. Antwort als Tabelle.
2. **Vorrangregel** (festschreiben, dokumentieren, testen):
   - `chart.topbar_metrics` bestimmt den **Metrik-Streifen**; `COMPOSE_CHART`/`APPLY_CHART`/`DISPLAY_STOCK` setzen ihn aus der Chart-Definition bzw. dem `topbar_metrics`-Parameter.
   - `SET_TOPBAR`-Blöcke sind **fensterlokal** (eigene `block_id`s im Ledger) und werden von einem Re-Render **nicht** gelöscht.
   - Wer den Streifen ändern will, ändert das Chart (SAVE_CHART nach COMPOSE) – nicht `SET_TOPBAR`.
3. Doku: Absatz in [docs/architecture/chart-presets.md](docs/architecture/chart-presets.md) + Klarstellung im Tool-Text.

**Tests:** `SET_TOPBAR` → `COMPOSE_CHART` ⇒ Block weiterhin im Ledger und im Snapshot; `COMPOSE` ohne `topbar_metrics` erbt vom Chart/Basis, nicht vom Fenster.

**DoD:** Ein Agent weiß, welche Metriken es gibt und welche Regel gilt.

---

### Phase 6 — Setup-Slot-Binding (Größe S–M) · Analyse-Punkt 9, Befund B7

1. `ChartAgent.assign_setup_slot(setup_name, *, monitor_count=None, window_id=None, slot=None, chart_id=None, symbol=None, timeframe=None)` in [agent_client.py](chart_viewer/src/chart_viewer/agent/agent_client.py) – schreibt in die gespeicherte Variante (Struktur aus `save_setup`, :547/:605-622); setzt nur die angegebenen Felder.
2. MCP-Action `SETUP_ASSIGN` mit obigen Parametern; `SAVE_SETUP` bekommt optional `windows: [{window_id, chart_id, symbol, timeframe}]`, um Bindungen schon beim Speichern zu pinnen.
3. Waisen-Check beim Laden: `chart_id` existiert nicht mehr ⇒ Warnung im `LOAD_SETUP`-Ergebnis (Phase-4-Kanal), Fenster öffnet trotzdem.

**Tests:** speichern → Slot ändern → laden ⇒ Fenster hat das neue Chart/Symbol; gelöschtes Chart ⇒ Warnung, kein Abbruch.

**DoD:** „Setup 'desk' Slot 3 soll Chart `momentum` mit NVDA zeigen" ist ein Aufruf ohne Fenster-Shuffle.

---

### Phase 7 — Control-Panel-Anschluss, Kern (Größe M, parallel möglich)

Nur was die neuen Ops direkt braucht; der Rest der Runde-1-Liste bleibt Follow-up.

| Element | nutzt | Aufwand |
| :-- | :-- | :-- |
| Pane-Gewicht editierbar + „Höhen aus Fenster übernehmen" (Splittergrößen → `weight`) | `compose_chart` (weight existiert) | S |
| „Speichern" in-place neben „Speichern unter…" | Phase-1-`save_chart` | S |
| Warnungsblock im Panel statt Statuszeile | Phase 4 | S |
| Pane duplizieren + Titel ändern | Phase-2-Semantik lokal | S |

**Follow-up (eigenes Ticket, nicht hier):** Katalog-Suche mit Vorschau, Undo/Redo, Multi-Select/Shortcuts, Chart-Lifecycle im Panel (Umbenennen/Löschen/Duplizieren), Fenster-aus-Chart-öffnen.

---

### Phase 8 — Doku & Politur (Größe S)

1. [docs/architecture/chart-presets.md](docs/architecture/chart-presets.md): §4 Control-Ops (neu: `list_windows`, `diff_chart`; Envelope mit `warnings`/`skipped`), Overrides-Schema, Topbar-Vorrang, §7 Umsetzungsstand-Tabelle aktualisieren, §8 MCP-Action-Tabelle.
2. `chart_viewer.ts`: Beschreibungstext um die neuen Aktionen und den Topbar-Vorrang ergänzen.
3. Verifikations-Walkthrough aus §5.3 als Anhang dieses Plans abhaken.

---

## 4. Verträge (Ziel)

### 4.1 Neue/erreichbar gemachte Ops

```
GET_CHART_STATE {window_id}                    -> {window_id, symbol, timeframe, chart_id, display_name,
                                                   draft, topbar_metrics, x_axis_pane,
                                                   panes:[{pane_id,pane_preset_id,scale,weight,overrides}]}
LIST_WINDOWS    {}                             -> {windows:[{window_id,symbol,timeframe,chart_id,
                                                   display_name,draft,pane_count,kind}]}
SAVE_CHART      {window_id, chart_id, display_name?, description?, overwrite?}
                                               -> {window_id, chart_id, saved:true, created:bool}
APPLY_CHART     {window_id, chart_id}          -> {window_id, chart_id, render:{...}, warnings:[]}
DIFF_CHART      {window_id, chart_id}          -> siehe Phase 2
LIST_TOPBAR_METRICS {symbol?}                  -> {metrics:[{id,display_name,has_data_for_symbol}]}
SETUP_ASSIGN    {setup_name, monitor_count?, window_id?, slot?, chart_id?, symbol?, timeframe?}
                                               -> {setup_name, monitor_count, slot, changed:[...]}
```

### 4.2 Antwort-Envelope (MCP-sichtbar)

```json
{"ok": true,
 "data": { "...": "op-spezifisch" },
 "warnings": [{"code":"unknown_column","pane_id":"rs","detail":"rs_adr_neutral"}],
 "skipped":  [{"pane_preset_id":"foo","reason":"unknown_preset"}]}
```
```json
{"ok": false, "error": {"code":"unknown_window","message":"Kein Chartfenster 'win_x_1d' offen."}}
```

**Fehlercodes:** `invalid_request`, `unknown_window`, `unknown_pane`, `unknown_preset`, `protected_pane`, `pane_set_mismatch`, `already_exists`, `not_found`, `chart_in_use`, `backend_unreachable`.

### 4.3 Kompatibilität

- Bestehende Aktionen/Antworten unverändert; `warnings`/`skipped` sind zusätzliche Felder.
- `COMPOSE_CHART` ohne `overrides` verhält sich exakt wie heute.
- Panel-Pfad (`control.request`) profitiert automatisch, weil er dieselben Handler nutzt.

---

## 5. Testplan

### 5.1 Unit (pytest, `chart_viewer/tests`)

| Datei | Neue Fälle |
| :-- | :-- |
| `test_command_dispatch.py` (neu) | `handle_command` für GET_CHART_STATE/LIST_WINDOWS/SAVE_CHART/APPLY_CHART; unbekannte Action; Fehlerabbildung `ok:false` |
| `test_control_service.py` | `execute()`-Erfolg/Fehler; `list_windows` (Chart + Watchlist + leerer Ledger); `save_chart` `overwrite=false` ⇒ `already_exists`; Overrides-Round-Trip |
| `test_chart_spec.py` | `apply_pane_overrides` (Farbe, hide_series, refs-Ersetzung, unbekannter Key ⇒ Warnung); `normalize_chart_spec` behält Overrides; `diff_chart_specs` (added/removed/reordered/changed) |
| `test_chart_warnings.py` (neu) | unbekanntes Preset ⇒ `skipped`; Serie ohne Daten ⇒ `warning`; Erfolg bleibt Erfolg |
| `test_topbar_precedence.py` (neu) | SET_TOPBAR-Block überlebt COMPOSE/APPLY; `topbar_metrics` folgt Chart/Basis |
| `test_setup_binding.py` (neu) | `assign_setup_slot` ändert nur die genannten Felder; Waisen-Warnung beim Laden |

Test-Harness: `FakeAgent`/`FakeTransport`/`ControlConfig` aus [test_control_service.py:19-60](chart_viewer/tests/test_control_service.py#L19-L60); HTTP-Aufrufe wie dort über injizierte `_pca_json`/Monkeypatch.

### 5.2 Regression

`pytest chart_viewer/tests` vollständig; besonders `test_control_panel_builder.py`, `test_pane_layout.py`, `test_pane_render.py`, `test_pane_scale.py`, `test_window_lifecycle.py`, `test_topbar_restore.py`. Zusätzlich die PCA-Service-Tests (`services/pca-service/test_preset_pane_scales.py`).

### 5.3 Verifikation am lebenden System (manuell, wird in Phase 8 abgehakt)

1. `DISPLAY_STOCK NVDA chart=rs_monitor_chart` → `LIST_WINDOWS` zeigt das Fenster mit `chart_id`.
2. `COMPOSE_CHART` mit einem unbekannten Preset ⇒ Antwort enthält `skipped`, Fenster rendert den Rest.
3. `SAVE_CHART` mit neuer Id ⇒ `LIST_CHARTS` zeigt sie; `GET_CHART_STATE` zeigt `draft:false`.
4. `DUPLICATE_CHART` ⇒ Variante existiert; `DIFF_CHART` zwischen Fenster und Variante zeigt genau die eine geänderte Sache.
5. `ADD_PANE` ADR unter den RS-Monitor ⇒ Reihenfolge und Gewicht im Screenshot prüfen (`SCREENSHOT`).
6. `overrides` (Farbe/Hide) ⇒ Screenshot-Vergleich vorher/nachher.
7. `SET_TOPBAR` → `APPLY_CHART` ⇒ Block noch sichtbar.
8. `SETUP_ASSIGN` → `SAVE_SETUP` → `LOAD_SETUP` ⇒ richtiges Chart/Symbol pro Slot.

MCP-Tool hat keine TS-Testinfrastruktur (`mcp/agent-pca` = Deno, keine Tests) → die Schritte 1–8 sind der Ersatz; optional ein Smoke-Skript `scripts/chart_builder_smoke.sh` mit `curl` gegen `:8766/api/command`.

---

## 6. Risiken & Gegenmaßnahmen

| Risiko | Wirkung | Gegenmaßnahme |
| :-- | :-- | :-- |
| Zweite Fachlogik im `run_server`-Dispatch | Ops driften auseinander | D1: nur Delegation, Review-Kriterium „kein neues SQL/HTTP im Dispatch" |
| `overrides`-Whitelist wird zur zweiten API | Pflegeaufwand, Überraschungen | bewusst klein halten; unbekannte Keys werden gewarnt, nicht interpretiert |
| Warnungen fluten die Tool-Antwort | Agentenkontext wächst | Warnungen aggregieren (`code` + Zähler) und in der MCP-Antwort kappen (Top 10 + „… n weitere") |
| `overwrite=false` bricht bestehende Aufrufer | Regressionsrisiko | Default `overwrite=true` (heutiges Verhalten), Schutz nur opt-in |
| `DELETE_CHART` löscht referenzierte Charts | Setup lädt ins Leere | Referenzprüfung als Default; `force` nötig |
| Topbar-Regel ändert sichtbares Verhalten | Nutzerüberraschung | in Phase 5 zuerst Test + Doku, dann Verhalten |
| Dispatch-Refactor (Phase 0) verändert Verhalten | schwer zu findende Regression | reiner Umzug, Tests müssen vorher/nachher identisch grün sein |

---

## 7. Reihenfolge & Aufwand

| Phase | Inhalt | Größe | Abhängigkeit | Ergebnis |
| :-- | :-- | :-- | :-- | :-- |
| 0 | Vertrag + Dispatch-Extraktion + Testnetz | S | — | testbarer Pfad, definierter Vertrag |
| 1 | Lifecycle-Brücke + 4 MCP-Aktionen | M | 0 | **blinde Flecken weg** |
| 2 | ADD/REMOVE/REORDER/DUPLICATE/DELETE/DIFF | M | 1 | inkrementelles Bauen |
| 3 | `overrides` | M | 0 | Pane-Anpassung pro Chart |
| 4 | Warnungen/`skipped` | S–M | 3 | keine stillen Halb-Charts |
| 5 | Topbar Discovery + Vorrang | S | 1 | Topbar planbar |
| 6 | Setup-Slot-Binding | S–M | 1 | reproduzierbare Setups |
| 7 | Panel-Kern | M | 1, 4 | Panel zieht gleich |
| 8 | Doku + Walkthrough | S | alle | abgeschlossen |

**Empfohlener Schnitt:** 0 → 1 → 4 → 3 → 2 → 5 → 6 → 7 → 8. Phase 4 vor 3, damit Overrides-Warnungen gleich im Warnkanal landen; Phase 2 nach 3, weil `ADD_PANE` mit Overrides sinnvoller wird.

---

## 8. Definition of Done

- [ ] `GET_CHART_STATE`, `LIST_WINDOWS`, `SAVE_CHART`, `APPLY_CHART` sind vom MCP aus aufrufbar und getestet.
- [ ] `ADD_PANE`, `REMOVE_PANE`, `REORDER_PANES`, `DUPLICATE_CHART`, `DELETE_CHART`, `DIFF_CHART` funktionieren ohne Full-Replace-Denken.
- [ ] `overrides` wirken im Renderer, sind dokumentiert und round-trippen durch die DB.
- [ ] Jede Render-Antwort trägt `warnings`/`skipped`; der MCP zeigt sie sichtbar.
- [ ] Topbar: Metrikliste abrufbar, Vorrang dokumentiert und getestet.
- [ ] Setup-Slots sind explizit bindbar; Waisen erzeugen eine Warnung.
- [ ] `pytest chart_viewer/tests` + PCA-Tests grün, keine Regression im Control-Panel-Pfad.
- [ ] `docs/architecture/chart-presets.md` beschreibt alle neuen Ops, Overrides und den Topbar-Vorrang.
- [ ] Verifikations-Walkthrough (§5.3) abgehakt, Screenshots in `dsh_playground/` abgelegt.

---

## 9. Offene Entscheidungen (vor Phase 1 zu klären)

1. **`overrides`-Umfang**: Whitelist wie in §3 Phase 3, oder zusätzlich `visible: false` für ganze Panes (Pane unterdrücken statt entfernen)?
2. **`DIFF_CHART`-Ort**: reine Funktion in `chart_spec.py` + Control-Op (empfohlen, pytestbar) oder ausschließlich MCP-seitig?
3. **`DELETE_CHART`-Semantik**: hartes DELETE (wie `manage_chart_presets DELETE`) oder Soft-Archiv wie bei Pane-Presets (`archived=true`)?
4. **`SET_TOPBAR`-Zukunft**: als fensterlokaler Override beibehalten (Regel §3 Phase 5) oder langfristig in die Chart-Definition überführen?
5. **Panel-Umfang Phase 7**: reicht der Kern (Gewicht, Speichern in-place, Warnungen, Duplizieren), oder soll der Katalog-/Undo-Ausbau mit hinein?

---

## 10. Nicht in diesem Plan

- `kind=custom` mit eigenem Renderer (im Modell vorgesehen, nicht implementiert).
- Spread-/Paar-Charts (bleiben eigene Fenster über `DISPLAY_SERIES`).
- Mehrere Instrumente pro Fenster.
- Persistenz manueller Splitter-Änderungen im Qt-Client (nur „übernehmen" als expliziter Schritt).
- Performance/Streaming der Chart-Daten.

---

## 11. Umsetzungsstand (2026-09-28)

**Umgesetzt.** Alle Phasen sind im Code, getestet (`pytest chart_viewer/tests`:
365 Tests gruen) und am lebenden System verifiziert (Chart-Agent-Container,
Viewer verbunden, MCP-Tool `manage_chart_viewer`).

| Phase | Artefakt | Tests |
| :-- | :-- | :-- |
| 0 Dispatch-Extraktion | `agent/command_api.py`; `run_server.py` ruft nur noch `handle_command` | `test_command_dispatch.py` |
| 1 Lifecycle-Bruecke | `ControlService.execute`, `_op_list_windows`, `save_chart` mit `overwrite`; MCP `GET_CHART_STATE`/`LIST_WINDOWS`/`SAVE_CHART`/`APPLY_CHART` (neues `tools/chart_builder.ts`) | `test_command_dispatch.py`, `test_control_service.py` |
| 2 Komfort-Ops | MCP `ADD_PANE`/`REMOVE_PANE`/`REORDER_PANES`/`DUPLICATE_CHART`/`DELETE_CHART`/`DIFF_CHART`; `chart_spec.diff_chart_specs` + Op `diff_chart` | `test_chart_spec.py`, `test_control_service.py` |
| 3 Overrides | `chart_spec.apply_pane_overrides` (Whitelist), `normalize_chart_spec` behaelt sie | `test_chart_spec.py`, `test_chart_warnings.py` |
| 4 Warnungen | `build_overlays` -> `warnings[]`, Orchestrator -> `skipped[]`, Transport bis in die MCP-Antwort | `test_chart_warnings.py` |
| 5 Topbar | Op/Action `LIST_TOPBAR_METRICS` + festgeschriebene Vorrangregel | `test_topbar_precedence.py` |
| 6 Setup-Binding | `ChartAgent.assign_setup_slot`, Dispatch `SETUP_ASSIGN`, `SAVE_SETUP` mit `windows` | `test_setup_binding.py` |
| 7 Panel-Kern | Gewicht (Spinbox), Titel-Override, Pane duplizieren, "Speichern" in-place, Warnungen in der Statuszeile | `test_control_panel_builder.py` |
| 8 Doku + Smoke | `docs/architecture/chart-presets.md` (4, 4.1-4.3, 7, Grenzen), `scripts/chart_builder_smoke.sh` | manuell (unten) |

### Abweichungen vom Plan

1. **`ThreadingHTTPServer` statt `HTTPServer`** (zusaetzlich, kritisch): SAVE_CHART und
   APPLY_CHART rendern intern ueber denselben Endpunkt zurueck. Mit dem
   single-threaded Server deadlockte der Aufruf - das Chart wurde geschrieben, die
   Antwort kam nie (leerer HTTP-Body). Fix in `run_server.py`, live verifiziert.
2. **Phase 7 "Hoehen aus Fenster uebernehmen" nicht umgesetzt**: braucht einen
   Viewer->Agent-Kanal fuer die Splittergroessen. Stattdessen ist das Gewicht direkt
   editierbar; das Ziehen im Fenster bleibt lokal (siehe Grenzen im Vertrag).
3. **`compose_chart` erbt Topbar/X-Achse auch ohne `base_chart_id`** vom Chart des
   Fensters (vorher: leerer Streifen). Voraussetzung fuer die Vorrangregel aus Phase 5.
4. **`SET_TOPBAR` persistiert auch ohne vorherigen Snapshot** (Fenster muss im Ledger
   stehen) - sonst waere "Blöcke ueberleben ein Re-Render" nicht testbar.
5. **MCP `timeframe` ohne Default**: `SETUP_ASSIGN` haette sonst bei jedem Aufruf
   stillschweigend `1D` in den Slot geschrieben (`const tf = timeframe || "1D"` bleibt).
6. **`DIFF_CHART` vergleicht Slots (`pane_id`)**: eine zweite Pane mit gleichem Preset
   erscheint als hinzugefuegt/entfernt statt als "geaendert" (bewusst, Vertrag 4).
7. **Diff-Fix**: die PCA-API liefert die Preset-Id verschachtelt unter `preset.id`,
   nicht als `pane_preset_id` - `_pane_snapshot` loest beides auf (sonst meldet der
   Diff fuer jedes unveraenderte Chart Unterschiede).
8. **DELETE_CHART-Schutz war doppelt kaputt** (Nachtrag nach Review):
   a) `collectChartRefs` hat den Baum nur traversiert und nie gegen `chartId`
      verglichen - `findChartReferences` (neu in `tools/chart_refs.ts`, rein und mit
      `deno test` abgedeckt) prueft jetzt `chart_id` strukturell und zusaetzlich
      nackte Strings.
   b) `LIST_SETUPS` lieferte gar keine Chart-Bindungen (nur id/monitor_count/
      window_count/updated_at), eine Referenzpruefung konnte also nie greifen.
      `ChartAgent.list_setups` liefert jetzt je Variante `windows` mit
      `window_id/chart_id/symbol/timeframe`.
   Zusaetzlich ist der Schutz **fail-closed**: ist der Agent nicht erreichbar, wird
   ohne `force` nicht geloescht.

### Verifikation am lebenden System (Walkthrough abgehakt)

- `LIST_WINDOWS` -> Fenster mit Symbol/Timeframe/chart_id/Draft/Pane-Anzahl (HTTP und MCP).
- `GET_CHART_STATE` -> 3 Panes in Reihenfolge, `chart_id=default`, `draft=false`.
- `DIFF_CHART` gegen `default` -> `same: true`.
- `ADD_PANE breadth_line__rs after=main` -> Pane hinter dem Preispane, Draft; `DIFF`
  zeigt die Aenderung; `APPLY_CHART default` stellt das Fenster exakt wieder her.
- `COMPOSE_CHART` (4 Panes) -> `SAVE_CHART` `created: true`; zweites `SAVE_CHART` mit
  `overwrite:false` -> `already_exists`; `DIFF` gegen die Kopie -> `same: true`;
  Test-Chart per PCA-API geloescht, Fenster zurueck auf `default`.
- `LIST_TOPBAR_METRICS symbol=HOOD` -> 23 Metriken mit `has_data_for_symbol`.

Nicht live gefahren (schreiben in die DB, bewusster Einzelschritt):
MCP `DUPLICATE_CHART`/`DELETE_CHART`/`SETUP_ASSIGN` - per Unit-Test abgedeckt.

### Offen / Follow-up

- Splitter-Ziehhoehen ins Chart-Gewicht uebernehmen (Viewer->Agent-Kanal).
- Panel-Katalog mit Suche/Vorschau, Undo/Redo, Multi-Select (Runde-1-Liste, Rest).
- `update_pane_preset`-Op (Pane-Presets aus dem Panel bearbeiten) bleibt optional.
- Deployment: Chart-Agent und MCP-Container neu starten
  (`docker restart qjm-chart-viewer-server llm-gw-mcp-pca`) - am 2026-09-28 erfolgt.

### Nachtrag: Anwenden-Liste und Builder (ein Chart-Bestand)

- **Befund (Anwender):** Ein ueber "GESPEICHERTES CHART ANWENDEN" gewaehltes Chart
  wurde im Fenster gerendert, der CHART-BUILDER zeigte aber weiter die Panes des
  vorherigen Charts (bzw. beim ersten Anwenden eine leere Liste). Ursache: Das Panel
  liest seinen Stand aus `get_chart_state`, rief den Op nach `apply_preset` aber nie
  auf; ein Agent-seitiges `APPLY_CHART`/`COMPOSE_CHART` (MCP) ebenso wenig, weil der
  eintreffende Snapshot nur die Fensterliste aktualisierte.
- **Fix:** `ControlPanelWindow.notify_window_content_changed(window_id)` +
  `ViewerApp` ruft es bei jedem `snapshot.full` (und beim `layout.restore`) fuer das
  Zielfenster; `_on_apply_preset_response` liest den Stand zusaetzlich direkt nach dem
  Anwenden. `_refresh_chart_state` fasst parallele Anfragen fuer dasselbe Fenster
  zusammen.
- **Konsistenz:** Die Anwenden-Liste kommt jetzt aus `list_charts` (derselbe Katalog,
  den der Builder speichert) statt aus dem Alt-Endpunkt `/api/presets`; sie wird nach
  jedem Speichern neu geladen und zeigt ohne eigenen Override das Chart des
  Zielfensters. Damit gilt: was anwendbar ist, kennt der Builder - und was der Builder
  speichert, ist sofort anwendbar.
- **Tests:** `test_applying_a_saved_chart_refreshes_the_builder_panes`,
  `test_saved_charts_are_offered_for_applying_after_save`,
  `test_agent_snapshot_refreshes_the_builder_panes` (echter `ViewerApp` +
  In-Process-Transport); `test_control_panel.py` fuehrt den Chart-Katalog als Fake.
  Live (read-only) geprueft: `dsh_playground/live_builder_check.py` verbindet sich als
  Viewer-Client, liest `get_chart_state` ueber den echten Panel-Pfad und vergleicht
  die Builder-Zeilen mit `/api/charts/{id}`.

### Nachtrag: Chart anwenden verschiebt kein Fenster mehr

- **Befund (Anwender):** Beim Anwenden eines gespeicherten Charts sprang das
  Fenster auf Groesse/Position 1100x750 / 100,100.
- **Wahre Ursache:** `orchestrator.build_display_stock` fuellte die Geometrie
  **immer** auf (`position or {"x":100,"y":100}`, `size or {"width":1100,"height":750}`).
  `command_api` reichte das als "ausdrueckliche Platzierung" an `open_window`
  weiter - jeder Re-Render (Chart anwenden, COMPOSE, Symbolwechsel, MCP) zog das
  Fenster damit auf dieselbe Stelle. Die Signatur war im Ledger sichtbar: alle
  Fenster standen exakt auf 100,100 / 1100x750.
  Zwei erste Anlaeufe (Ledger-Stand in `open_window` behalten, Client-Guard)
  konnten das nicht beheben, weil der Wert schon vorher gesetzt wurde - der
  Live-Test war zudem wirkungslos, weil das Testfenster bereits auf dem
  Zielwert stand.
- **Fix:** Der Renderer erfindet keine Geometrie mehr: `build_display_stock`
  reicht `position`/`size` nur durch, wenn sie angefordert wurden.
  `open_window` nimmt Geometrie nur noch in die Nachricht, wenn sie ausdruecklich
  kommt **oder** das Fenster neu angelegt wird (Startwerte). `_render_window` und
  der Symbolwechsel schicken keine. Der Client setzt move()/resize() nur bei
  echter Aenderung, liest beide Werte vor dem ersten move() (In-Process-Transport:
  Ledger-Eintrag == Payload-Dict) und meldet seine Geometrie nach dem Oeffnen
  aktiv zurueck, damit der Ledger ihm folgt statt umgekehrt.
- **Tests:** `test_window_lifecycle.py` (Re-Render mit dem **echten**
  `build_display_stock` laesst Position/Groesse stehen; Platzierung wirkt weiter;
  Client meldet seine Geometrie), `test_pane_scale.py` (Orchestrator erfindet
  nichts, explizite Geometrie kommt durch), `test_control_service.py`
  (`apply_chart` schickt keine Geometrie).
- **Live verifiziert (00:41):** `DISPLAY_STOCK` mit `preset=rs_monitor_chart` und
  `COMPOSE_CHART` -> Log `window.open ...: ohne Geometrie (der Client behaelt
  Position/Groesse)`, Ledger unveraendert, **kein** `geometry_changed`.

### Nachtrag: "Layout als default speichern" ist kein stiller No-op

- **Befund:** `SAVE_SETUP {setup_name:"default"}` antwortete mit
  `{"status":"ok","skipped":true}` und schrieb nichts. Ursache: `save_setup`
  vergleicht einen Geometrie-Hash (Fenster, Positionen, Monitore) mit dem letzten
  Autosave und ueberspringt dann - die Bridge rief es ohne `force` auf. Der Hash
  kennt Chart und Symbol **nicht**: nach einem reinen Chart-/Symbolwechsel waere
  "jetzt als default speichern" ein stiller No-op.
- **Fix:** Der explizite Pfad (`SAVE_SETUP` ueber Bridge/MCP) schreibt immer
  (`force=True`); die Dedup-Bremse bleibt ausschliesslich fuer den entprellten
  Autosave. Test: `test_setup_binding.py::test_save_setup_always_writes_instead_of_being_deduped`.
- **Verifiziert:** `default` (monitor_count 1) enthaelt danach genau den Ledger-Stand
  (`win_hood_1d`, HOOD, Chart `default`, Position/Groesse, color_flag 0) mit neuem
  `updated_at`.

### Nachtrag: Volumen-Pane entfernen (die Definition ist vollstaendig)

- **Befund (Anwender):** Das Volumen-Pane liess sich im Builder nicht entfernen - es
  war beim naechsten Render wieder da. Ursache waren **zwei** stille Ergaenzungen, die
  dem Vertrag widersprachen (Migration 040: "ab jetzt steht es explizit in der
  Definition und ist entfernbar"):
  1. `chart_spec.normalize_chart_spec` haengte ein `builtin:volume` an, sobald keine
     Pane role=volume hatte - also bei jedem Compose/Render.
  2. `presets_api._store_chart_panes` tat dasselbe beim **Speichern**, sodass selbst
     ein korrekt gerendertes Chart beim Persistieren wieder Volumen bekam.
- **Fix (Invariante):** Die Pane-Liste einer Definition ist VOLLSTAENDIG. Einzige
  Normalisierung bleibt das Preispane auf Slot `main` (Vertrag Abschnitt 1).
  `builtin:volume` wird nur gezeichnet, wenn eine Pane mit role=volume dasteht.
  Die **flache Altform** (`members` mit pane-Strings) kennt keinen Zustand "ohne
  Volumen" (der alte Viewer zeichnete es immer) - sie bekommt es bei der Uebersetzung
  weiterhin explizit, wie in Migration 040; danach ist es eine normale, entfernbare
  Pane.
- **Tests:** `test_chart_spec.py` (kein implizites Volumen; explizites Volumen-Pane
  bleibt mit Slot/Gewicht; Histogramm nur bei role=volume),
  `test_pane_scale.py` (Chart-Definition und Builder-Compose ohne Volumen),
  PCA-Selbsttest `test_preset_pane_scales.py` (Speichern ergaenzt nichts; Altform
  ergaenzt es explizit) - 12 Tests im Container.
- **Live verifiziert (2026-09-28):** `COMPOSE_CHART` ohne Volumen ->
  `GET_CHART_STATE` ohne Volumen (Screenshot `dsh_playground/screenshot_*win_hood_1d.png`),
  `APPLY_CHART default` stellt den Definitionsstand wieder her; `POST /api/charts`
  ohne Volumen speichert ohne Volumen; Altform `members` erhaelt es explizit.
  Container `qjm-pca-service`, `qjm-chart-viewer-server`, `llm-gw-mcp-pca` neu
  gestartet; das offene Layout wurde vorher gesichert und mit
  `dsh_playground/restore_layout.py` (snapshot/replay) exakt wiederhergestellt -
  inklusive des offenen Builder-Drafts.

### Nachtrag: Panel-Bedienung (Preset-Wechsel, Hinzufuegen-Filter)

- **Preset-Wechsel der markierten Pane** ([control_panel.py](chart_viewer/src/chart_viewer/ui/control_panel.py)):
  Das Dropdown unter der Pane-Liste referenziert ein anderes geteiltes Preset, ohne die
  Slot-Id zu wechseln - Gewicht und Titel-Override bleiben, die Skala zieht nur mit, wenn
  sie noch der Default des alten Presets war (ein bewusstes LIN/LOG bleibt stehen), ein
  `scale`-Override faellt mit dem alten Preset. Reines Draft-Compose, kein Agent-Op.
- **Hinzufuegen-Dropdown ohne `role=price`**: die 9 Preispane-Presets des Katalogs standen
  auch in der Anhaengen-Liste und konnten die Regel "genau ein Preispane" (Vertrag §1)
  verletzen; getippte Preispane-Namen werden ebenfalls abgewiesen. Beide Dropdowns zeigen
  jetzt dieselbe, nach Anzeigename sortierte Liste.
- **Gefunden und behoben**: `_normalize_pane` hat `overrides` verworfen. Titel- und
  Serien-Overrides waren damit nach jedem `get_chart_state` still verloren - der
  Titel-Edit wirkte nur bis zum naechsten Compose. Der Override-Titel schlaegt jetzt auch
  den Preset-Namen in der Pane-Zeile.
- Clientseitig: kein Container-Neustart, ein Neustart des Viewers genuegt.
