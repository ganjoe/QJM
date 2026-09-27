# Chart Viewer — Wert-Boxen an der Y-Achse (Crosshair Value Box)

Status: Entwurf · Datum: 2026-09-24 · Betroffen: `chart_viewer` (PySide6 Desktop Viewer)
Ergänzt: `CHARTVIEWER_CROSSHAIR_SNAP_PLAN.md` (Crosshair-Snap auf Candle-Zentren, umgesetzt)

---

## 1. Ziel

Beim Bewegen der vertikalen Crosshair-Linie soll an der Y-Achse **jeder Pane** für **jede Kurve
dieser Pane** ein eingerahmtes Datenfeld den Kurvenwert **an der markierten Bar** anzeigen.
Der Wert hängt nur von der X-Position (Bar-Index) ab — der Cursor muss **nicht** an der Kurve
kleben, um einen Wert abzulesen.

- **Haupt-Pane:** Kurskurve → **Close + Volumen** in einer Box; zusätzlich jede Overlay-Kurve
  der Pane (SMA/EMA/Bollinger …) als eigene Box.
- **Subpanes** (z. B. `volume`, `rs`, `adr`): jede Overlay-Kurve als eigene Box.
- **Schriftgröße:** Faktor **1.5 × X-Achsen-Schriftgröße** (9 pt → 13.5 pt), Faktor konfigurierbar.
- **Bestehende Cursor-Positions-Anzeige bleibt erhalten** (Nutzerentscheidung): der bisherige
  Badge im aktiven Pane zeigt weiterhin den Preis auf Höhe der Maus-Y-Position.
- Gilt für **lokale** Maus-Crosshairs **und** für per **Fenster-Sync** empfangene Crosshairs
  (gleiche `sync_group_id`).

### 1.1 Soll-Bild (schematisch)

```
 Pane "main" (Kerzen + SMA50 + SMA200)                        Pane "volume"
┌──────────────────────────────────────────┬──────────────┐ ┌────────────────────────────┬──────────────┐
│                                ┆         │ ┌──────────┐ │ │                            │ ┌──────────┐ │
│         ╭──── SMA50 ───────────┆──       │ │  185.32  │ │ │           ▂▄▆              │ │  12.4 Mio│ │
│   ██    ██                     ┆         │ │  12,4 Mio│ │ │        ▂▄▆█████            │ └──────────┘ │
│   ██    ██   ← Kerzen          ┆         │ └──────────┘ │ │      ▂▄▆█████████          │              │
│                                ┆         │ ┌──────────┐ │ │                            │              │
│              ╭── SMA200 ───────┆──       │ │  172.08  │ │ │                            │              │
└────────────────────────────────┆─────────┴─└──────────┘─┘ └────────────────────────────┴──────────────┘
                                 ▲
                    vertikale Crosshair-Linie (gesnappter Bar-Index)
```

Jede Box sitzt auf der **Höhe ihres Wertes** (y = `price_to_y(Wert)`) im Y-Achsen-Gutter,
rechtsbündig am Pane-Rand. Bei Kollision werden die Boxen gestapelt (§4.2).

---

## 2. Ist-Zustand

### 2.1 Was heute existiert (bleibt unverändert)

`ui/pane.py:483-503` (`_render_crosshair`): Im **aktiven** Pane wird bei gesetztem
`_crosshair_y` eine horizontale Linie plus ein Preis-Badge im Gutter gezeichnet:

```python
if self._is_crosshair_active and self._crosshair_y is not None:
    cy = int(self._crosshair_y)
    price = self.y_trans.y_to_price(cy)      # <-- Wert der CURSOR-POSITION
    badge_rect = QRectF(chart_w, cy - 10, 60, 20)
```

Das ist die **Cursor-Positions-Anzeige** und wird von dieser Erweiterung nicht angetastet.

### 2.2 Was fehlt

Für die vertikale Linie gibt es keinerlei Wertanzeige. Der Nutzer muss den Cursor exakt auf die
Kurve legen; in Panes ohne Maus (Remote-Sync) gibt es gar keinen ablesbaren Wert.

### 2.3 Vorhandene Bausteine (wiederverwendbar)

| Baustein | Ort | Nutzen |
|---|---|---|
| `_crosshair_bar_index` (gesnappter Index, in **allen** Panes gesetzt) | `ui/pane.py:89`, `:454-464` | Wert-Index |
| `_indexed_overlays`: `{ov_id: {overlay, indices[float], pts[(bar_i, val, val2)]}}` | `ui/pane.py:117-142` | O(log n)-Lookup je Kurve via `bisect` |
| `bars[idx].close` / `.volume` | `models/entities.py:46-56` | Kurskurve im Haupt-Pane |
| `nearest_bar_index()` / `bar_center_for_pixel()` | `coords/x_axis.py:64-84` | Fallback-Index |
| `price_to_y()` (linear **und** log) | `coords/y_axis.py:101-118` | y-Pixel der Box |
| X-Achsen-Font `setPointSize(9)` | `ui/pane.py:426-428` (X), `:337-339` (Y) | Referenzschrift |
| Geometrie `Y_AXIS_WIDTH=70`, `X_AXIS_HEIGHT=22` | `ui/pane.py:17-18` | Box-Raum |
| `_de_num` / `_compact_number` | `orchestrator.py:119-135` | Format-Vorbild (wird geteilt) |
| Crosshair-Verteilung inkl. `bar_index` an alle Panes | `ui/canvas.py:310-328` | **keine Canvas-Änderung nötig** |

