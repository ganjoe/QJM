# Implementationsplan: Control-Panel für den Chart-Viewer

Status: **Umgesetzt (2026-09-25)** – siehe Abschnitt 14 für den Implementierungsabschluss
Geltungsbereich: Desktop-Viewer (PySide6-Client) + Chart-Agent (`chart-viewer-server`) + Wire-Protokoll
Referenz: `dsh_playground/chart_viewer_spec_v2.2.md` (Abschnitte 1, 3, 8, 11), `dsh_playground/watchlist_architecture.md`

---

## 1. Anforderungen & Vertrag

| # | Anforderung (User) | Festlegung in diesem Plan |
| :-- | :-- | :-- |
| A1 | Kleines Control-Panel, **immer offen**, solange der ChartViewer läuft | Viewer-eigenes Singleton-Fenster `ControlPanelWindow`, wird beim App-Start erzeugt und angezeigt; **nicht** Teil des Agent-Layout-Ledgers (Setups/Screenshots unberührt) |
| A2 | Direkteingabe Ticker mit **Echtzeitsuche** ab den ersten Buchstaben | `QLineEdit` + 150 ms Debounce → `control.request/search_symbols` → PostgREST-Prefix-Suche auf `cda_master_universe` |
| A3 | Suchfeld bezieht sich auf die **Datenbank** | Suche läuft ausschließlich **agent-seitig** (Service-Role-Key bleibt auf dem Server, der Windows-Client sieht nie DB-Credentials); Agent läuft im Container `qjm-chart-viewer-server` |
| A4 | **Alle Watchlisten** selektierbar (Dropdown **und** Suchfeld für Watchlisten) | filterbares `QComboBox` (Completer, `MatchContains`) mit `all` + allen Namen aus `GET /api/watchlists` |
| A5 | Watchlisten **editierbar**: Ticker löschen/hinzufügen | `control.request/mutate_watchlist` → PCA-Service-API (`add`/`remove`); Master-Liste `all` ist schreibgeschützt |
| A6 | Über die Suche gefundenen Ticker **einer Watchlist hinzufügen** | Button „＋“ + `Ctrl+Enter`; stößt zusätzlich den Chart-Download an (wie MCP `manage_watchlist ADD`) |
| A7 | **Chart-Preset** auswählen (Dropdown) | `control.request/list_presets` (PCA `GET /api/presets`); Anwenden auf die Chart-Fenster der gewählten Color-Flag-Gruppe |
| A8 | (abgeleitet) Ticker direkt ins Chart bringen | Klick/Enter auf Suchergebnis → bestehende Color-Flag-Routing-Logik des Viewers (Verhalten wie Watchlist-Fenster), inkl. Preset |

**Nicht-Ziele (v1):** kein Zeitrahmen-Umschalter, kein Watchlist-Anlegen/Umbenennen/Löschen im Panel, keine Namenssuche (Firma→Ticker), kein Editieren der Liste `all`, keine Änderung an Scannern.

---

## 2. Ist-Analyse (verifiziert am Code und am laufenden System)

### 2.1 Laufendes System (geprüft am 2026-09-25)

| Prüfung | Ergebnis |
| :-- | :-- |
| Ports | `8765` (Agent-WS), `8766` (Control-API), `8794` (PCA-Service), `8001` (Supabase/PostgREST), `8002` (stock-data-node) lauschen |
| Viewer | 1 Client verbunden; offene Fenster: `muse_portfolio` (Watchlist), `win_amd_1d` (Chart, Symbol `BNC`, Preset `qmaggi_lab`), `phoenix_rs_adr_20260924` (Watchlist) |
| Watchlisten | 27 benutzerdefinierte Listen + `all` (41.723 Ticker mit `has_parquet=true`) |
| Presets | 12 Presets (u. a. `default`, `qmaggi`, `qmaggi_lab`, `rs_top40`, `trend_template`, `momentum`) |
| `cda_master_universe` | Spalten u. a. `ticker`, `type` (z. B. `CS`), `has_parquet`, `currency`, `market_cap`; **keine Firmennamen-Spalte** |
| PCA-Watchlist-API | `GET /api/watchlists/10_favorite` liefert `[{ticker, position, added_at}]` |
| Baseline-Tests | `cd chart_viewer && .venv/bin/python -m pytest -q` → **102 passed, 1 failed** (vorbestehend: `test_window_lifecycle.py::test_criterion_10_viewer_restart_layout_restore`, weil `InProcessTransport.connect()` kein `viewer.ready` sendet) |

### 2.2 Relevante Bausteine im Code

