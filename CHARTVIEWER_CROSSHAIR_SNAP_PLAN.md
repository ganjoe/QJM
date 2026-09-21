# Chart Viewer — Cursor-Snap auf Candles / X-Achsen-Datenpunkte (TC2000-Verhalten)

Status: Entwurf · Autor: Analyse-Agent · Datum: 2026-09-21
Betroffen: `chart_viewer` (PySide6 Desktop Viewer)

---

## 1. Symptom

Beim Bewegen der Maus über den Chart **springt die Datumsanzeige (Badge) an der X-Achse innerhalb
einer Candle** von einem Datum zum nächsten. Der Sprung passiert nicht am Rand/der Lücke zwischen
zwei Candles, sondern **mitten in der Candle**, genauer am Docht (Candle-Zentrum).

Gewünscht (TC2000): Der X-Cursor rastet auf Candles ein und springt in 1er-Schritten von
Candle zu Candle bzw. von X-Achsen-Datenpunkt zu X-Achsen-Datenpunkt.

---

## 2. Analyse / Root Cause

### 2.1 Relevante Geometrie

| Größe | Formel | Quelle |
|---|---|---|
| Candle-Zentrum | `bar_to_x(i) = anchor_x - (right_index - i) * candle_width_px` | `coords/x_axis.py:55` |
| Pixel → Bar (fraktional) | `x_to_bar(px) = right_index - (anchor_x - px) / candle_width_px` | `coords/x_axis.py:59` |
| Candle-Body | `x_center ± 0.4 * candle_width` | `ui/pane.py:624,651` |
| X-Achsen-Tick + Label | an `bar_to_x(idx)` (= Candle-Zentrum), Text = `bars[idx].t_open` | `ui/pane.py:430-451` |

### 2.2 Der Fehler

Der Datums-Badge in `ui/pane.py:501` verwendet **`int()` = floor**, nicht Nearest-Neighbour:

```python
# ui/pane.py, _render_crosshair()
bar_idx = int(self.x_trans.x_to_bar(cx))          # <-- floor!
t_sec = self.bars[bar_idx].t_open
time_str = dt.strftime("%Y-%m-%d")
```

`floor(x_to_bar)` wechselt erst beim **Candle-Zentrum** (dem Docht), nicht auf halber Strecke
zwischen zwei Candles. Nachgerechnet für Candle 10, `candle_width=8px`:

| Offset vom Zentrum | `x_to_bar` | `int()` (IST, Badge) | `round()` (SOLL) |
|---|---|---|---|
| −4 px (linker Body-Rand) | 9.500 | **9** ❌ | 10 |
| −2 px | 9.750 | **9** ❌ | 10 |
| −1 px | 9.875 | **9** ❌ | 10 |
| 0 px (Docht) | 10.000 | 10 | 10 |
| +2 px (rechter Body) | 10.250 | 10 | 10 |
| +4 px | 10.500 | 10 | 10 |

→ Die **linke Hälfte der Candle-Body zeigt das Datum der VORHERIGEN Candle**; der Wechsel
passiert exakt am Docht. Das ist genau das gemeldete „Springen innerhalb der Candle".

### 2.3 Zusätzliche Inkonsistenz (derselbe Bug, andere Stelle)

Der Broadcast an andere Fenster nutzt dagegen bereits **`round()`**:

```python
# ui/canvas.py:290-294, _broadcast_crosshair_from_x()
bar_idx = self.x_trans.x_to_bar(x_px)
snapped = int(round(bar_idx))          # <-- round
ts = self.window_data.bars[snapped].t_open
self.crosshair_moved.emit(ts, snapped)
```

Damit sind **lokaler Badge (floor)** und **emittierter/synchronisierter Timestamp (round)**
um bis zu einen Bar auseinander. Synced Crosshair (anderes Fenster) und eigenes Badge zeigen
für dieselbe Mausposition ggf. unterschiedliche Daten.

> Hinweis: `round()` ist in Python banker's rounding (`round(2.5) == 2`, `round(3.5) == 4`).
> Für eine deterministische Lösung daher `math.floor(x + 0.5)` verwenden, nicht `round()`.

### 2.4 Was bereits korrekt ist

- Candle-Zentren, X-Achsen-Ticks und `bars[idx].t_open` passen zusammen.
- `XAxisTransform.snap_to_bar()` existiert, snappt aber nur `right_index` (Viewport-Pinning), nicht den Cursor.
- Multi-Window-Sync clampt bereits indexbasiert über Gaps (`EventHub.nearest_bar`).

---

## 3. Zielverhalten (TC2000)

1. Der vertikale X-Cursor **rastet auf das Zentrum der nächstgelegenen Candle** ein
   (`floor(x_to_bar(px) + 0.5)`, clamped auf `[0, len(bars)-1]`).
2. Der Datums-/Zeit-Badge zeigt **immer den `t_open` der gerasteten Candle**.
3. Der Badge wechselt **auf halber Strecke zwischen zwei Candles** (Nearest-Neighbour),
   also in der Lücke — nie innerhalb der Candle.