### 2.4 Abgrenzung

Kein Wire-/Envelope-/Snapshot-Format, keine neue MCP-Action, keine Migration, keine Änderung an
Achsen-Labels, Zoom, Pan, Measure-Tool oder Annotationen. Rein clientseitiges Rendering.

---

## 3. Soll-Verhalten (Regeln)

1. Boxen erscheinen, sobald eine vertikale Crosshair-Linie sichtbar ist (`_crosshair_x is not None`)
   und ein Bar-Index bestimmbar ist — **in jeder Pane**, nicht nur im aktiven.
2. Der angezeigte Wert hängt **ausschließlich vom Bar-Index (X)** ab, nie von der Cursor-Y-Position.
3. Pro Pane eine Box je Kurve:
   - Haupt-Pane: Kurskurve (Close **und** Volumen, zwei Zeilen) + jede Overlay-Kurve der Pane.
   - Subpane: jede Overlay-Kurve (`line`/`histogram` → `value`; `band` → `value` und
     `value2` als je eigene Box). `marker`-Overlays werden nicht gerendert → keine Box.
4. Box-Position: `y = price_to_y(Wert)`, geklemmt in den Content-Bereich; bei Kollision gestapelt.
5. Schrift: `X_AXIS_FONT_PT * crosshair_value_box_font_factor * VALUE_BOX_SCALE`
   (Default `9 * 1.5 * 0.7 ≈ 9.45 pt`, siehe Nachtrag §11). Beide Zeilen einer Box nutzen diese
   Größe, die Volumen-Zeile wird gedimmt.
6. Optik: dunkler opaker Hintergrund, 1px-Rahmen **in der Kurvenfarbe** plus 3px-Farbstreifen
   links (eindeutige Zuordnung Box → Kurve), Zahl in Weiß.
7. Remote-Sync: Boxen werden auch ohne Maus im Fenster gezeichnet.
8. Ohne Crosshair (Maus verlässt das Pane / kein Sync / Fenster nicht aktiv) → die Boxen
   **bleiben sichtbar** und zeigen den **neuesten Wert jeder Kurve** (Nachtrag §11). Sie
   verschwinden nur, wenn die Pane keine Kurven/Bars hat (dann gibt es keine Werte).

---

## 4. Design

### 4.1 Datenmodell und Wert-Ermittlung

Neu in `ui/pane.py` (Modul-Ebene):

```python
X_AXIS_FONT_PT: float = 9.0        # Referenz: Schrift der X-Achsen-Labels (heute hartkodiert 9)
VALUE_BOX_BG = "#1E222D"
VALUE_BOX_TEXT = "#FFFFFF"
VALUE_BOX_SECONDARY = "#9CA3AF"
VALUE_BOX_PRICE_COLOR = "#D1D4DC"

@dataclass(frozen=True)
class CrosshairValue:
    key: str                        # "price" | overlay_id | f"{overlay_id}:upper" | ":lower"
    color: str                      # Rahmen/Farbstreifen (Kurvenfarbe)
    value: float                    # für die y-Position (price-Box: Close)
    text: str                       # Primärzeile (groß, weiß)
    secondary_text: str | None = None   # optionale 2. Zeile (Volumen, gedimmt)
```

Neue Methode `ChartPane.crosshair_values(bar_idx)`:

```python
def crosshair_values(self, bar_idx: int) -> list[CrosshairValue]:
    """Wert jeder Kurve dieser Pane am gegebenen Bar-Index (x-only, nie Cursor-Y)."""
    out: list[CrosshairValue] = []

    # 1) Haupt-Pane: Kurskurve = Close + Volumen
    if self.is_main and self.bars and 0 <= bar_idx < len(self.bars):
        bar = self.bars[bar_idx]
        out.append(CrosshairValue(
            key="price", color=VALUE_BOX_PRICE_COLOR, value=float(bar.close),
            text=format_value(bar.close),
            secondary_text=format_volume(bar.volume),
        ))

    # 2) Jede Overlay-Kurve dieser Pane
    for ov_id, item in getattr(self, "_indexed_overlays", {}).items():
        ov = item["overlay"]
        if ov.type == "marker":
            continue
        color = (ov.style or {}).get("color", "#26A69A")
        val, val2 = self._overlay_value_at(item, bar_idx)
        if ov.type == "band":
            if val is not None:
                out.append(CrosshairValue(f"{ov_id}:upper", color, float(val), format_value(val)))
            if val2 is not None:
                out.append(CrosshairValue(f"{ov_id}:lower", color, float(val2), format_value(val2)))
        elif val is not None:
            out.append(CrosshairValue(ov_id, color, float(val), format_value(val)))
    return out
```