| Baustein | Datei / Zeile | Rolle im Plan |
| :-- | :-- | :-- |
| Viewer-Controller | `chart_viewer/src/chart_viewer/ui/app.py` Z. 22, 62, 100 | Panel-Erzeugung, Dispatch von `control.response`, Verbindungsstatus |
| Fenster-Lifecycle | ebd. Z. 209 (`_handle_window_open`), 271 (`_handle_watchlist_open`) | Panel bleibt außerhalb von `self.windows`/`self.watchlists` |
| Flag-Routing (Ticker→Chart) | ebd. Z. 341–368 (`_on_watchlist_row_selected`) | wird zu `route_symbol_to_flag(symbol, flag, preset=None)` verallgemeinert |
| Symbolwechsel | ebd. Z. 370–381; `ui/window.py` Z. 88–91 | erhält optionales `preset` |
| Watchlist-Fenster | `ui/watchlist_window.py` Z. 14, 81, 139 | Vorbild für Layout/Interaktion; empfängt Live-Updates |
| Color-Flag | `ui/color_flag.py` (`ColorFlagButton`) | Ziel-Bindung des Panels |
| Agent | `agent/agent_client.py` Z. 100, 182 (`_on_envelope`), 215, 330, 346 | Dispatch-Hook für `control.request`; Refresh offener Watchlist-Fenster |
| Supabase-Helfer | ebd. Z. 46–97 (`_supabase_get/post/patch/delete`) | werden für die Tickersuche genutzt |
| Symbolwechsel-Rebuild | ebd. Z. 346–396 (`_handle_window_change_symbol`) | Muster für Preset-Anwendung per internem `DISPLAY_STOCK`-POST |
| Control-API | `run_server.py` Z. 142 (`do_POST`), 285–372 (`DISPLAY_STOCK`) | unverändert; wird intern für Preset-Apply wiederverwendet |
| Snapshot-Bau | `orchestrator.py` Z. 22, 64, 148 (`build_display_stock`) | Preset → Indikatoren/Topbar |
| Viewer-Config | `config.py` Z. 12, 92 (`from_env`) | neue Panel-Parameter (ENV-steuerbar) |
| Transports | `transport/base.py`, `websocket.py` Z. 140 (`_on_connection_established`), `in_process.py` | Connect-Callbacks für Panel-Reload |
| Watchlist-State | `core/state_manager.py` Z. 90 (`update_watchlist_data`, `replace=True`), `models/entities.py` Z. 129 | Live-Refresh offener Watchlist-Fenster |
| PCA-Ticker-API | `services/pca-service/watchlists_api.py` Z. 33 (Master-Schutz), 103/122/153/223 | Backend für Panel-Watchlisten-Ops |
| Preset-API | `services/pca-service/main.py` (Router `/api`), `mcp/agent-pca/tools/presets.ts` Z. 32–56 | `GET /api/presets`, `GET /api/presets/{id}` |
| Download-Trigger | `mcp/agent-pca/tools/pca.ts` Z. 7–16 + `shared.ts` Z. 16 | `POST {STOCK_DATA_NODE_URL}/add` bei Watchlist-Add |
| Watchlist-Fenster-Payload | `mcp/agent-pca/tools/chart_viewer.ts` Z. 749–804 | `window_id = list_id`, `columns=["Symbol"]`, `rows=[{symbol, cells:{Symbol}}]` |
| Deployment | `llm-gateway/docker-compose.yml` Z. 388–421 | Container `qjm-chart-viewer-server`; Src read-only gemountet |

### 2.3 Was fehlt

- Kein Control-Panel, kein Suchfeld, kein Preset-Dropdown im Viewer.
- Kein Viewer→Agent-**Request/Response**-Kanal (bisher nur fire-and-forget + `ack`).
- Kein Transport-Signal „verbunden/getrennt“ (das Panel könnte beim Start noch gar nicht senden).
- Kein Agent-seitiger Zugriff auf die **Ticker-Liste** (nur der PCA-Service cached das Master-Universe für Scanner).
- Keine Möglichkeit, ein Preset auf ein **bestehendes** Fenster anzuwenden (nur `DISPLAY_STOCK` beim Öffnen/Symbolwechsel).

---

## 3. Architektur-Entscheidungen

| # | Entscheidung | Begründung / Alternative |
| :-- | :-- | :-- |
| E1 | **Panel ist Viewer-eigenes Fenster**, wird in `ViewerApp.start()` erzeugt und angezeigt | „immer offen, wenn ChartViewer gestartet ist“; keine Agent-Kommandos nötig; Layout-Setups (`save_setup`/`load_setup`, `close_windows_except`) und Screenshots bleiben unverändert. *Alternative verworfen:* agent-gesteuertes Fenster (dann müsste jedes Setup das Panel kennen). |
| E2 | **Datenpfad über das bestehende WebSocket-Protokoll**: `control.request` (Viewer→Agent) / `control.response` (Agent→Viewer) | Der Agent ist laut Spezifikation die einzige Datenquelle; der DB-Key bleibt serverseitig; Reconnect/Sequenzierung des WS-Transports werden mitbenutzt; In-Process-Tests bleiben möglich. *Alternative verworfen:* direkter HTTP-POST des Clients auf `8766/api/control` (zweiter Kanal, doppelte Fehlerbehandlung, Host-/Port-Wissen im Client). |
| E3 | **Ziel-Bindung über die Color-Flag-Gruppe** (wie Watchlist-Fenster) | Bestehendes, vertrautes Routing; ein Ticker-Klick aktualisiert alle Charts derselben Flag-Gruppe; das Preset wirkt auf dieselbe Gruppe. Das Panel zeigt an, welche Charts betroffen sind. |
| E4 | **Preset-Anwendung = internes `DISPLAY_STOCK`** an `http://127.0.0.1:8766/api/command` je betroffenem Fenster | Exakt das Muster aus `_handle_window_change_symbol` (Z. 389–394); kein Refactoring von `run_server.py`; Topbar-/Snapshot-Persistenz bleibt eine Quelle. |
| E5 | **Watchlist-Mutationen über die PCA-Service-API**, nicht direkt Supabase | Master-Schutz, Ticker-Normalisierung und Duplikat-Behandlung (409) existieren dort bereits; ein späterer Umzug betrifft nur eine Stelle. |
| E6 | **Suche per PostgREST-Prefix-Query + kleinem Agent-LRU** (kein Vollindex in v1) | 41.723 Zeilen, lokaler Postgres, Prefix-`ilike` + `limit`: wenige ms; LRU (TTL 30 s) macht Backspace/Retyping instant. *Optional Phase 4:* Vollindex im Agent, falls P95 > 100 ms. |
| E7 | `all` wird **nie** gerendert (41.723 Zeilen) | `get_watchlist` liefert für `all` nur `{count, truncated:true}`; das Panel zeigt einen Info-Text statt der Liste; Add/Remove deaktiviert. |
| E8 | Antworten werden **gebroadcastet**, Korrelation über `request_id` | `WebSocketServerTransport.send_command` ist ein Broadcast (eine Sequenznummern-Quelle). Bei mehreren Viewern ignorieren fremde Panels unbekannte `request_id`. *Später optional:* `send_to_client`. |