4. **Eine** Quelle der Wahrheit: gerasteter Index → Badge, Linie, Broadcast/Sync, Measure-Tool.
5. Verhalten bei Lücken (Weekend/Feiertage): weiterhin indexbasiert; der Datumssprung
   Freitag→Montag zwischen zwei benachbarten Candles ist korrekt und gewollt.

---

## 4. Design

### 4.1 Neue zentrale Snap-Funktion

In `coords/x_axis.py`:

```python
def nearest_bar_index(self, pixel_x: float, bar_count: int) -> int:
    """Nächstgelegene Candle (Nearest-Neighbour, half-up)."""
    if bar_count <= 0:
        return 0
    idx = math.floor(self.x_to_bar(pixel_x) + 0.5)
    return max(0, min(bar_count - 1, idx))

def bar_center_for_pixel(self, pixel_x: float, bar_count: int) -> tuple[int, float]:
    idx = self.nearest_bar_index(pixel_x, bar_count)
    return idx, self.bar_to_x(float(idx))
```

### 4.2 Canvas als Single Source of Truth

`ui/canvas.py`:

```python
def _snap_crosshair(self, x_px: float):
    bars = self.window_data.bars if self.window_data else []
    if not bars or x_px < 0:
        return None, None
    return self.x_trans.bar_center_for_pixel(x_px, len(bars))

def _broadcast_crosshair_from_x(self, x_px: float) -> None:
    idx, snapped_x = self._snap_crosshair(x_px)
    if idx is None:
        return
    self.crosshair_moved.emit(self.window_data.bars[idx].t_open, idx)
```

`_apply_crosshair` / `_apply_crosshair_remote` übergeben künftig **`snapped_x`** und **`bar_index`**
an die Panes (statt des rohen Maus-Pixels).

### 4.3 Pane

`ui/pane.py`:

```python
def set_crosshair(self, x, y, is_active, bar_index=None):
    self._crosshair_x = x
    self._crosshair_y = y if is_active else None
    self._is_crosshair_active = is_active
    self._crosshair_bar_index = bar_index
    self.update()

# _render_crosshair(): statt int(x_to_bar):
idx = self._crosshair_bar_index
if idx is None:
    idx = math.floor(self.x_trans.x_to_bar(cx) + 0.5)   # Fallback, konsistent
```

Formatierung pro Timeframe vereinheitlichen:
- `bar_duration >= 86400` → `%Y-%m-%d`
- intraday → `%Y-%m-%d %H:%M`
- X-Achsen-Ticks (`%d. %b`) und Badge aus derselben Formatier-Hilfsfunktion.

### 4.4 Konfiguration

`config.py`:
```python
crosshair_snap_to_bar: bool = True   # ENV: CV_CROSSHAIR_SNAP
```
Default `True` (neues TC2000-Verhalten). `False` = alter Freilauf nur als Notausstieg.

---

## 5. Konkrete Änderungen (Datei für Datei)

| Datei | Änderung |
|---|---|
| `coords/x_axis.py` | `nearest_bar_index()`, `bar_center_for_pixel()` neu |
| `ui/canvas.py` | `_snap_crosshair()`; `_broadcast_crosshair_from_x()` emittiert gerasteten Index; `_apply_crosshair()` + `_apply_crosshair_remote()` übergeben `snapped_x`/`bar_index`; `mouseMove`-Pfad unverändert |
| `ui/pane.py` | `set_crosshair(..., bar_index)`; Badge nutzt `_crosshair_bar_index`; Fallback `floor(x+0.5)`; Datumsformat-Helper; (optional) Measure-Tool-Endpunkte snappen |
| `ui/app.py` | Remote-Sync übergibt `idx` aus `nearest_bar` an `_apply_crosshair_remote` |
| `config.py` | `crosshair_snap_to_bar` + ENV |
| `tests/test_crosshair_snap.py` | neue Tests (siehe §6) |

---

## 6. Testplan

### 6.1 Unit (neu, `tests/test_crosshair_snap.py`)
- `test_snap_uses_nearest_neighbour`: Grenze liegt bei `bar_to_x(i) ± 0.5*cw`, nicht am Zentrum.
- `test_snap_never_changes_inside_body`: für alle `d ∈ [-0.4cw, +0.4cw]` ist der Index konstant.
- `test_badge_index_equals_emitted_index`: Badge-Index == Broadcast-Index für jeden Pixel über 5 Candles (Regression für den floor/round-Mismatch).
- `test_snap_clamps_first_and_last_bar`: Pixel links/rechts außerhalb → Index 0 bzw. `len-1`.
- `test_snap_empty_bars` und `test_snap_ms_timestamps`.
- `test_no_bankers_rounding`: `x_to_bar = 2.5/3.5` → deterministisch 3 bzw. 4.