```python
def _overlay_value_at(self, item, bar_idx: int) -> tuple[float | None, float | None]:
    """Wert eines Overlays an (oder nahe) bar_idx; None, wenn dort kein Punkt liegt."""
    indices = item["indices"]
    if not indices:
        return None, None
    target = float(bar_idx)
    pos = bisect.bisect_left(indices, target)
    cand = [i for i in (pos, pos - 1) if 0 <= i < len(indices)]
    if not cand:
        return None, None
    best = min(cand, key=lambda i: abs(indices[i] - target))
    tol = float(getattr(self.config, "crosshair_value_box_snap_bars", 1.0))
    if abs(indices[best] - target) > tol:
        return None, None
    _, val, val2 = item["pts"][best]
    return val, val2
```

Einheitliche Index-Auflösung (ersetzt den Inline-Fallback des X-Achsen-Badges in `:507-511`,
damit Badge und Wert-Boxen **denselben** Bar meinen):

```python
def _resolve_crosshair_bar_index(self, cx: float) -> int | None:
    idx = self._crosshair_bar_index
    if idx is None and self.bars:
        idx = math.floor(self.x_trans.x_to_bar(cx) + 0.5)   # half-up, nie floor()
    if idx is None:
        return None
    return max(0, min(len(self.bars) - 1, int(idx)))
```

### 4.2 Box-Layout (Kollision, Clamping, Breite)

```python
def _layout_value_boxes(self, values, content_h: float, font: QFont
                        ) -> list[tuple[CrosshairValue, QRectF]]:
    fm = QFontMetrics(font)
    line_h = float(fm.height())
    pad_v, pad_h, stripe_w, gap = 3.0, 6.0, 3.0, 2.0
    max_w = float(getattr(self.config, "crosshair_value_box_max_width_px", 150.0))
    min_w = float(getattr(self.config, "crosshair_value_box_min_width_px", 62.0))
    x_right = float(self.width()) - 3.0            # rechtsbündig am Pane-Rand

    items = []
    for v in values:
        text_w = float(fm.horizontalAdvance(v.text))
        if v.secondary_text:
            text_w = max(text_w, float(fm.horizontalAdvance(v.secondary_text)))
        n_lines = 2 if v.secondary_text else 1
        w = max(min_w, min(max_w, text_w + 2 * pad_h + stripe_w))
        h = n_lines * line_h + 2 * pad_v
        y = self.y_trans.price_to_y(v.value) - h / 2.0        # Wunschposition = Wertposition
        items.append([v, w, h, y])

    items.sort(key=lambda it: it[3])                          # nach Wunsch-y
    for _ in range(2):                                        # 2 Pässe: unten -> oben Korrektur
        prev_bottom = -1e9
        for it in items:
            it[3] = max(it[3], prev_bottom + gap, 2.0)
            prev_bottom = it[3] + it[2]
        if prev_bottom > content_h - 2.0:
            next_top = content_h - 2.0
            for it in reversed(items):
                it[3] = min(it[3], next_top - it[2])
                next_top = it[3] - gap

    return [(it[0], QRectF(x_right - it[1], it[3], it[1], it[2])) for it in items]
```

Eigenschaften: Wertposition bleibt erhalten, solange nichts kollidiert; Kollisionen schieben nach
unten; bei Überlauf wird der Stapel von unten zurückgeschoben. Im Extremfall (viele Kurven in sehr
kleiner Pane) bleiben Boxen im Bereich und dürfen sich überlappen — kein Layout-Crash.

> **Skalierung (Nachtrag §11):** Alle Pixelmaße dieses Blocks sind mit `VALUE_BOX_SCALE` (0.7)
> multipliziert — Padding, Farbstreifen, Abstand sowie die Config-Grenzen `max_w`/`min_w`.
> Zusammen mit der skalierten Schrift sind die Felder damit rund 30 % kleiner als im Entwurf.

### 4.3 Rendering (nur Live-Layer)

Die Boxen werden in `_render_crosshair()` gezeichnet (nicht im Pixmap-Cache), da sie sich mit
jeder Mausbewegung ändern. **Zeichenreihenfolge:** vertikale Linie → Wert-Boxen → horizontale Linie
+ **Legacy-Cursor-Badge zuletzt**, damit die Cursor-Positions-Anzeige nie verdeckt wird.

```python
        # ── Wert-Boxen: alle Kurven dieser Pane am gesnappten Bar ──────────
        if getattr(self.config, "crosshair_value_box_enabled", True):
            bar_idx = self._resolve_crosshair_bar_index(cx)
            if bar_idx is not None:
                values = self._value_cache_for(bar_idx)
                if values:
                    box_font = QFont(painter.font())
                    box_font.setPointSizeF(
                        X_AXIS_FONT_PT * float(getattr(
                            self.config, "crosshair_value_box_font_factor", 1.5))
                    )
                    painter.setFont(box_font)
                    for v, rect in self._layout_value_boxes(values, content_h, box_font):
                        self._draw_value_box(painter, v, rect)
```