---

## 4. UI-Spezifikation

### 4.1 Skizze (kompakt, ca. 360 × 540 px)

```
┌─ Chart Control ────────────────────────────── 📌 ─┐
│ Ticker  [ NV__________________ ]        ⟳        │
│  ┌───────────────────────────────────────────┐   │
│  │ NVDA   CS  ●                              │   │  ← Suche (DB), max. 20
│  │ NVDA.W ▸                                  │   │
│  └───────────────────────────────────────────┘   │
│  [ ▸ Chart ]   [ ＋ Watchlist ]                  │
│                                                  │
│ Watchlist  [ 00_positions ▾ ]      ⟳  [ Neu ]    │  ← filterbar (Suchfeld)
│  ┌───────────────────────────────────────────┐   │
│  │ VICR                                      │   │
│  │ MRNA                                      │   │  ← Inhalt, Del = entfernen
│  │ TEM                                       │   │
│  └───────────────────────────────────────────┘   │
│  [ − Entfernen ]        ( 15 Ticker )            │
│                                                  │
│ Preset     [ QMaggi + RS neutral ▾ ]             │  ← Presets aus PCA
│ Ziel-Flag  ●   Charts: AMD, BNC                  │
│                                                  │
│ ● verbunden              Letzte Aktion: + NVDA   │
└──────────────────────────────────────────────────┘
```

### 4.2 Widgets & Interaktionen

| Element | Verhalten |
| :-- | :-- |
| **Ticker-Suchfeld** (`QLineEdit`) | `textChanged` → Debounce-Timer (`config.control_search_debounce_ms`, Default 150 ms) → `search_symbols`. `↓` springt in die Trefferliste. `Enter` öffnet den ersten/ausgewählten Treffer im Chart. Leeres Feld löscht die Liste. |
| **Trefferliste** (`QListWidget`) | Zeile: `TICKER · type · ●` (● = `has_parquet`, ○ = kein Chart-Cache, Tooltip: „Download wird beim Hinzufügen angestoßen“). Doppelklick/`Enter` → Chart. Pfeiltasten navigieren. |
| **„▸ Chart“** | aktiv bei Auswahl; ruft `route_symbol_to_flag(ticker, panel_flag, preset)`. |
| **„＋ Watchlist“** / `Ctrl+Enter` | hängt den gewählten Treffer an die gewählte Watchlist (`mutate_watchlist/add`); deaktiviert bei `all`/keiner Liste/keinem Treffer. |
| **Watchlist-Dropdown** (`QComboBox`, `setEditable(True)`) | Einträge: `all` (oben, 🔒) + alle Namen. `QCompleter` mit `MatchContains`, `CaseInsensitive`, `PopupCompletion`, `setInsertPolicy(NoInsert)`. Auswahl (`activated`) → `get_watchlist`. Freitext filtert nur, löst keine Mutation aus; `Enter` übernimmt den ersten Treffer. |
| **Listeninhalt** (`QListWidget`) | Ticker in Positions-Reihenfolge; Doppelklick → Chart; `Del`/„−“ → `mutate_watchlist/remove` (nur wenn `editable`). Bei `all`: Info-Label „Master-Universe · 41.723 Ticker · schreibgeschützt“ statt Liste. |
| **Preset-Dropdown** (`QComboBox`, filterbar) | Einträge `display_name` (`userData = id`); Anzeige des aktuell wirksamen Presets. `activated` → `apply_preset`. Ist kein Chart der Flag-Gruppe offen, wird das Preset gemerkt und beim nächsten `route_symbol_to_flag` mitgegeben (Statuszeile erklärt das). |
| **Flag-Button** (`ColorFlagButton`) | Ziel-Gruppe (0–3), farblich wie in Chart-/Watchlist-Fenstern; persistiert. |
| **Statuszeile** | Verbindungszustand (● grün / ○ grau / ⚠ rot), Anzahl Ticker der aktiven Liste, letzte Aktion/Fehler, `all`-Hinweis. |
| **📌 Pin** | `Qt.WindowStaysOnTopHint` an/aus, Default **an** („immer offen“); persistiert. |
| **Schließen** | `closeEvent` → `hide()` statt Zerstören; Toggle per `Ctrl+Shift+P` in jedem Viewer-Fenster; beim nächsten Start (und nach Reconnect) wieder sichtbar. |
| **Persistenz** | `QSettings("QJM", "ChartViewer")`: Fenstergeometrie, Pin, Flag, letzte Watchlist, letztes Preset. |

### 4.3 Fehler- und Leerzustände

- **Nicht verbunden:** alle Requests werden unterdrückt, Statuszeile „getrennt – warte auf Agent“, automatischer Reload bei Connect.
- **Keine Antwort** auf einen Request (> `control_request_timeout_ms`, Default 5 s): Zeile wird als „⚠ keine Antwort“ markiert, Request verworfen; Button-Zustand zurückgesetzt.
- **Backend-Fehler** (PCA/Supabase unerreichbar): Klartext in der Statuszeile (`error.message`), UI bleibt bedienbar.
- **Doppelt hinzufügen:** Antwort `already_exists` → Statuszeile „NVDA ist bereits in 00_positions“, Liste unverändert.
- **Reconnect/Resync:** Panel leert Pending-Requests und lädt Watchlisten + Presets neu (Connect-Callback, Abschnitt 5.4).