### 6.2 UI / Integration
- Mausbewegung bei `x = bar_to_x(i) - 0.4cw` und `x = bar_to_x(i) + 0.4cw` → **gleicher** Badge-Text.
- `x = bar_to_x(i) - 0.5cw - ε` → `i-1`; `x = bar_to_x(i) - 0.5cw + ε` → `i`.
- Multi-Window: Quell-Badge-Datum == Ziel-Badge-Datum (Group-Sync).
- Bestehende Tests müssen grün bleiben: `test_coords.py`, `test_crosshair_multi_window.py` (Gap-Clamping), `test_interaction.py`, `test_calendar.py`.

### 6.3 Manuelle QA
- Daily-Chart, weit hineingezoomt: Cursor über die volle Body-Breite → kein Datumswechsel.
- Zoom-out bis Thin-Bar-Mode (<3px): Snap bleibt indexbasiert, kein Flackern.
- Weekend-Lücke: Wechsel Freitag→Montag korrekt an der Lücke.
- Messwerkzeug und Annotationen unverändert/plausibel.

---

## 7. Edge Cases & Entscheidungen

- **Future-Margin (rechtes 10%)**: Cursor clampt auf letzte reale Candle (Index `len-1`) — konsistent mit `_broadcast`. Alternative Projektion vermeiden (kein Look-ahead-Datum).
- **Erste/letzte Candle**: hart clampen; kein Badge außerhalb des geladenen Bereichs.
- **Keine Bars**: Snap liefert `None`, Badge wird nicht gezeichnet (wie heute).
- **Lücken**: indexbasiert (Real-Timestamps der Bars), kein `base_timestamp + idx*duration` für die Anzeige.
- **Banker's Rounding**: `floor(x+0.5)` statt `round()`.
- **Measure-Tool**: Endpunkte ebenfalls auf Candle-Zentren snappen (Delta-Bars ganzzahlig).
- **Tastatur (optional, Folge-Task)**: `Alt+←/→` bewegt den Crosshair bar-weise, ohne Pan.

---

## 8. Aufwand & Rollout

- Kern-Fix + Refactor: **~0,5 Tag** inkl. Unit-Tests.
- Optional (Keyboard-Crosshair, OHLC-Tooltip der gerasteten Candle): +0,5 Tag.
- Reines Visual-Verhalten, keine Wire-/Envelope-Änderung, keine Migration.
- Default `crosshair_snap_to_bar = True`; bei Bedarf per ENV abschaltbar.
- Definition of Done: Neue Tests grün, bestehende Viewer-Tests grün, manuelle QA §6.3 abgehakt.

---

## 9. Akzeptanzkriterien

1. Innerhalb einer Candle ändert sich die Datumsanzeige **nie**.
2. Der Wechsel erfolgt in der Lücke (halbe Distanz zwischen zwei Candle-Zentren).
3. Badge, Linie, Broadcast und Sync verwenden **denselben** gerasteten Index.
4. Für die Rand-Candles und leere Zustände gibt es kein Flackern/keinen Crash.
5. Bestehende Sync- und Gap-Clamping-Tests bleiben grün.

---

## 10. Umsetzungsstand (2026-09-21)

Umgesetzt (Kern-Fix):

| Datei | Änderung |
|---|---|
| `coords/x_axis.py` | `nearest_bar_index()` / `bar_center_for_pixel()` — Nearest-Neighbour via `floor(x + 0.5)` |
| `ui/canvas.py` | `_snap_crosshair()`; Linie + Badge + Broadcast nutzen denselben Index; `_apply_crosshair_remote(..., bar_index)` |
| `ui/pane.py` | `set_crosshair(..., bar_index)`, Badge aus `_crosshair_bar_index`, `_format_crosshair_time()` (Datum, intraday mit Uhrzeit) |
| `ui/app.py` | Crosshair-Sync übergibt den Bar-Index |
| `config.py` | `crosshair_snap_to_bar` (Default `True`, ENV `CV_CROSSHAIR_SNAP`) |
| `tests/test_crosshair_snap.py` | 8 neue Regressionstests |

Tests: `tests/test_crosshair_snap.py` 8/8 grün, Gesamtsuite 84 passed.
Ein vorbestehender, unabhängiger Fehlschlag bleibt:
`test_window_lifecycle.py::test_criterion_10_viewer_restart_layout_restore` —
schlägt auch ohne diese Änderung fehl (per `git stash` verifiziert).

Deployment: Server-Container `qjm-chart-viewer-server` neu gestartet; `/api/sync_version`
liefert einen neuen Hash und `/api/sync` den neuen Code (verifiziert). Der Windows-Client
(10.20.0.25) muss einmal über `launch_windows_v2.bat` neu gestartet werden, damit er das
neue `src` herunterlädt und die Python-Module neu lädt — der Server lädt Client-Code nicht
automatisch neu.

Nicht enthalten (optional, siehe §7): `Alt+←/→`-Crosshair-Steuerung und das Snappen der
Measure-Tool-Endpunkte.