```python
def _draw_value_box(self, painter: QPainter, v: CrosshairValue, rect: QRectF) -> None:
    painter.fillRect(rect, QColor(VALUE_BOX_BG))
    painter.setPen(QPen(QColor(v.color), 1))
    painter.drawRect(rect)
    painter.fillRect(QRectF(rect.left() + 1, rect.top() + 1, 3, rect.height() - 2),
                     QColor(v.color))                       # Farbstreifen = Kurvenzuordnung
    text_rect = rect.adjusted(8, 1, -4, -1)
    painter.setPen(QColor(VALUE_BOX_TEXT))
    if v.secondary_text:
        half = text_rect.height() / 2.0
        painter.drawText(QRectF(text_rect.left(), text_rect.top(), text_rect.width(), half),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, v.text)
        painter.setPen(QColor(VALUE_BOX_SECONDARY))
        painter.drawText(QRectF(text_rect.left(), text_rect.top() + half,
                                text_rect.width(), half),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         v.secondary_text)
    else:
        painter.drawText(text_rect,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, v.text)
```

### 4.4 Formatierung

Das X-Achsen-Badge und der Legacy-Badge nutzen heute `f"{v:.2f}"` bzw. `f"{v:.0f}"`
(Punkt als Dezimaltrenner). Die Wert-Box übernimmt diese Achsen-Konvention und ergänzt
Kompaktformate für große Zahlen. Dafür werden `_de_num` / `_compact_number` aus
`orchestrator.py` in ein kleines, UI-freies Modul **`src/chart_viewer/formatting.py`**
verschoben (Orchestrator importiert sie von dort; Verhalten unverändert) und um
`format_value()` / `format_volume()` erweitert:

```python
def format_value(v: float) -> str:
    """Kurs-/Indikatorwert: Achsen-kompatibel (Punkt), kompakt bei großen Zahlen."""
    a = abs(v)
    if a >= 1e6:  return compact_number(v)     # "1,08 Mrd."
    if a >= 1000: return f"{v:.0f}"            # "45000"
    if a >= 1:    return f"{v:.2f}"            # "185.32"
    return f"{v:.4f}"                          # "0.0087" (ADR, kleine Werte)

def format_volume(v: float) -> str:
    return compact_number(v)                   # "12,4 Mio." / "1,08 Mrd."
```

`ui/pane.py` importiert daraus `from chart_viewer.formatting import format_value, format_volume`
(neu außerdem: `from dataclasses import dataclass` und `QFontMetrics` aus `PySide6.QtGui`).

### 4.5 Konfiguration (`config.py`)

```python
    # Crosshair value boxes (Y-axis readout per curve at the snapped cursor bar)
    crosshair_value_box_enabled: bool = True           # ENV CV_CROSSHAIR_VALUE_BOX
    crosshair_value_box_font_factor: float = 1.5       # ENV CV_CROSSHAIR_VALUE_FONT_FACTOR
    crosshair_value_box_snap_bars: float = 1.0         # ENV CV_CROSSHAIR_VALUE_SNAP_BARS
    crosshair_value_box_max_width_px: float = 150.0    # ENV CV_CROSSHAIR_VALUE_MAX_WIDTH_PX
    crosshair_value_box_min_width_px: float = 62.0     # ENV CV_CROSSHAIR_VALUE_MIN_WIDTH_PX
```

plus `from_env()`-Parsing analog zu den bestehenden Feldern. `X_AXIS_FONT_PT` ersetzt die
beiden hartkodierten `setPointSize(9)` in `_render_x_axis` / `_render_y_axis`, damit die
Kopplung „Box = 1.5 × X-Achse“ im Code sichtbar ist.

### 4.6 Performance

- `_render_crosshair()` läuft bei jeder Mausbewegung pro Pane im Live-Layer — das bleibt so.
- Pro Kurve **ein** `bisect` (O(log n)) + Formatierung; typisch ≤ 6 Kurven/Pane → vernachlässigbar.
- Cache gegen wiederholtes Formatieren bei vertikaler Mausbewegung (gleicher `bar_idx`):

```python
self._value_cache: tuple[int, list[CrosshairValue]] | None = None

def _value_cache_for(self, bar_idx: int) -> list[CrosshairValue]:
    if self._value_cache is None or self._value_cache[0] != bar_idx:
        self._value_cache = (bar_idx, self.crosshair_values(bar_idx))
    return self._value_cache[1]
```

  Invalidierung in `set_data()` **und** `mark_dirty()` (Live-Ticks mutieren `bars[-1]` in
  place, ohne `set_data()` aufzurufen → Cache muss dort verworfen werden).
- Kein Netz-, DB- oder Dateizugriff; keine Wire-/Envelope-Änderung.

---

## 5. Datei-für-Datei-Änderungen