---

## 5. Protokoll-Erweiterung

### 5.1 `control.request` (Viewer → Agent, `kind=COMMAND`, `window_id=null`)

```json
{ "request_id": "cp_9f3c1a...", "op": "search_symbols",
  "params": { "query": "NVD", "limit": 20 } }
```

### 5.2 `control.response` (Agent → Viewer, `kind=EVENT`)

```json
{ "request_id": "cp_9f3c1a...", "op": "search_symbols", "ok": true,
  "data": { "results": [ {"ticker":"NVDA","type":"CS","has_parquet":true} ],
            "match_count": 2, "source": "db", "elapsed_ms": 11 },
  "error": null }
```

Fehlerobjekt: `{ "code": "invalid_request" | "protected_list" | "not_found" | "backend_unreachable" | "timeout" | "internal", "message": "..." }`

### 5.3 Ops (vollständige Liste v1)

| op | params | `data` bei Erfolg |
| :-- | :-- | :-- |
| `search_symbols` | `query`, `limit` (1–50) | `results[{ticker,type,has_parquet}]`, `match_count`, `source` |
| `list_watchlists` | – | `watchlists[{name, editable}]` (`all` zuerst) |
| `get_watchlist` | `list_name` | `tickers[{ticker,position}]`, `count`, `editable`, `truncated` |
| `mutate_watchlist` | `action ("add" oder "remove")`, `list_name`, `ticker` | `status ("added"/"already_exists"/"removed")`, `count` |
| `list_presets` | – | `presets[{id,display_name,description,indicator_count}]` |
| `apply_preset` | `preset_id`, `color_flag` | `applied[window_id]`, `skipped[{window_id,reason}]` |

### 5.4 Weitere Protokolländerungen (rückwärtskompatibel)

| Änderung | Ort | Wirkung |
| :-- | :-- | :-- |
| `window.change_symbol` erhält optionales Feld `preset` | `ui/app.py` `_on_symbol_change_requested`; `agent_client._handle_window_change_symbol` | Symbolwechsel übernimmt das im Panel gewählte Preset und schreibt es in den Layout-Ledger |
| `ChartWindow.symbol_change_requested` wird `Signal(str, str, object)` | `ui/window.py` | dritter Parameter `preset` (`None` = bisheriges Verhalten) |
| Transport-Callbacks `on_connect(handler)` / `on_disconnect(handler)` | `transport/base.py`, `websocket.py` (`_on_connection_established`, Reconnect-Loop), `in_process.py` | Panel lädt beim (Re-)Connect Watchlisten/Presets; Statusanzeige |
| `viewer.ready` bleibt unverändert | – | Agent muss an der Handshake-Logik nichts ändern |

> Unbekannte Nachrichtentypen werden von beiden Seiten bereits ignoriert (elif-Ketten in `app.py` ab Z. 100 und `agent_client.py` ab Z. 182) → ein gemischter Rollout (alter Client / neuer Agent) bricht nicht.

---

## 6. Server-Seite: `ControlService`

Neues Modul `chart_viewer/src/chart_viewer/agent/control_service.py` (ca. 250–300 Zeilen):

```python
class ControlService:
    def __init__(self, agent, *, pca_url, supabase_url, supabase_key,
                 stock_data_url, http_timeout_s=8.0, search_cache_ttl_s=30.0,
                 max_workers=2, search_limit_max=50): ...

    def handle(self, request_id: str, op: str, params: dict) -> None:
        """Antwortet per agent.transport.send_command('control.response', ...)."""
```

| Aspekt | Festlegung |
| :-- | :-- |
| Dispatch | `ChartAgent._on_envelope` bekommt den Zweig `elif envelope.type == "control.request": self.control_service.handle(...)` |
| Threading | `ThreadPoolExecutor(max_workers=2, thread_name_prefix="cv-control")` für alle Netz-Ops; **`search_symbols` wird direkt im Receive-Thread beantwortet** (RAM-/LRU-Operation bzw. eine schnelle Query) → Reihenfolge der Suchantworten bleibt garantiert, Tippen blockiert nicht hinter einem langsamen Watchlist-Op |
| Suche | `q = re.sub(r"[^A-Za-z0-9.\-^]", "", query)[:12].upper()`; Query `GET {SUPABASE_URL}/rest/v1/cda_master_universe?select=ticker,type,has_parquet&ticker=ilike.{quote(q + "%")}&order=ticker.asc&limit={limit}`; exakter Treffer wird nach vorn gezogen; wenn 0 Treffer und `len(q)>=2` → zweite Query `ilike.%{q}%` („enthält“) |
| Such-Cache | LRU `{(q, limit): (ts, results)}`, TTL 30 s (ENV `CV_CONTROL_SEARCH_CACHE_TTL_SEC`); macht Backspace/Retyping ohne DB-Roundtrip |
| Watchlisten-Liste | `GET {PCA_SERVICE_URL}/api/watchlists` → Namen; `all` nach vorn, `editable=false` |
| Watchlist-Inhalt | `GET {PCA_SERVICE_URL}/api/watchlists/{quote(name)}`; bei `all`: Count-Query gegen `cda_master_universe?has_parquet=eq.true&select=ticker&limit=0` mit `Prefer: count=exact`, danach `tickers=[]`, `truncated=true` |
| Add | `POST {PCA}/api/watchlists {list_name, ticker, position: 0}`; 400 → `protected_list`; 409/`already_exists` → `status="already_exists"` |
| Remove | `DELETE {PCA}/api/watchlists/{quote(name)}/{quote(ticker)}` |
| Download-Trigger | nach erfolgreichem Add: `POST {STOCK_DATA_NODE_URL}/add {"tickers":[ticker]}` fire-and-forget (Fehler nur Log) |
| Refresh offener Watchlist-Fenster | nach Mutation: existiert `list_name` im `agent.layout_ledger` als Watchlist-Fenster → frische Ticker laden und `agent.update_watchlist_data(list_name, columns=["Symbol"], rows=[{symbol,cells:{Symbol}}], replace=True)` |
| Presets | `GET {PCA}/api/presets` → Liste `[{id, display_name, description, indicator_count}]` |
| Preset anwenden | über `agent.layout_ledger`: Chart-Fenster = Einträge **ohne** `list_id`; Filter `color_flag == params.color_flag`; je Fenster Zeitrahmen aus dem Ledger → `"{mult}{unit}"`, dann `POST http://127.0.0.1:8766/api/command {"action":"DISPLAY_STOCK","window_id":..., "symbol":..., "preset":..., "timeframe_str":...}` |
| Fehler-Mapping | `urllib.error.HTTPError` → `backend_unreachable`/`not_found`/`protected_list`; `TimeoutError` → `timeout`; alles andere → `internal` (mit Log) |
| Env | `PCA_SERVICE_URL` (im Container bereits gesetzt: `http://pca-service:8791`), `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, **neu** `STOCK_DATA_NODE_URL` (Default `http://host.docker.internal:8002`) |
| Refactoring | `_supabase_get/post/patch/delete` aus `agent_client.py` nach `agent/supabase.py` verschieben und in `agent_client` re-importieren (vermeidet Zirkularimport, keine Verhaltensänderung) |

---

## 7. Client-Seite: `ControlPanelWindow`

Neues Modul `chart_viewer/src/chart_viewer/ui/control_panel.py` (ca. 450–600 Zeilen), Stil wie `WatchlistWindow` (dunkles Theme `#131722/#1E222D/#D1D4DC`).

```python
class ControlPanelWindow(QMainWindow):
    control_request   = Signal(str, str, dict)   # request_id, op, params
    symbol_activated  = Signal(str, int, str)    # symbol, color_flag, preset_id
    # Slots: apply_response(request_id, op, ok, data, error)
    #        set_connection_state(connected: bool)
    #        set_chart_windows([{window_id, symbol, color_flag}])
```

| Schritt | Datei | Inhalt |
| :-- | :-- | :-- |
| P1 | `ui/app.py` | `self.control_panel = ControlPanelWindow(config, parent=self)` im Konstruktor; `show()` in `start()`; `resync/clear_all` lässt das Panel unberührt |
| P2 | `ui/app.py` | `control_request` → `_send_control_request()` (Envelope `control.request`, `MessageKind.COMMAND`); bei getrennter Verbindung → `panel.set_connection_state(False)` |
| P3 | `ui/app.py` | neuer Zweig `elif msg_type == "control.response"` in `_handle_envelope_gui_thread` → `self.control_panel.apply_response(...)` |
| P4 | `ui/app.py` | `connection_signal = Signal(bool)`; `transport.on_connect/on_disconnect` → Signal; verbunden mit `panel.set_connection_state` (Reload bei True) |
| P5 | `ui/app.py` | `_on_watchlist_row_selected` delegiert an neues `route_symbol_to_flag(symbol, color_flag, preset=None)`; `panel.symbol_activated` verbindet auf dieselbe Funktion (mit Preset) |
| P6 | `ui/app.py` | `_on_symbol_change_requested(window_id, symbol, preset=None)` → `preset` im Payload; `_handle_window_open`/`_on_window_closed`/`_on_flag_changed` aktualisieren `panel.set_chart_windows(...)` aus `self.windows` |
| P7 | `ui/window.py` | `symbol_change_requested = Signal(str, str, object)`; `request_symbol_change(symbol, preset=None)`; `Ctrl+Shift+P` in `keyPressEvent` → Toggle-Signal |
| P8 | `config.py` | `control_panel_enabled=True`, `control_search_debounce_ms=150`, `control_search_limit=20`, `control_panel_width=360`, `control_panel_height=540`, `control_panel_always_on_top=True`, `control_request_timeout_ms=5000` (+ `from_env`-Einträge) |
| P9 | `ui/control_panel.py` | UI, Debounce, Pending-Timeout-Timer, Response-Handling, QSettings, Fehlerstatus |

**Wichtige Invarianten**

- Das Panel liegt **nicht** in `ViewerApp.windows`/`watchlists` → `get_all_geometries()`, `save_setup()`, `close_windows_except()`, `ScreenshotCapture` bleiben unverändert.
- Das Panel hat nie mehr als eine Suche gleichzeitig in Flight; eintreffende Antworten werden verworfen, wenn `query != aktueller Feldinhalt`.
- `all` wird nie als Liste gerendert (E7).
- Nach `mutate_watchlist` gilt: UI-Update erst nach erfolgreicher Antwort (kein optimistisches Entfernen) → keine Geister-Einträge bei Backend-Fehler.

---

## 8. Dateien & Änderungen