| Datei | Änderung | Umfang |
|---|---|---|
| `ui/pane.py` | neue Imports (`dataclass`, `QFontMetrics`, `formatting`); `X_AXIS_FONT_PT`, `CrosshairValue`, `crosshair_values()`, `_overlay_value_at()`, `_resolve_crosshair_bar_index()`, `_layout_value_boxes()`, `_draw_value_box()`, `_value_cache_for()`, Rendering in `_render_crosshair()`, Cache-Invalidierung, Badge-Fallback auf den Helper umstellen | ~160 Zeilen |
| `src/chart_viewer/formatting.py` (neu) | `de_num`, `compact_number` (aus Orchestrator), `format_value()`, `format_volume()` | ~60 Zeilen |
| `orchestrator.py` | Import der verschobenen Formatter (Verhalten unverändert, `_de_num`/`_compact_number` bleiben als Aliase) | ~4 Zeilen |
| `config.py` | 5 neue Felder + ENV-Parsing | ~10 Zeilen |
| `tests/test_crosshair_value_box.py` (neu) | §6.1 | ~220 Zeilen |
| `tests/test_formatting.py` (neu) | Formatter-Unit-Tests | ~40 Zeilen |
| `ui/canvas.py` | **keine Änderung** (verteilt `bar_index` bereits an alle Panes, auch remote) | 0 |
| `CHARTVIEWER_VALUE_BOX_PLAN.md` | dieser Plan; nach Umsetzung Abschnitt „Umsetzungsstand“ | — |

---

## 6. Testplan

Ausführung: `cd /home/daniel/QJM/chart_viewer && .venv/bin/python -m pytest tests -q`
(Baseline vor der Änderung: **84 passed, 1 failed** — der vorbestehende, unabhängige Fehlschlag
`test_window_lifecycle.py::test_criterion_10_viewer_restart_layout_restore`.)

### 6.1 Neue Unit-Tests (`tests/test_crosshair_value_box.py`, offscreen wie die übrigen UI-Tests)

1. `test_value_is_taken_from_bar_index_not_cursor_y` — `set_crosshair(x, y=999, active=True,
   bar_index=5)` → Werte == Bar 5; identischer Aufruf mit `y=10` → identische Werte.
2. `test_all_curves_of_pane_get_a_box` — Pane mit 2 Linien + 1 Band + Close → 5 Einträge
   (Close, Linie A, Linie B, Band-Oberkante, Band-Unterkante).
3. `test_main_pane_box_contains_close_and_volume` — `text` == Close, `secondary_text` ==
   kompaktes Volumen.
4. `test_missing_value_hides_curve` — Overlay beginnt erst bei Bar 10: Bar 3 → kein Eintrag,
   Bar 12 → Eintrag.
5. `test_snap_tolerance` — Overlay-Punkt bei Bar 7, Crosshair Bar 8: mit
   `crosshair_value_box_snap_bars=1.0` gefunden, mit `0.0` nicht.
6. `test_layout_boxes_do_not_overlap` — 6 Kurven mit nahezu identischem Wert → Rechtecke
   überlappen nicht und liegen innerhalb `[0, content_h]`.
7. `test_layout_survives_tiny_pane` — Pane 60px hoch, 6 Boxen → keine Exception, alle im Bereich.
8. `test_font_is_coupled_to_x_axis` — `13.5 == X_AXIS_FONT_PT * 1.5`; mit
   `font_factor=2.0` doppelte Boxhöhe.
9. `test_remote_crosshair_shows_values` — `canvas._apply_crosshair_remote(px, idx)` →
   Werte vorhanden, `_is_crosshair_active is False`, Legacy-Badge aus (`_crosshair_y is None`).
10. `test_no_crosshair_no_boxes` — ohne Crosshair liefert `crosshair_values` zwar Werte,
    aber das Rendering zeichnet keine Box (Pixelvergleich/Guard).
11. `test_value_box_pixels_in_gutter` — Pixel-Smoke-Test: Mit Crosshair erscheint die
    Kurvenfarbe als Rahmen/Streifen im Gutter (`x > chart_w`), ohne Crosshair nicht
    (Muster wie `test_interaction.py::test_thin_indicator_line_is_crisp_one_pixel`).
12. `test_badge_and_box_share_bar_index` — X-Achsen-Badge-Datum und Wert-Box nutzen denselben
    `bar_idx` (Regressionsschutz zur Snap-Logik).

### 6.2 Integration / Multi-Pane

- Canvas mit `main` + `volume`-Overlay: Volume-Pane zeigt den Histogrammwert, Main-Pane
  Close + Volumen — beide über denselben Crosshair-Bar.
- Zwei Fenster in derselben Sync-Gruppe: Remote-Crosshair → Wert-Boxen im Zielfenster.
- Bestehende Suiten bleiben grün: `test_crosshair_snap.py`, `test_crosshair_multi_window.py`,
  `test_interaction.py`, `test_coords.py`, `test_calendar.py`, `test_screenshot.py`.

### 6.3 Manuelle QA (Screenshot über MCP `manage_chart_viewer` → `SCREENSHOT`)

- `DISPLAY_STOCK` mit Preset (z. B. `trend_template`): Main + Subpanes.
- Maus **horizontal** bewegen: Boxen wandern mit, Zahlen wechseln exakt beim Bar-Wechsel
  (synchron zum Datums-Badge).
- Maus **vertikal** bewegen: Zahlen bleiben konstant (Kernkriterium).
- Zoom-out (Thin-Bar-Mode) und Weekend-Lücke: Werte folgen dem gerasteten Bar.
- Zwei Fenster gleicher Sync-Gruppe: Remote-Linie zeigt Werte im Zielfenster.
- 640×480-Screenshot: Lesbarkeit, keine Überlappung, Box im Gutter bzw. kontrolliert breiter.
- Volumen-/Indikator-Pane und Haupt-Pane gleichzeitig prüfen.

---

## 7. Edge Cases & Entscheidungen

### 7.1 Vom Nutzer entschieden

| Thema | Entscheidung |
|---|---|
| Umfang | **Alle Kurven** jeder Pane bekommen je eine Box |
| Haupt-Pane | **Close + Volumen** in einer Box (zwei Zeilen) |
| Remote-Sync | Boxen werden **auch** bei fremdem Crosshair gezeigt |
| Bestehende Funktion | Cursor-Positions-Badge **bleibt erhalten** und wird ergänzt |

### 7.2 Technische Entscheidungen

| Thema | Entscheidung | Begründung |
|---|---|---|
| Position | y = Wertposition, bei Kollision gestapelt | direkte visuelle Zuordnung Box ↔ Kurve |
| Boxbreite | rechtsbündig am Pane-Rand, min. 62px, max. 150px | Gutter ist 70px; breite Volumenwerte ragen kontrolliert in den Chart |
| Band | 2 Boxen (Ober-/Unterkante) | konsistent „eine Box je Kurve“; Alternative: eine Box mit 2 Zeilen |
| Marker-Overlays | keine Box | werden vom Renderer nicht gezeichnet |
| Fehlender Wert (Warm-up/None) | keine Box für diese Kurve | keine erfundenen Werte |
| Toleranz | Default 1.0 Bar (konfigurierbar) | robust gegen minimale Zeitstempel-Offsets |
| Wert außerhalb des Y-Bereichs | Box an den Rand geklemmt | Wert bleibt ablesbar; optional ▲/▼-Marker als Folge-Task |
| Überlauf (viele Kurven, kleine Pane) | Stapel geklemmt, Überlappung erlaubt | kein Layout-Crash, definiertes Verhalten |
| Legacy-Badge vs. Boxen | Legacy-Badge zuletzt (oben) | Cursor-Marker nie verdeckt; kein Jitter durch wandernde Boxen |
| Schrift | 13.5 pt, normale Stärke, Zahl weiß | Nutzeranforderung (1.5 × X-Achse) |
| Zahlenformat | wie Y-Achse (Punkt), Volumen deutsch kompakt | Konsistenz Achse/Topbar; Alternative: `12.4M` |
| Log-Skala | keine Sonderbehandlung | `price_to_y()` behandelt log bereits |
| Snap deaktiviert (`CV_CROSSHAIR_SNAP=false`) | Index trotzdem aus `nearest_bar_index` | Werte bleiben korrekt |
| Pane ohne Bars | keine Boxen | `_indexed_overlays` ist dann leer |

---

## 8. Aufwand & Rollout

- **Aufwand:** ~1 Tag inkl. Tests (Kern ~0,5 Tag, Tests/QA ~0,5 Tag).
- **Risiko:** gering — additiv, rein visuell, per ENV abschaltbar
  (`CV_CROSSHAIR_VALUE_BOX=false` ⇒ exakt heutiges Verhalten).
- **Deployment:** `chart_viewer/src` ist im Container read-only **bind-gemountet**
  (`llm-gateway/docker-compose.yml`, Service `chart-viewer-server`); `/api/sync_version`
  hasht mtime+size des Client-`src` und ändert sich nach dem Speichern automatisch.
  1. Optional (etabliertes Verfahren, empfohlen wegen `orchestrator.py`/`formatting.py`):
     `cd /home/daniel/QJM && ./restart.sh chart-viewer-server`
  2. Verifizieren: `curl -s http://10.20.0.23:8766/api/sync_version` liefert neuen Hash,
     `/api/sync` enthält den neuen Code.
  3. Windows-Client (10.20.0.25) einmal über `launch_windows_v2.bat` neu starten — er lädt das
     neue `src`-Bundle; der laufende Viewer lädt Client-Code nicht automatisch neu.
- **Definition of Done:** neue Tests grün, Gesamtsuite ohne **neue** Fehlschläge (Baseline
  84 passed / 1 vorbestehender Fehlschlag), manuelle QA §6.3 abgehakt.

---

## 9. Akzeptanzkriterien

1. Für **jede Kurve jeder Pane** existiert an der Y-Achse ein eingerahmtes Wertfeld, das den Wert
   **des von der vertikalen Linie markierten Bars** zeigt.
2. **Vertikale** Mausbewegung ändert keinen angezeigten Wert; **horizontale** Bewegung wechselt
   exakt mit dem gesnappten Bar-Index (Wert und Datums-Badge gehören zum selben Bar).