| Datei | Typ | Inhalt |
| :-- | :-- | :-- |
| `chart_viewer/src/chart_viewer/ui/control_panel.py` | **neu** | Panel-Fenster, Widgets, Debounce, Response-Handling, QSettings |
| `chart_viewer/src/chart_viewer/agent/control_service.py` | **neu** | Ops, HTTP-Clients, LRU, Executor, Fehler-Mapping |
| `chart_viewer/src/chart_viewer/agent/supabase.py` | **neu** | verschobene `_supabase_*`-Helfer |
| `chart_viewer/src/chart_viewer/ui/app.py` | geändert | Panel-Lifecycle, `control.response`, Connect-Signal, `route_symbol_to_flag`, Preset-Durchreichung, Chart-Fensterliste ans Panel |
| `chart_viewer/src/chart_viewer/ui/window.py` | geändert | Preset-Parameter im Signal, `Ctrl+Shift+P`-Toggle |
| `chart_viewer/src/chart_viewer/agent/agent_client.py` | geändert | `control.request`-Dispatch, `ControlService`-Instanz, Preset in `_handle_window_change_symbol`, Helfer für Watchlist-Refresh |
| `chart_viewer/src/chart_viewer/transport/base.py` | geändert | `on_connect`/`on_disconnect` (Default-No-op, damit bestehende Transports nicht brechen) |
| `chart_viewer/src/chart_viewer/transport/websocket.py` | geändert | Callbacks bei Connect/Disconnect auslösen |
| `chart_viewer/src/chart_viewer/transport/in_process.py` | geändert | Callbacks für Tests (behebt nebenbei den Baseline-Fehler, wenn `ViewerApp.start()` `viewer.ready` zentral sendet) |
| `chart_viewer/src/chart_viewer/config.py` | geändert | neue Panel-/Control-Parameter inkl. `from_env` |
| `chart_viewer/tests/test_control_panel.py` | **neu** | Viewer-seitige Tests (in-process) |
| `chart_viewer/tests/test_control_service.py` | **neu** | Agent-seitige Tests (HTTP gemockt) |
| `llm-gateway/docker-compose.yml` | geändert | `STOCK_DATA_NODE_URL` für `chart-viewer-server` (Z. ca. 395) |
| `dsh_playground/chart_viewer_spec_v2.2.md` | geändert | Abschnitt 3.3: `control.request`/`control.response`, `preset` in `window.change_symbol` |

`run_server.py`, `orchestrator.py`, PCA-Service und MCP-Tools bleiben unverändert.

---

## 9. Phasen & Arbeitsschritte

### Phase 1 – Protokoll & Agent (Basis)
1. `agent/supabase.py` extrahieren, `agent_client` anpassen, bestehende Tests grün halten.
2. `control_service.py`: `handle()` + `search_symbols` (Sanitizing, Query, LRU) + Unit-Tests.
3. `list_watchlists`, `get_watchlist` (inkl. `all`-Sonderfall), `mutate_watchlist` (+ Download-Trigger, Watchlist-Refresh).
4. `list_presets`, `apply_preset` (Ledger-Filter + interner `DISPLAY_STOCK`-POST).
5. `_on_envelope`-Dispatch + `window.change_symbol` mit `preset`.
6. Compose: `STOCK_DATA_NODE_URL` setzen; Container neu starten.

### Phase 2 – Viewer-UI
7. `config.py`-Parameter + `from_env`.
8. `control_panel.py`-Grundgerüst (Layout, Stil, Statuszeile, Pin, QSettings).
9. Suche (Debounce, Request, Response, Trefferliste, Tastatur).
10. Watchlist-Dropdown (Completer), Inhalt, Add/Remove, `all`-Schutz.
11. Preset-Dropdown + Anwenden + Merken.
12. Flag-Button, Chart-Fensterliste, `Ctrl+Shift+P`.

### Phase 3 – Integration & Politur
13. `app.py`: Lifecycle, Dispatch, Connect-Signal, `route_symbol_to_flag`-Refactor, Chart-Fensterliste.
14. Fehlerzustände (Timeout, getrennt, Backend-Fehler, `already_exists`).
15. Doku (Spec-Abschnitt 3.3), Code-Kommentare.

### Phase 4 – Verifikation & optionaler Ausbau
16. Neue Tests + kompletter Bestand; manueller End-to-End-Test mit laufendem Viewer (der Windows-Client synchronisiert beim nächsten Start).
17. **Optional:** Watchlist-Anlegen/Umbenennen/Löschen im Panel; Firmennamensuche über `company_ticker_mappings`; Voll-Index im Agent, falls Suchlatenz > 100 ms; `send_to_client` statt Broadcast; Counts im Dropdown.

---

## 10. Tests & Abnahmekriterien

### 10.1 Automatisierte Tests (`cd chart_viewer && .venv/bin/python -m pytest -q`)

| Test | Prüft |
| :-- | :-- |
| `test_panel_created_on_start` | `app.start()` → Panel sichtbar; `app.windows == {}` |
| `test_panel_not_in_layout_or_screenshots` | `get_all_geometries()`, `close_windows_except()` kennen das Panel nicht |
| `test_search_roundtrip_populates_results` | `search_symbols`-Response füllt die Liste; veraltete Antwort (anderer Query) wird ignoriert |
| `test_search_debounce_single_request` | 5 Tastendrücke in < 150 ms ⇒ genau **ein** Request |
| `test_watchlist_dropdown_filter_and_select` | Completer filtert; `activated` → `get_watchlist` |
| `test_add_and_remove_ticker_updates_list` | `mutate_watchlist`-Requests + Listeninhalt |
| `test_master_list_is_readonly` | `all` → Buttons deaktiviert, kein 41k-Render |
| `test_add_requires_selection` | „＋“ ohne Treffer/Liste deaktiviert |
| `test_preset_dropdown_and_apply` | `list_presets` füllt Combo; Auswahl → `apply_preset` mit Flag |
| `test_symbol_activation_routes_by_flag` | Treffer + Enter → bestehendes Chart-Fenster mit Flag bekommt `change_symbol`; ohne Fenster wird eines erzeugt |
| `test_preset_passed_on_symbol_change` | `window.change_symbol`-Payload enthält `preset` |
| `test_connection_state_reloads_lists` | Reconnect → erneutes `list_watchlists`/`list_presets` |
| `test_control_request_timeout_marks_error` | Keine Antwort → Statusfehler, Request verworfen |
| `test_control_envelope_roundtrip` | msgpack Encode/Decode beider neuer Typen |
| `test_search_query_sanitizing` (Agent) | Sonderzeichen/Überlänge werden bereinigt, korrekte PostgREST-URL |
| `test_mutate_maps_409_to_already_exists` (Agent) | Fehler-Mapping |
| `test_get_all_is_truncated` (Agent) | `all` → `tickers=[]`, `truncated=true`, `editable=false` |
| `test_apply_preset_uses_ledger_flag_filter` (Agent) | nur Chart-Fenster mit passendem `color_flag`, Watchlist-Fenster ausgenommen |
| `test_apply_preset_no_open_window` (Agent) | leere `applied`-Liste, kein Fehler |