3. Die Zahl ist **konfigurierbar skaliert** (`X_AXIS_FONT_PT × font_factor × VALUE_BOX_SCALE`,
   im Default `9 × 1.5 × 0.7 ≈ 9.45 pt`).
4. Haupt-Pane zeigt **Close + Volumen**; Subpanes zeigen ihre Overlay-Werte; Bänder Ober- und
   Unterkante.
5. Funktioniert in **allen Panes** und bei **Remote-Sync**; die bestehende Cursor-Positions-Anzeige
   bleibt unverändert erhalten.
6. Keine Überlappung der Boxen (außer im dokumentierten Extremfall), keine Box verlässt den
   Pane-Bereich, kein Flackern bei Thin-Bar/leeren Daten.
7. Bestehende Tests bleiben grün (bis auf den dokumentierten vorbestehenden Fehlschlag).

---

## 10. Umsetzungsstand

Umgesetzt am 2026-09-24 (vollständig, inkl. Tests und Sichtprüfung):

| Datei | Änderung |
|---|---|
| `ui/pane.py` | `X_AXIS_FONT_PT=9.0`, `CrosshairValue`, `crosshair_values()`, `_overlay_value_at()`, `_resolve_crosshair_bar_index()`, `_value_box_font()`, `_value_cache_for()`, `_layout_value_boxes()`, `_draw_value_box()`; Rendering in `_render_crosshair()` zwischen vertikaler Linie und Cursor-Badge; Cache-Invalidierung in `set_data()`/`mark_dirty()`; beide Achsen-Label-Fonts nutzen `X_AXIS_FONT_PT`; das X-Achsen-Datum nutzt denselben `_resolve_crosshair_bar_index()` |
| `src/chart_viewer/formatting.py` (neu) | `de_num`, `compact_number` (aus `orchestrator.py` verschoben) + `format_value`, `format_volume` |
| `orchestrator.py` | bezieht `_de_num`/`_compact_number` per Alias-Import aus `formatting` (Verhalten unverändert) |
| `config.py` | 5 neue Felder + ENV-Parsing (`CV_CROSSHAIR_VALUE_BOX`, `CV_CROSSHAIR_VALUE_FONT_FACTOR`, `CV_CROSSHAIR_VALUE_SNAP_BARS`, `CV_CROSSHAIR_VALUE_MAX_WIDTH_PX`, `CV_CROSSHAIR_VALUE_MIN_WIDTH_PX`) |
| `tests/test_crosshair_value_box.py` (neu) | 13 Tests (Wertquelle, alle Kurven, Bänder, Lücken, Toleranz, Layout, Font-Kopplung, Remote-Sync, Multi-Pane, Pixel-Smoke) |
| `tests/test_formatting.py` (neu) | 5 Tests |

**Verifikation**

- Neue Tests: **18/18 grün** (`test_crosshair_value_box.py` 13, `test_formatting.py` 5).
- Gesamtsuite: **102 passed, 1 failed** — der Fehlschlag ist der bereits vor der Änderung
  dokumentierte, unabhängige `test_window_lifecycle.py::test_criterion_10_viewer_restart_layout_restore`
  (Baseline vorher: 84 passed, 1 failed).
- Sichtprüfung (Offscreen-Render, `dsh_playground/cv_value_box_demo.png`): 3 Panes
  (main/volume/rsi). Haupt-Pane: `136.51` + `12,43 Mio.` (Close+Volumen), SMA50 `134.95` (blau),
  SMA200 `132.15` (orange); Volume-Pane `12,43 Mio.`; RSI-Pane `22.30` (violett). Alle Boxen
  sitzen auf der Höhe ihres Werts, Schrift 13.5 pt (X-Achse 9 pt), Rahmen in Kurvenfarbe.

**Deployment**

- `/api/sync_version` = `add8e4d5f361d9f403ccdff34ed3c08aef7e21c094cd26f7dd454d96e4fb18c6`;
  `/api/sync` liefert `src/chart_viewer/formatting.py` und die neue `ui/pane.py`
  (im Bundle verifiziert).
- Container `qjm-chart-viewer-server` neu gestartet, `/api/status` = healthy, Client verbunden.
- **Offen (manuell am Windows-Rechner):** Viewer einmal über `launch_windows_v2.bat` neu starten —
  erst dann lädt der Client das neue `src`-Bundle und zeigt die Wert-Boxen.

**Abweichungen/Präzisierungen gegenüber dem Entwurf**

- `format_value()`: |v| ≥ 1e6 kompakt, sonst 2 Nachkommastellen (Punkt), < 1 vier
  Nachkommastellen — hält die Box schmal und trotzdem präzise (Entwurf hatte zusätzlich eine
  Ganzzahl-Stufe ab 1000, die z. B. 4500.25 auf 4500 gerundet hätte).
- Der Legacy-Cursor-Badge wird weiterhin zuletzt gezeichnet; im Überlappungsfall kann er eine
  Wert-Box teilweise verdecken (bewusste Entscheidung, §7.2).

---