### 10.2 Definition of Done

1. Viewer-Start zeigt das Panel; Schließen der Chart-Fenster lässt es bestehen; Neustart → Panel wieder da (Geometrie/Pin/letzte Auswahl wiederhergestellt).
2. Suche: Eingabe „NV“ ⇒ Treffer erscheinen ≤ 300 ms (P95) und stammen aus der DB (neu per CDA hinzugefügte Ticker sind sichtbar).
3. Ticker→Chart: Enter/Doppelklick aktualisiert die Charts der Flag-Gruppe bzw. öffnet ein neues Fenster; das gewählte Preset ist aktiv.
4. Watchlisten: Dropdown filterbar, Inhalt korrekt, „＋“ fügt hinzu, `Del`/„−“ entfernt, `all` gesperrt; ein offenes Watchlist-Fenster derselben Liste aktualisiert sich live.
5. Presets: Dropdown enthält alle PCA-Presets; Anwenden wirkt auf die Flag-Gruppe; ohne offenes Chart wird es für das nächste Öffnen gemerkt.
6. Alle bestehenden Tests bleiben grün (Baseline: 102 grün, 1 vorbestehender Fehler, der mitbehoben werden kann); neue Tests grün; keine Änderung an Setup-/Screenshot-Verhalten.
7. Doku: neue Nachrichtentypen in der Spec; Panel-Verhalten wie in diesem Plan.

---

## 11. Risiken & Gegenmaßnahmen

| Risiko | Wirkung | Gegenmaßnahme |
| :-- | :-- | :-- |
| Prefix-Query über 41k Zeilen ohne passenden Index | Suchlatenz | `limit` + LRU; Messung im E2E-Test; Phase-4-Vollindex als Fallback |
| Blockierender Netz-Op im Agent-Receive-Thread | Tippen stockt | Suche inline (Cache/1 Query), alles andere im Executor |
| `all` mit 41.723 Zeilen | UI-Freeze / Speicher | `truncated`-Vertrag; Info-Text statt Liste |
| Falscher/mehrdeutiger Ticker | Chart öffnet nicht, Backend-Fehler | `has_parquet`-Indikator in der Trefferliste, Klartext in der Statuszeile |
| Broadcast-Antworten bei mehreren Viewern | fremde Panels sehen `request_id` nicht | `request_id`-Korrelation; optional `send_to_client` (Phase 4) |
| Panel „klaut“ Fokus/Tastatur | Chart-Tastennavigation | eigenes Fenster, keine globalen Shortcuts außer `Ctrl+Shift+P` |
| Always-on-top nervt | Bedienkomfort | Pin-Toggle, persistiert |
| Gemischter Rollout Client/Agent | Panel zeigt „keine Antwort“ | beide Seiten ignorieren unbekannte Typen; Fehlerzustand sichtbar; Client-Sync holt die neue Version |
| `STOCK_DATA_NODE_URL` nicht erreichbar | neuer Ticker bleibt ohne Chart-Daten | Add funktioniert trotzdem; Trigger-Fehler nur im Log; Ticker erscheint später über den regulären Sweep |
| Preset-Apply setzt `/tmp/last_chart_state.json` | Restart zeigt das zuletzt angewandte Preset | bewusst akzeptiert (dokumentiert) |

---

## 12. Deployment & Rollback

| Schritt | Kommando / Wirkung |
| :-- | :-- |
| Server-Code (Agent, ControlService) | `./restart.sh chart-viewer-server` (build + recreate). `src` ist read-only in den Container gemountet → Code ist nach dem Neustart aktiv |
| Compose-Änderung (`STOCK_DATA_NODE_URL`) | Teil desselben Neustarts |
| Client-Code (UI) | keine manuelle Aktion: `/api/sync_version` ändert sich mit den Datei-Mtimes, `launch_windows_v2.bat` lädt `/api/sync` beim nächsten Start |
| Verifikation | `curl -s http://127.0.0.1:8766/api/status` (Client verbunden, Ledger), Panel im Client sichtbar, Suche/Watchlist/Preset manuell durchspielen |
| Rollback | `git revert` + `./restart.sh chart-viewer-server`; der Client zieht beim nächsten Start automatisch die alte Src-Version |

---

## 13. Offene Entscheidungen (Empfehlung fett)

1. **Immer im Vordergrund?** → **Ja, Pin standardmäßig aktiv**, per 📌 abschaltbar und persistiert.
2. **Ziel-Bindung: Flag-Gruppe oder explizites Zielfenster?** → **Flag-Gruppe** (E3). Ein Zielfenster-Dropdown wäre als Erweiterung nachrüstbar.
3. **Ticker-Klick: bestehendes Fenster umschalten oder immer ein neues Fenster?** → **bestehendes Fenster der Flag-Gruppe umschalten**, sonst neues öffnen (identisch zum Watchlist-Verhalten); „＋ Neu“ als optionaler Knopf in Phase 4.
4. **Sollen Ticker ohne `has_parquet` in den Treffern erscheinen?** → **Ja, markiert (○)**, weil das Hinzufügen zur Watchlist den Download anstößt; das Chart öffnet erst nach dem Download.
5. **Watchlist-Anlegen/Umbenennen/Löschen im Panel?** → **Phase 4**, v1 nur Ticker-Add/Remove (Anforderung A5 wörtlich).
6. **Namenssuche (Firma→Ticker)?** → **nicht in v1**; `cda_master_universe` hat keine Namensspalte, `company_ticker_mappings` (416 Firmen) deckt nur einen Bruchteil ab.

---

## 14. Implementierungsabschluss (2026-09-25)

| Punkt | Ergebnis |
| :-- | :-- |
| Neue Module | `agent/supabase.py`, `agent/control_service.py`, `ui/control_panel.py` |
| Geänderte Module | `agent/agent_client.py`, `ui/app.py`, `ui/window.py`, `core/event_hub.py`, `config.py`, `transport/base.py\|websocket.py\|in_process.py`, `llm-gateway/docker-compose.yml`, Spec Abschnitt 3.3 |
| Tests | `tests/test_control_service.py` (17), `tests/test_control_panel.py` (19) → Gesamtsuite **139 passed** |
| Baseline-Fehler behoben | `test_criterion_10_viewer_restart_layout_restore` läuft grün, weil `viewer.ready` jetzt zentral in `ViewerApp` über den Connect-Callback gesendet wird (vorher sendete nur der WebSocket-Transport, der In-Process-Transport nicht) |
| Test-Isolation | Die Alt-Tests laufen über `tests/conftest.py` ohne Control-Panel (`control_panel_enabled=False`); die Panel-Tests aktivieren es explizit und bauen es deterministisch ab (Qt-GC-Stabilität) |
| Live-Verifikation (Container) | Suche 18–21 ms kalt / 0 ms warm; `all` = 41.723 Ticker truncated; 27 Watchlisten; 12 Presets; Add/Duplikat(409→`already_exists`)/Remove gegen `zz_cp_verify` (danach gelöscht); `all` → `protected_list` |
| Live-E2E (headless Viewer → echter Agent) | Panel verbindet, lädt 27 Watchlisten + 12 Presets, Suche „NV“ in 20,6 ms (20 Treffer inkl. Typ/Parquet-Marker), Inhalt `10_favorite`, `all` schreibgeschützt, Preset gemerkt, unbekannter Op → `invalid_request` |
| Deployment | `./restart.sh chart-viewer-server`; `STOCK_DATA_NODE_URL` ergänzt; `/api/sync` liefert die neuen Module (Client-Sync beim nächsten Start von `launch_windows_v2.bat`) |
| Offen | Nur noch Bedienung/Feinschliff durch den User am Windows-Client (u. a. Pin-Verhalten, ob „＋ Neu“ gebraucht wird) |

---

## 15. Nachtrag: Watchlist-Bedienung (2026-09-26)

| Anforderung | Umsetzung |
| :-- | :-- |
| Ein Klick in der Watchlist lädt den Chart | `watchlist_list.itemSelectionChanged` → `_on_watchlist_selection_changed` (ersetzt `itemDoubleClicked`). Programmatisches Füllen/Entfernen läuft über `_selection_guard()`, damit das Entfernen einer Zeile den Chart nicht auf den nächsten Ticker umschaltet |
| Dropdown öffnet bei Klick auf das ganze Feld | Event-Filter auf `watchlist_combo.lineEdit()`: `MouseButtonPress` → `showPopup()`/`hidePopup()`; der Pfeil behält sein Standard-Toggle, der Text bleibt editierbar |
| Buttons Kopieren/Verschieben | `⧉ Kopieren` / `⇄ Verschieben` rechts neben `− Entfernen`; `watchlist_dialogs.ask_watchlist()` zeigt eine modale Liste aller editierbaren Listen (ohne Quelle und ohne `all`) |
| Buttons Neu/Löschen neben „WATCHLIST“ | `＋ Neu` → `NewWatchlistDialog` (Name + optionale Vorlagenliste zum Übernehmen der Ticker), `− Löschen` → Auswahlliste + Bestätigung |
| Agent-Operationen | `mutate_watchlist` um `copy` / `move` / `copy_list` / `delete` erweitert; `all` bleibt in jeder Rolle `protected_list`; `copy_list` nutzt `POST /api/watchlists/batch` (replace); `delete` leert ein offenes Watchlist-Fenster und entfernt den Ledger-Eintrag |
| Leere neue Watchlist | `pca_watchlists` speichert nur Ticker-Zeilen: eine leere Liste bleibt lokal (`_pending_watchlists`) und wird mit dem ersten Ticker in der DB materialisiert |
| Tests | `test_watchlist_dialogs.py` (5), Panel-Tests +8, Control-Service-Tests +16 → Gesamtsuite **192 passed** |
| Deployment | `docker restart qjm-chart-viewer-server` (liest `/app/src` per Bind-Mount, kein Rebuild nötig); Live-E2E über `control.request`: add/copy/move/copy_list/delete gegen temporäre Listen, Master-Schutz, danach aufgeräumt |