## 11. Nachtrag 2026-09-26 — Daueranzeige ohne Crosshair + ~30 % kleinere Felder

Nutzerwunsch: Die Wert-Boxen sollen **nicht mehr verschwinden**, wenn der Cursor das Pane verlässt
bzw. das Chartfenster nicht aktiv ist (z. B. während der Arbeit im Control Center). Stattdessen
zeigen sie den **Wert am Ende der Zahlenreihe (neuester Wert)**. Zusätzlich sollten die Felder um
**ca. 30 % kleiner** werden.

### 11.1 Verhalten

| Situation | Vorher | Jetzt |
|---|---|---|
| Crosshair sichtbar (Maus im Pane, Remote-Sync) | Boxen am markierten Bar | unverändert |
| Maus verlässt das Pane / Fenster wird deaktiviert | **keine Boxen** (`_render_crosshair` stieg aus) | Boxen bleiben stehen und zeigen den **neuesten Wert jeder Kurve** |
| Crosshair wieder aktiv | Boxen am Bar | unverändert (Cache wird pro Bar-Index geführt) |

- `ChartPane.latest_values()` liefert je Kurve den **letzten vorhandenen Punkt** (`pts[-1]`).
  Eine Kurve, die den Kurs um ein bis zwei Bars nachläuft, zeigt damit ihren neuesten Wert statt
  leer zu bleiben; die strenge Regel „kein Wert am markierten Bar ⇒ keine Box“ gilt weiterhin nur
  für den Crosshair-Fall (`crosshair_values()`).
- `_value_cache_for(bar_idx)` akzeptiert `None` als Schlüssel = Idle-Auslesung; `mark_dirty()`
  (Live-Ticks) verwirft sie wie bisher.
- `ChartWindow.changeEvent()` meldet Aktivierungswechsel an `ChartCanvas.set_window_active()`;
  beim Fokusverlust wird der Crosshair verworfen, weil die Maus das Chart dann nicht mehr trackt.
- Ohne Crosshair wird **keine** vertikale Linie, kein Preis-Badge und kein Datums-Badge gezeichnet —
  nur die Felder.

### 11.2 Größe

Neue Modulkonstante `VALUE_BOX_SCALE = 0.7` in `ui/pane.py`. Sie multipliziert **Schrift und
Chrome**: `pad_v`, `pad_h`, Farbstreifen, `gap`, `min_w`/`max_w` (Config-Werte) und den
Text-Einzug. Die Default-Schrift liegt damit bei 9.45 pt statt 13.5 pt; Höhe und Breite einer Box
schrumpfen messbar auf ~70 % (Test `test_value_boxes_are_about_thirty_percent_smaller`).

### 11.3 Dateien & Tests

| Datei | Änderung |
|---|---|
| `ui/pane.py` | `VALUE_BOX_SCALE`, `latest_values()`, `_price_value()`, `_overlay_boxes()` (aus `crosshair_values()` herausgelöst), `_value_cache_for(None)`, `_render_crosshair()` zeichnet die Boxen unabhängig von der Linie, skalierte Geometrie |
| `ui/canvas.py` | `set_window_active(active)` — Fokusverlust verwirft den Crosshair |
| `ui/window.py` | `changeEvent()` reicht `ActivationChange`/`WindowActivate`/`WindowDeactivate` an die Canvas weiter |
| `tests/test_crosshair_value_box.py` | 5 neue Tests (Idle-Werte, nachlaufende Kurve, Cache-Schlüssel, Fenster-Deaktivierung, ChartWindow-Wiring), Font-/Größentest angepasst, Pixel-Smoke-Test prüft beide Zustände |
| `tests/test_interaction.py` | `test_thin_indicator_line_is_crisp_one_pixel` misst nur die Plotfläche — die Boxen im Gutter unterscheiden sich zwischen den beiden Renderings |

**Verifikation**

- `tests/test_crosshair_value_box.py`: **19/19 grün**; Gesamtsuite: **198 passed** (offscreen).
- Sichtprüfung (Offscreen-Render): `main`-Pane mit SMA20/SMA50/Bollinger — ohne Crosshair stehen die
  fünf Felder (`145.52`, `142.78`, `138.20`, `135.98` + `13 Mio.`, `129.18`) auf Höhe ihrer Kurve im
  Gutter; mit Crosshair wechseln sie auf den markierten Bar; nach `set_window_active(False)` wieder
  auf die neuesten Werte.
- Ende-zu-Ende-Probe mit zwei echten Top-Level-Fenstern: Fokuswechsel A→B verwirft den Crosshair in A
  (Idle-Werte), B behält seinen eigenen; Rückwechsel zu A verwirft den Crosshair in B.

**Deployment**

- Client-Code liegt im `/api/sync`-Bundle (`/api/sync_version` =
  `6808134a14cacd624ab4c0921dffea20155a8e5a350d830b581c18546ee6a7dc`).
- Kein Server-Neustart nötig (nur Client-UI-Dateien); der Windows-Viewer muss wie gewohnt einmal
  über `launch_windows_v2.bat` neu gestartet werden.
