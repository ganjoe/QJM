"""ChartPane: A single vertical pane with its own Y-axis, rendering overlays or candlesticks."""

from __future__ import annotations
import math
import bisect
from dataclasses import dataclass
from typing import List, Dict, NamedTuple, Optional
from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtWidgets import QWidget
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QFont,
    QFontMetrics,
    QPolygonF,
    QPixmap,
    QMouseEvent,
    QWheelEvent,
    QResizeEvent,
)

from chart_viewer.config import ViewerConfig
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.coords.y_axis import YAxisTransform
from chart_viewer.formatting import format_value, format_volume
from chart_viewer.models.entities import Bar, Overlay, OverlayPoint, Annotation
from chart_viewer.models.color import resolve_bar_color

Y_AXIS_WIDTH = 70.0
X_AXIS_HEIGHT = 22.0

# Log/Lin-Taste im Y-Achsen-Gutter (unterste Taste, direkt ueber dem AUTO/MANUAL-Pill).
Y_SCALE_BUTTON_WIDTH = 50.0
Y_SCALE_BUTTON_HEIGHT = 16.0
Y_SCALE_BUTTON_BOTTOM_PX = 40.0  # Abstand der Taste zur Unterkante des Panes

# Crosshair value boxes ("Datenfelder" in the Y-axis gutter). The number is
# coupled to the X-axis label font size (factor 1.5 by default) so it reads
# slightly larger than the axis; VALUE_BOX_SCALE shrinks font AND chrome of the
# whole readout as one block (0.7 = ~30 % smaller than the original layout).
X_AXIS_FONT_PT: float = 9.0
VALUE_BOX_SCALE: float = 0.7
VALUE_BOX_BG = "#1E222D"
VALUE_BOX_TEXT = "#FFFFFF"
VALUE_BOX_SECONDARY = "#9CA3AF"
VALUE_BOX_PRICE_COLOR = "#D1D4DC"

# Painting order of the overlay primitives per pane (contract section 3).
OVERLAY_DRAW_ORDER = ("zone", "band", "histogram", "level", "line", "marker")
# Decoration that must never reach the Y autoscale or the crosshair value boxes.
DECOR_OVERLAY_TYPES = frozenset({"zone", "level"})
# Overlays without a curve -> no crosshair value box.
NON_CURVE_OVERLAY_TYPES = frozenset({"marker", "zone", "level"})


class IndexedOverlayPoint(NamedTuple):
    """One overlay point resolved to bar indices (built once in set_data)."""

    idx: float                     # bar index of t
    value: Optional[float]
    value2: Optional[float]
    t2_idx: Optional[float] = None  # bar index of t2 (zones), None otherwise
    color_override: Optional[str] = None


@dataclass(frozen=True)
class CrosshairValue:
    """One curve's value at the bar marked by the vertical crosshair line."""

    key: str                              # "price" | overlay_id | f"{overlay_id}:upper"/":lower"
    color: str                            # curve colour -> border and left stripe
    value: float                          # y position (price box: the close)
    text: str                             # primary line (large, white)
    secondary_text: Optional[str] = None  # optional second line (volume, dimmed)


def resolve_line_width(style: dict, default_width: int = 1) -> int:
    """Effective pen width (in pixels) for an indicator line.

    An explicit ``style["width"]`` IS the pixel count and is used verbatim — a
    width of 1 stays 1 and is never rounded up. Without an explicit width the
    configured default applies. The result is only floored at 1, because Qt
    treats a pen width of 0 as a cosmetic hairline, which is a different concept.
    """
    raw = (style or {}).get("width")
    if raw is None:
        return max(1, int(default_width))
    return max(1, int(raw))


class ChartPane(QWidget):
    """A single chart pane with its own Y-axis and rendering layer.

    The main pane renders candlesticks + overlays.
    Indicator panes render only overlays (lines, histograms, bands).
    All panes share the same XAxisTransform for synchronised zoom/pan.
    """

    # Signals
    crosshair_y_moved = Signal(float)  # price at cursor y
    y_scale_changed = Signal()         # user manually scaled this pane's Y
    crosshair_moved_signal = Signal(str, float, float)  # (pane_id, x_px, y_px)
    x_pan_requested = Signal(float)    # (delta_px) right-click X-pan request
    y_scale_type_changed = Signal(str, str)  # (pane_id, "linear"|"log") by user click

    def __init__(
        self,
        pane_id: str,
        x_trans: XAxisTransform,
        config: ViewerConfig,
        is_main: bool = False,
        role: str = "",
        title: str = "",
        weight: int = 0,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.pane_id = pane_id
        self.x_trans = x_trans  # Shared reference — same object across all panes
        self.y_trans = YAxisTransform(config=config)
        self.config = config
        self.is_main = is_main
        # Pane identity from the chart definition: role "price"/"value"/"volume",
        # a small title in the top-left corner and the splitter weight.
        self.role = role or ("price" if is_main else "value")
        self.title = title or ""
        self.weight = int(weight or 0)
        self.draw_x_axis = False  # Only the pane directly under chart draws the X-axis

        # Data
        self.bars: List[Bar] = []
        self.overlays: Dict[str, Overlay] = {}
        self.annotations: Dict[str, Annotation] = {}
        self.series_style: dict = {}
        self.y_axis_mode: str = "auto"       # "auto" | "manual" (Fit-Verhalten)
        self.y_scale_type: str = "linear"    # "linear" | "log" (Y-Achsen-Skalierung)
        self.watermark_text: str = ""
        self.base_timestamp: int = 0
        self.bar_duration: int = 86400

        # Cache
        self._pixmap: Optional[QPixmap] = None
        self._dirty: bool = True

        # Interaction
        self._drag_start_y: float = 0.0
        self._is_scaling_y: bool = False
        self._is_hovering_y_handle: bool = False
        self._is_dragging_y_handle: bool = False
        self._is_hovering_scale_button: bool = False

        # Crosshair
        self._crosshair_x: Optional[float] = None   # Shared X pixel (vertical line)
        self._crosshair_y: Optional[float] = None   # Local Y pixel (horizontal line, only in active pane)
        self._is_crosshair_active: bool = False      # True if mouse is in THIS pane
        self._crosshair_bar_index: Optional[int] = None  # Snapped candle index (shared, badge source of truth)
        self._value_cache: Optional[tuple] = None    # (bar_idx, [CrosshairValue]) for the live layer

        # Measure tool
        self._is_measuring: bool = False
        self._measure_start_pos: Optional[QPointF] = None
        self._measure_start_price: float = 0.0
        self._measure_start_bar: float = 0.0

        # Pan tool (Right-click or Middle-click drag)
        self._is_panning_x: bool = False
        self._pan_start_x: float = 0.0

        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)

    def set_data(
        self,
        bars: List[Bar],
        overlays: Dict[str, Overlay],
        series_style: dict | None = None,
    ) -> None:
        self.bars = bars
        self.overlays = overlays
        self.series_style = series_style or {}
        self._bar_timestamps = [b.t_open for b in bars] if bars else []
        self._ts_map = {b.t_open: i for i, b in enumerate(bars)} if bars else {}

        # Pre-index all overlay points to bar index once for sub-millisecond panning and rendering
        self._indexed_overlays = {}
        if bars:
            for ov_id, ov in overlays.items():
                indexed_pts = []
                for pt in (ov.values or []):
                    t = pt.t if hasattr(pt, "t") else pt.get("t")
                    val = pt.value if hasattr(pt, "value") else pt.get("value")
                    val2 = getattr(pt, "value2", None) if hasattr(pt, "value2") else pt.get("value2")
                    t2 = getattr(pt, "t2", None) if hasattr(pt, "t2") else pt.get("t2")
                    color_override = (
                        getattr(pt, "color_override", None)
                        if hasattr(pt, "color_override")
                        else pt.get("color_override")
                    )
                    t_int = int(t) if t is not None else None
                    if t_int is not None and t_int in self._ts_map:
                        bar_i = float(self._ts_map[t_int])
                    elif t_int is not None:
                        bar_i = self._timestamp_to_bar_idx(t_int)
                    else:
                        continue
                    fval = float(val) if val is not None else None
                    fval2 = float(val2) if val2 is not None else None
                    # Zone end (t2) resolved to a bar index, None for non-zones.
                    bar_i2 = None
                    if t2 is not None:
                        t2_int = int(t2)
                        if t2_int in self._ts_map:
                            bar_i2 = float(self._ts_map[t2_int])
                        else:
                            bar_i2 = self._timestamp_to_bar_idx(t2_int)
                    indexed_pts.append(IndexedOverlayPoint(
                        idx=bar_i,
                        value=fval,
                        value2=fval2,
                        t2_idx=bar_i2,
                        color_override=color_override,
                    ))

                indexed_pts.sort(key=lambda x: x[0])
                indices = [p[0] for p in indexed_pts]
                self._indexed_overlays[ov_id] = {
                    "overlay": ov,
                    "indices": indices,
                    "pts": indexed_pts,
                }
        self._value_cache = None
        self.mark_dirty()

    def set_annotations(self, annotations) -> None:
        """Replace this pane's annotation set and repaint.

        Accepts the StateManager's ``{id: Annotation}`` mapping or a plain list.
        """
        if annotations is None:
            self.annotations = {}
        elif isinstance(annotations, dict):
            self.annotations = dict(annotations)
        else:
            self.annotations = {a.id: a for a in annotations}
        self.mark_dirty()
        # Annotations arrive out-of-band from the agent while the window is idle.
        # A merely scheduled update() can sit in the event queue until the next
        # natural repaint (e.g. the user moving the mouse), so the drawing would
        # appear only after an unrelated interaction. Paint synchronously instead
        # — annotation changes are low-frequency, so the cost is irrelevant.
        self.repaint()

    def mark_dirty(self) -> None:
        self._dirty = True
        # Value boxes are computed live; invalidate the cache so in-place bar
        # updates (live ticks mutate bars[-1]) reach the next paint.
        self._value_cache = None
        self.update()

    def update_y_range(self) -> None:
        """Auto-fit Y-range to visible data, respecting custom headroom ratio in manual mode."""
        if not self.bars and not self.overlays:
            return

        min_idx = max(0, int(math.floor(self.x_trans.x_to_bar(0.0) - 1)))
        chart_w = max(10.0, self.width() - Y_AXIS_WIDTH)
        max_idx_val = min(len(self.bars) - 1, int(math.ceil(self.x_trans.x_to_bar(chart_w) + 1))) if self.bars else int(self.x_trans.right_index)

        p_min = float("inf")
        p_max = float("-inf")

        # Price range from candlesticks (main pane only)
        if self.is_main and self.bars:
            bar_max_idx = min(len(self.bars) - 1, max_idx_val)
            if min_idx <= bar_max_idx:
                visible_bars = self.bars[min_idx : bar_max_idx + 1]
                p_min = min(p_min, min(b.low for b in visible_bars))
                p_max = max(p_max, max(b.high for b in visible_bars))

        # Value range from pre-indexed overlays
        # Rule: In the main pane, Price dictates the scale. Overlays (like distant SMAs) should not squash the chart.
        if not self.is_main:
            indexed_ovs = getattr(self, "_indexed_overlays", {})
            for item in indexed_ovs.values():
                # Zones and levels are decoration: they must never stretch the
                # Y autoscale (only lines/bands/histograms do).
                if item["overlay"].type in DECOR_OVERLAY_TYPES:
                    continue
                indices = item["indices"]
                if not indices:
                    continue
                i_start = bisect.bisect_left(indices, min_idx)
                i_end = bisect.bisect_right(indices, max_idx_val)
                for _, val, val2, _t2, _color in item["pts"][i_start:i_end]:
                    if val is not None:
                        if val < p_min: p_min = val
                        if val > p_max: p_max = val
                    if val2 is not None:
                        if val2 < p_min: p_min = val2
                        if val2 > p_max: p_max = val2

        if p_min == float("inf") or p_max == float("-inf"):
            return

        # Die angeforderte Skalierung (Log/Lin-Taste) wird bei jedem Fit erneut
        # angewandt; fit_range zieht bei nicht-positiven Werten selbst auf
        # linear zurueck (is_mode_forced).
        self.y_trans.fit_range(p_min, p_max, requested_mode=self.y_scale_type)

    # --- Y-Skalierung: linear / logarithmisch (Taste pro Pane) -------------

    def _content_height(self) -> float:
        """Height of the pane area without the X-axis strip (same rule as paintEvent)."""
        h = float(self.height())
        return max(10.0, h - X_AXIS_HEIGHT) if self.draw_x_axis else h

    def y_scale_button_rect(self, chart_h: Optional[float] = None) -> QRectF:
        """Rect of the LOG/LIN button in this pane's Y-axis gutter.

        Shared by the renderer and the hit test so a click always lands on
        exactly what the user sees.
        """
        content_h = self._content_height() if chart_h is None else float(chart_h)
        chart_w = max(10.0, float(self.width()) - Y_AXIS_WIDTH)
        gx = chart_w + Y_AXIS_WIDTH / 2.0
        top = content_h - Y_SCALE_BUTTON_BOTTOM_PX
        return QRectF(
            gx - Y_SCALE_BUTTON_WIDTH / 2.0,
            top,
            Y_SCALE_BUTTON_WIDTH,
            Y_SCALE_BUTTON_HEIGHT,
        )

    def is_log_effective(self) -> bool:
        """True when the pane is actually drawn logarithmically right now."""
        return self.y_trans.mode == "log"

    def is_log_forced(self) -> bool:
        """True when log is requested but the visible values forbid it (<= 0)."""
        return self.y_scale_type == "log" and not self.is_log_effective()

    def set_y_scale_type(self, scale_type: str, refit: bool = True, notify: bool = True) -> bool:
        """Set the requested Y scale ("linear" | "log"). Returns True on change.

        notify=False is used when the mode comes from a preset/snapshot: the
        change must be rendered but not echoed back to the agent as a user edit.
        """
        normalized = "log" if str(scale_type or "").strip().lower() == "log" else "linear"
        changed = normalized != self.y_scale_type
        self.y_scale_type = normalized
        if refit:
            self.update_y_range()
        if changed:
            self.mark_dirty()
            if notify:
                self.y_scale_type_changed.emit(self.pane_id, normalized)
        return changed

    def toggle_y_scale_type(self) -> None:
        """LOG/LIN button clicked: switch the scale and report it to the canvas."""
        self.set_y_scale_type("linear" if self.y_scale_type == "log" else "log")
        if self._is_hovering_scale_button:
            self.setToolTip(self._y_scale_tooltip())

    def _y_scale_tooltip(self) -> str:
        """Hover text of the LOG/LIN button (explains a forced fallback too)."""
        if self.y_scale_type == "log":
            if self.is_log_forced():
                return "Log-Y nicht m\u00f6glich (Werte <= 0) - Klick schaltet auf linear"
            return "Y-Achse logarithmisch - Klick schaltet auf linear"
        return "Y-Achse linear - Klick schaltet auf logarithmisch"

    def _timestamp_to_bar_idx(self, t: int) -> float:
        """Convert timestamp to fractional bar index using cached bar timestamps."""
        if not self.bars:
            return 0.0
        ts_list = getattr(self, "_bar_timestamps", None)
        if ts_list is None:
            ts_list = [b.t_open for b in self.bars]
            self._bar_timestamps = ts_list
        pos = bisect.bisect_left(ts_list, t)
        if pos < len(ts_list) and ts_list[pos] == t:
            return float(pos)
        if pos <= 0:
            return 0.0
        if pos >= len(ts_list):
            return float(len(ts_list) - 1)
        t_prev, t_next = ts_list[pos - 1], ts_list[pos]
        if t_next > t_prev:
            return float(pos - 1) + (t - t_prev) / (t_next - t_prev)
        return float(pos)

    # ── Rendering ──────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return

        chart_w = max(10.0, w - Y_AXIS_WIDTH)
        chart_h = float(h)
        content_h = max(10.0, chart_h - X_AXIS_HEIGHT) if self.draw_x_axis else chart_h
        if abs(self.y_trans.viewport_height_px - content_h) > 0.5:
            self.y_trans.viewport_height_px = content_h
            self.update_y_range()
        if abs(self.x_trans.viewport_width_px - chart_w) > 0.5:
            self.x_trans.set_viewport_width(chart_w)

        if self._dirty or self._pixmap is None or self._pixmap.size() != self.size():
            if self._pixmap is None or self._pixmap.size() != self.size():
                self._pixmap = QPixmap(self.size())
            pp = QPainter(self._pixmap)
            try:
                pp.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                self._render_background(pp, chart_w, content_h, w, h)

                if self.is_main:
                    self._render_candlesticks(pp, chart_w, content_h)

                self._render_overlays(pp, chart_w, content_h)
                self._render_annotations(pp, chart_w, content_h)
                self._render_y_axis(pp, chart_w, content_h)

                if self.draw_x_axis:
                    self._render_x_axis(pp, chart_w, chart_h)
            finally:
                pp.end()
            self._dirty = False

        screen_painter = QPainter(self)
        try:
            screen_painter.drawPixmap(0, 0, self._pixmap)
            chart_w_live = max(10.0, w - Y_AXIS_WIDTH)
            chart_h_live = float(h)
            content_h_live = max(10.0, chart_h_live - X_AXIS_HEIGHT) if self.draw_x_axis else chart_h_live

            # TC2000 Projection guideline when hovering or dragging handle
            if getattr(self, "_is_hovering_y_handle", False) or getattr(self, "_is_dragging_y_handle", False):
                y_handle = self.y_trans.price_to_y(self.y_trans.p_max)
                y_handle = max(8.0, min(content_h_live - 15.0, y_handle))
                guide_pen = QPen(QColor("#00E676" if getattr(self, "_is_dragging_y_handle", False) else "#758696"))
                guide_pen.setStyle(Qt.PenStyle.DashLine)
                guide_pen.setWidth(1)
                screen_painter.setPen(guide_pen)
                screen_painter.drawLine(0, int(y_handle), int(chart_w_live), int(y_handle))

            # Crosshair is drawn live (not cached) for sub-ms response
            self._render_crosshair(screen_painter, chart_w_live, content_h_live, chart_h_live)
            # Measure tool drawn live
            if self._is_measuring and self._measure_start_pos and self._crosshair_y is not None:
                self._render_measure_tool(screen_painter, chart_w_live, content_h_live)
        finally:
            screen_painter.end()

    def _render_background(self, painter: QPainter, chart_w: float, chart_h: float, w: int, h: int) -> None:
        bg_color = QColor(self.config.default_background_color)
        painter.fillRect(QRectF(0, 0, chart_w, chart_h), bg_color)

        # Y-axis gutter
        gutter_color = QColor("#161922")
        painter.fillRect(QRectF(chart_w, 0, Y_AXIS_WIDTH, chart_h), gutter_color)

        # Axis border
        axis_pen = QPen(QColor("#2A2E39"))
        axis_pen.setWidth(1)
        painter.setPen(axis_pen)
        painter.drawLine(int(chart_w), 0, int(chart_w), h)

        # Bottom border
        painter.drawLine(0, int(chart_h) - 1, w, int(chart_h) - 1)

        # Watermark (main pane only)
        if self.is_main and self.watermark_text:
            painter.save()
            wm_color = QColor(self.config.default_text_color)
            wm_color.setAlpha(18)
            painter.setPen(wm_color)
            font = QFont(painter.font())
            font.setPointSize(44)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(0, 0, chart_w, chart_h), Qt.AlignmentFlag.AlignCenter, self.watermark_text)
            painter.restore()

        # Horizontal grid lines
        num_ticks = max(2, int(chart_h / 80))
        grid_pen = QPen(QColor(self.config.default_grid_color))
        grid_pen.setStyle(Qt.PenStyle.DotLine)
        grid_pen.setWidth(1)
        for i in range(1, num_ticks):
            y = (chart_h / num_ticks) * i
            painter.setPen(grid_pen)
            painter.drawLine(0, int(y), int(chart_w), int(y))

        # Pane title (chart definition): small, muted, top-left. The big
        # watermark stays reserved for the price pane.
        if self.title:
            painter.save()
            title_color = QColor(self.config.default_text_color)
            title_color.setAlpha(120)
            painter.setPen(title_color)
            title_font = QFont(painter.font())
            title_font.setPointSize(8)
            painter.setFont(title_font)
            painter.drawText(
                QRectF(6, 2, max(10.0, chart_w - 12), 14),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self.title,
            )
            painter.restore()

    def _render_y_axis(self, painter: QPainter, chart_w: float, chart_h: float) -> None:
        """Render Y-axis labels, ticks, TC2000 mini-arrow handle, and scale handle pill for this pane."""
        num_ticks = max(2, int(chart_h / 80))
        scale_font = QFont(painter.font())
        scale_font.setPointSizeF(X_AXIS_FONT_PT)
        painter.setFont(scale_font)

        tick_pen = QPen(QColor("#434958"))
        tick_pen.setWidth(1)

        for i in range(1, num_ticks):
            y = (chart_h / num_ticks) * i
            price = self.y_trans.y_to_price(y)

            # Tick mark
            painter.setPen(tick_pen)
            painter.drawLine(int(chart_w), int(y), int(chart_w + 5), int(y))

            # Price label
            painter.setPen(QColor("#9CA3AF"))
            if abs(price) < 100:
                price_text = f"{price:.2f}"
            else:
                price_text = f"{price:.0f}"
            painter.drawText(
                QRectF(chart_w + 7, y - 8, Y_AXIS_WIDTH - 9, 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                price_text,
            )

        # TC2000 Mini-Arrow Handle (pointing left from Y-axis gutter at p_max boundary)
        if self.is_main:
            y_handle = self.y_trans.price_to_y(self.y_trans.p_max)
            y_handle = max(8.0, min(chart_h - 25.0, y_handle))
            arrow_poly = QPolygonF([
                QPointF(chart_w, y_handle),
                QPointF(chart_w + 8.0, y_handle - 6.0),
                QPointF(chart_w + 8.0, y_handle + 6.0),
            ])
            is_active_handle = getattr(self, "_is_hovering_y_handle", False) or getattr(self, "_is_dragging_y_handle", False)
            painter.setBrush(QBrush(QColor("#00E676" if is_active_handle else "#FFFFFF")))
            painter.setPen(QPen(QColor("#161922"), 1))
            painter.drawPolygon(arrow_poly)

        # Scale handle pill
        gx = int(chart_w + Y_AXIS_WIDTH / 2.0)
        handle_font = QFont(painter.font())
        handle_font.setPointSize(7)
        handle_font.setBold(True)
        painter.setFont(handle_font)

        # Log/Lin-Taste: zeigt die angeforderte Y-Skalierung dieses Panes.
        self._draw_y_scale_button(painter, chart_h)

        pill = QRectF(gx - 25, chart_h - 20, 50, 16)
        if self.y_axis_mode == "auto":
            painter.fillRect(pill, QColor("#14241F"))
            painter.setPen(QPen(QColor("#1E5642"), 1))
            painter.drawRoundedRect(pill, 3, 3)
            painter.setPen(QColor("#00E676"))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, "● AUTO")
        else:
            painter.fillRect(pill, QColor("#2A1E14"))
            painter.setPen(QPen(QColor("#663B19"), 1))
            painter.drawRoundedRect(pill, 3, 3)
            painter.setPen(QColor("#FF9100"))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, "● MANUAL")

    def _draw_y_scale_button(self, painter: QPainter, chart_h: float) -> None:
        """Draw the LOG/LIN toggle of this pane in the Y-axis gutter.

        The requested scale is shown, not the effective one: when log is
        impossible for the visible values (<= 0) the button stays on LOG but
        turns amber, so the click is never silently forgotten.
        """
        rect = self.y_scale_button_rect(chart_h)
        requested_log = self.y_scale_type == "log"
        forced = self.is_log_forced()

        if forced:
            bg, border, text_color, label = "#2A1E14", "#663B19", "#FF9100", "LOG"
        elif requested_log:
            bg, border, text_color, label = "#141C2E", "#2962FF", "#7EA6FF", "LOG"
        else:
            bg, border, text_color, label = "#1E222D", "#434958", "#9CA3AF", "LIN"
        if self._is_hovering_scale_button:
            border = "#787B86"

        painter.fillRect(rect, QColor(bg))
        painter.setPen(QPen(QColor(border), 1))
        painter.drawRoundedRect(rect, 3, 3)
        painter.setPen(QColor(text_color))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    def _render_x_axis(self, painter: QPainter, chart_w: float, chart_h: float) -> None:
        """Render X-axis time labels and tick marks at bottom of pane."""
        from datetime import datetime, timezone

        axis_y = chart_h - X_AXIS_HEIGHT
        axis_h = X_AXIS_HEIGHT

        # Background
        painter.fillRect(QRectF(0, axis_y, chart_w, axis_h), QColor("#161922"))
        painter.fillRect(QRectF(chart_w, axis_y, Y_AXIS_WIDTH, axis_h), QColor("#12141B"))

        # Top border
        border_pen = QPen(QColor("#2A2E39"))
        border_pen.setWidth(1)
        painter.setPen(border_pen)
        painter.drawLine(0, int(axis_y), int(chart_w + Y_AXIS_WIDTH), int(axis_y))
        painter.drawLine(int(chart_w), int(axis_y), int(chart_w), int(chart_h))

        # Time labels
        visible_bars = self.x_trans.visible_bars
        step_bars = max(8, int(visible_bars / 7))
        min_idx = math.floor(self.x_trans.min_visible_bar_index)
        max_idx = math.ceil(self.x_trans.right_index)

        tick_pen = QPen(QColor("#434958"))
        tick_pen.setWidth(1)

        scale_font = QFont(painter.font())
        scale_font.setPointSizeF(X_AXIS_FONT_PT)
        painter.setFont(scale_font)

        first_step = (min_idx // step_bars) * step_bars
        for idx in range(first_step, max_idx + 1, step_bars):
            x = self.x_trans.bar_to_x(idx)
            if 0 <= x <= chart_w:
                painter.setPen(tick_pen)
                painter.drawLine(int(x), int(axis_y), int(x), int(axis_y + 3))

                if self.base_timestamp > 0:
                    if self.bars and 0 <= idx < len(self.bars):
                        t_sec = self.bars[idx].t_open
                    else:
                        t_sec = self.base_timestamp + int(idx * self.bar_duration)
                    try:
                        dt = datetime.fromtimestamp(t_sec, tz=timezone.utc)
                        date_str = dt.strftime("%d. %b")
                    except Exception:
                        date_str = f"{idx}"
                    painter.setPen(QColor("#848E9C"))
                    painter.drawText(
                        QRectF(x - 35, axis_y + 3, 70, 16),
                        Qt.AlignmentFlag.AlignCenter,
                        date_str,
                    )

    def set_crosshair(self, x: Optional[float], y: Optional[float], is_active: bool, bar_index: Optional[int] = None) -> None:
        """Set crosshair position. x = shared X pixel, y = local Y pixel (only if active).

        bar_index is the snapped candle index shared by every pane, so the date badge
        drawn by the X-axis pane matches the vertical line and the emitted timestamp.
        """
        self._crosshair_x = x
        self._crosshair_y = y if is_active else None
        self._is_crosshair_active = is_active
        self._crosshair_bar_index = bar_index
        self.update()  # Trigger repaint (only live layer, pixmap stays cached)

    def _render_crosshair(self, painter: QPainter, chart_w: float, content_h: float, chart_h: float) -> None:
        """Draw crosshair lines live (not cached) plus the per-curve value boxes.

        The value boxes are NOT tied to the crosshair: while no bar is marked
        (mouse outside the pane, chart window not focused, e.g. while working in
        the control panel) they keep showing the NEWEST value of every curve, so
        the readout never blanks out.
        """
        cx: Optional[int] = None
        bar_idx: Optional[int] = None
        if self._crosshair_x is not None:
            cx = int(self._crosshair_x)
            bar_idx = self._resolve_crosshair_bar_index(float(self._crosshair_x))

            ch_pen = QPen(QColor(self.config.default_crosshair_color))
            ch_pen.setStyle(Qt.PenStyle.DashLine)
            ch_pen.setWidth(1)
            painter.setPen(ch_pen)

            # Vertical line — always drawn across content area
            painter.drawLine(cx, 0, cx, int(content_h))

        # Value boxes: one per curve of THIS pane, read at the bar the line marks
        # (bar_idx) — or at the end of the series (bar_idx None). Drawn before the
        # cursor badge so the cursor readout always stays on top.
        if getattr(self.config, "crosshair_value_box_enabled", True):
            values = self._value_cache_for(bar_idx)
            if values:
                box_font = self._value_box_font(painter.font())
                painter.setFont(box_font)
                for value, box_rect in self._layout_value_boxes(values, content_h, box_font):
                    self._draw_value_box(painter, value, box_rect)

        if cx is None:
            return

        # Horizontal line + price badge (cursor position) — only in the active pane
        if self._is_crosshair_active and self._crosshair_y is not None:
            cy = int(self._crosshair_y)
            if cy <= content_h:
                painter.drawLine(0, cy, int(chart_w), cy)

                # Price badge on Y-axis
                price = self.y_trans.y_to_price(cy)
                if abs(price) < 100:
                    price_str = f"{price:.2f}"
                else:
                    price_str = f"{price:.0f}"
                badge_w = 60
                badge_h = 20
                badge_rect = QRectF(chart_w, cy - badge_h / 2.0, badge_w, badge_h)
                painter.fillRect(badge_rect, QColor("#363A45"))
                painter.setPen(QColor("#FFFFFF"))
                font = painter.font()
                font.setPointSize(9)
                painter.setFont(font)
                painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, price_str)

        # Crosshair time badge if this pane draws X-axis (same bar index as the boxes)
        if self.draw_x_axis and self.base_timestamp > 0 and bar_idx is not None:
            if self.bars and 0 <= bar_idx < len(self.bars):
                t_sec = self.bars[bar_idx].t_open
            else:
                t_sec = self.base_timestamp + int(bar_idx * self.bar_duration)
            time_str = self._format_crosshair_time(t_sec, bar_idx)
            badge_w = 118
            badge_h = 18
            axis_y = chart_h - X_AXIS_HEIGHT
            badge_rect = QRectF(cx - badge_w / 2.0, axis_y + 1, badge_w, badge_h)
            painter.fillRect(badge_rect, QColor("#363A45"))
            painter.setPen(QColor("#FFFFFF"))
            badge_font = QFont(painter.font())
            badge_font.setPointSize(8)
            painter.setFont(badge_font)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, time_str)

    def _format_crosshair_time(self, t_sec: int, bar_idx: int) -> str:
        """Format a bar timestamp for the crosshair badge (date, plus time intraday)."""
        from datetime import datetime, timezone
        try:
            dt = datetime.fromtimestamp(t_sec, tz=timezone.utc)
        except Exception:
            return f"Bar {bar_idx}"
        if self.bar_duration >= 86400:
            return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%d %H:%M")

    # ── Crosshair value boxes (per-curve Y-axis readout) ─────────────────

    def _resolve_crosshair_bar_index(self, x_px: float) -> Optional[int]:
        """Authoritative bar index for the crosshair (date badge + value boxes).

        Prefers the snapped index distributed by the canvas and falls back to
        nearest-neighbour (half-up, never floor) so the badge flips in the gap
        between two candles, not at the candle center.
        """
        idx = self._crosshair_bar_index
        if idx is None:
            if not self.bars:
                return None
            idx = math.floor(self.x_trans.x_to_bar(x_px) + 0.5)
        if self.bars:
            return max(0, min(len(self.bars) - 1, int(idx)))
        return int(idx)

    def crosshair_values(self, bar_idx: int) -> List[CrosshairValue]:
        """Value of EVERY curve of this pane at the given bar.

        The value depends on the bar index (X) only — never on the cursor's Y
        position — so hovering anywhere in the pane reads the same numbers.
        """
        out: List[CrosshairValue] = []

        # Main pane: the price curve = Close + Volume of the marked bar
        if self.is_main and self.bars and 0 <= bar_idx < len(self.bars):
            out.append(self._price_value(self.bars[bar_idx]))

        # One box per overlay curve that belongs to this pane
        for ov_id, item in getattr(self, "_indexed_overlays", {}).items():
            if item["overlay"].type in NON_CURVE_OVERLAY_TYPES:
                continue  # markers/zones/levels carry no curve -> no value box
            val, val2 = self._overlay_value_at(item, bar_idx)
            out.extend(self._overlay_boxes(ov_id, item, val, val2))
        return out

    def latest_values(self) -> List[CrosshairValue]:
        """Value of every curve at the END of its series (the newest point).

        This is the idle readout shown while no bar is marked by a crosshair
        (mouse outside the pane, chart window not focused). Per overlay the last
        AVAILABLE point counts, so a curve lagging the price by a bar or two
        still reports its newest number instead of blanking out entirely.
        """
        out: List[CrosshairValue] = []
        if self.is_main and self.bars:
            out.append(self._price_value(self.bars[-1]))

        for ov_id, item in getattr(self, "_indexed_overlays", {}).items():
            if item["overlay"].type in NON_CURVE_OVERLAY_TYPES:
                continue
            pts = item.get("pts") or []
            if not pts:
                continue
            point = pts[-1]  # newest available point of this curve
            out.extend(self._overlay_boxes(ov_id, item, point.value, point.value2))
        return out

    def _price_value(self, bar: Bar) -> CrosshairValue:
        """The main pane's price box: Close, with the bar's volume as second line."""
        return CrosshairValue(
            key="price",
            color=VALUE_BOX_PRICE_COLOR,
            value=float(bar.close),
            text=format_value(bar.close),
            secondary_text=format_volume(bar.volume),
        )

    def _overlay_boxes(self, ov_id: str, item: dict, val, val2) -> List[CrosshairValue]:
        """Box(es) of one overlay curve: a band yields upper+lower, others one box.

        Zones and levels are decoration and never appear in the readout.
        """
        ov = item["overlay"]
        if ov.type in NON_CURVE_OVERLAY_TYPES:
            return []
        color = (ov.style or {}).get("color", "#26A69A")
        out: List[CrosshairValue] = []
        if ov.type == "band":
            if val is not None:
                out.append(CrosshairValue(f"{ov_id}:upper", color, float(val), format_value(val)))
            if val2 is not None:
                out.append(CrosshairValue(f"{ov_id}:lower", color, float(val2), format_value(val2)))
        elif val is not None:
            out.append(CrosshairValue(ov_id, color, float(val), format_value(val)))
        return out

    def _overlay_value_at(self, item: dict, bar_idx: int) -> tuple:
        """Overlay value(s) at (or near) bar_idx; (None, None) when no point lies there.

        An exact index match wins. A nearest neighbour within
        ``crosshair_value_box_snap_bars`` (default 1 bar) is accepted so minimal
        timestamp offsets cannot blank the box; indicator warm-up bars (no point
        yet) correctly produce no value at all.
        """
        indices = item["indices"]
        if not indices:
            return None, None
        target = float(bar_idx)
        pos = bisect.bisect_left(indices, target)
        candidates = [i for i in (pos, pos - 1) if 0 <= i < len(indices)]
        if not candidates:
            return None, None
        best = min(candidates, key=lambda i: abs(indices[i] - target))
        tolerance = float(getattr(self.config, "crosshair_value_box_snap_bars", 1.0))
        if abs(indices[best] - target) > tolerance:
            return None, None
        point = item["pts"][best]
        return point.value, point.value2

    def _value_box_font(self, base_font: QFont) -> QFont:
        """Value box font: X-axis font size x config factor, x VALUE_BOX_SCALE."""
        font = QFont(base_font)
        font.setPointSizeF(
            X_AXIS_FONT_PT
            * float(getattr(self.config, "crosshair_value_box_font_factor", 1.5))
            * VALUE_BOX_SCALE
        )
        return font

    def _value_cache_for(self, bar_idx: Optional[int]) -> List[CrosshairValue]:
        """Cache the readout per key (bar index; None = newest values).

        Vertical mouse moves reuse the entry; ``bar_idx=None`` is the idle
        readout at the end of the series.
        """
        if self._value_cache is None or self._value_cache[0] != bar_idx:
            values = self.latest_values() if bar_idx is None else self.crosshair_values(bar_idx)
            self._value_cache = (bar_idx, values)
        return self._value_cache[1]

    def _layout_value_boxes(self, values: List[CrosshairValue], content_h: float, font: QFont) -> list:
        """Place one box per curve at its value's Y position, collision-free.

        Returns [(CrosshairValue, QRectF)]. Boxes are right-aligned to the pane
        edge (they grow leftwards over the chart when a value string is wider
        than the Y-axis gutter) and never leave the content area; in the extreme
        case (many curves in a very small pane) they may overlap instead.
        """
        metrics = QFontMetrics(font)
        line_h = float(metrics.height())
        # Padding, stripe and width limits belong to the scaled block as well,
        # so shrinking VALUE_BOX_SCALE shrinks the whole field, not just the text.
        pad_v = 3.0 * VALUE_BOX_SCALE
        pad_h = 6.0 * VALUE_BOX_SCALE
        stripe_w = 3.0 * VALUE_BOX_SCALE
        gap = 2.0 * VALUE_BOX_SCALE
        max_w = float(getattr(self.config, "crosshair_value_box_max_width_px", 150.0)) * VALUE_BOX_SCALE
        min_w = float(getattr(self.config, "crosshair_value_box_min_width_px", 62.0)) * VALUE_BOX_SCALE
        x_right = float(self.width()) - 3.0

        items = []
        for v in values:
            text_w = float(metrics.horizontalAdvance(v.text))
            if v.secondary_text:
                text_w = max(text_w, float(metrics.horizontalAdvance(v.secondary_text)))
            n_lines = 2 if v.secondary_text else 1
            w = max(min_w, min(max_w, text_w + 2 * pad_h + stripe_w))
            h = n_lines * line_h + 2 * pad_v
            y = self.y_trans.price_to_y(v.value) - h / 2.0
            items.append([v, w, h, y])

        # Desired y = value position; push overlapping boxes downwards, then
        # correct a stack that overflows the bottom edge from below upwards.
        items.sort(key=lambda it: it[3])
        for _ in range(2):
            prev_bottom = -1e9
            for it in items:
                it[3] = max(it[3], prev_bottom + gap, 2.0)
                prev_bottom = it[3] + it[2]
            if prev_bottom > content_h - 2.0:
                next_top = content_h - 2.0
                for it in reversed(items):
                    it[3] = min(it[3], next_top - it[2])
                    next_top = it[3] - gap

        # Final clamp: a box must never leave the pane (overlap accepted above)
        for it in items:
            it[3] = max(2.0, min(it[3], max(2.0, content_h - 2.0 - it[2])))

        return [(it[0], QRectF(x_right - it[1], it[3], it[1], it[2])) for it in items]

    def _draw_value_box(self, painter: QPainter, v: CrosshairValue, rect: QRectF) -> None:
        """Draw one framed value box (border/stripe in the curve's colour)."""
        painter.fillRect(rect, QColor(VALUE_BOX_BG))
        painter.setPen(QPen(QColor(v.color), 1))
        painter.drawRect(rect)
        painter.fillRect(
            QRectF(rect.left() + 1, rect.top() + 1, max(1.0, 3.0 * VALUE_BOX_SCALE),
                   max(1.0, rect.height() - 2)),
            QColor(v.color),
        )

        text_rect = rect.adjusted(8.0 * VALUE_BOX_SCALE, 1, -4.0 * VALUE_BOX_SCALE, -1)
        if v.secondary_text:
            half = text_rect.height() / 2.0
            painter.setPen(QColor(VALUE_BOX_TEXT))
            painter.drawText(
                QRectF(text_rect.left(), text_rect.top(), text_rect.width(), half),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                v.text,
            )
            painter.setPen(QColor(VALUE_BOX_SECONDARY))
            painter.drawText(
                QRectF(text_rect.left(), text_rect.top() + half, text_rect.width(), half),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                v.secondary_text,
            )
        else:
            painter.setPen(QColor(VALUE_BOX_TEXT))
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                v.text,
            )


    def _render_measure_tool(self, painter: QPainter, chart_w: float, chart_h: float) -> None:
        """Render TC2000-style measure tool with info card."""
        p1 = self._measure_start_pos
        p2 = QPointF(self._crosshair_x or 0, self._crosshair_y or 0)
        if not p1:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Line connecting start and end
        line_pen = QPen(QColor("#2962FF"))
        line_pen.setWidth(2)
        painter.setPen(line_pen)
        painter.drawLine(p1, p2)

        # Endpoint handles
        painter.setPen(QPen(QColor("#2962FF"), 2))
        painter.setBrush(QBrush(QColor("#FFFFFF")))
        painter.drawEllipse(p1, 5.0, 5.0)
        painter.drawEllipse(p2, 5.0, 5.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#2962FF")))
        painter.drawEllipse(p1, 2.0, 2.0)
        painter.drawEllipse(p2, 2.0, 2.0)

        # Delta calculations
        current_price = self.y_trans.y_to_price(p2.y())
        delta_price = current_price - self._measure_start_price
        start_p = self._measure_start_price if self._measure_start_price != 0 else 1.0
        delta_pct = (delta_price / start_p) * 100.0

        current_bar = self.x_trans.x_to_bar(p2.x())
        delta_bars = int(abs(current_bar - self._measure_start_bar))
        days = delta_bars if self.bar_duration == 86400 else max(1, int(delta_bars * self.bar_duration / 86400))

        is_pos = delta_price >= 0
        accent_color = QColor("#00E676" if is_pos else "#FF5252")
        arrow = "▲" if is_pos else "▼"
        sign = "+" if is_pos else ""

        # Floating info card
        card_w = 185.0
        card_h = 72.0
        bx = p2.x() + 18.0
        if bx + card_w > chart_w - 10:
            bx = p2.x() - card_w - 18.0
        if bx < 10.0:
            bx = 10.0
        by = p2.y() - card_h - 12.0
        if by < 10.0:
            by = p2.y() + 16.0
        if by + card_h > chart_h - 10:
            by = chart_h - 10 - card_h

        card_rect = QRectF(bx, by, card_w, card_h)
        painter.fillRect(card_rect.translated(2, 2), QColor(0, 0, 0, 90))
        painter.setBrush(QBrush(QColor(18, 22, 30, 245)))
        painter.setPen(QPen(QColor(55, 65, 85), 1.5))
        painter.drawRoundedRect(card_rect, 6, 6)

        # Header: delta %
        header_font = QFont(painter.font())
        header_font.setPointSize(11)
        header_font.setBold(True)
        painter.setFont(header_font)
        painter.setPen(accent_color)
        header_text = f"{arrow} {sign}{delta_pct:.2f}% ({sign}{delta_price:.2f} $)"
        painter.drawText(QRectF(bx + 12, by + 8, card_w - 24, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, header_text)

        # Bars & Days
        sub_font = QFont(painter.font())
        sub_font.setPointSize(9)
        sub_font.setBold(False)
        painter.setFont(sub_font)
        painter.setPen(QColor("#D1D4DC"))
        painter.drawText(QRectF(bx + 12, by + 30, card_w - 24, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"{delta_bars} Bars \u2022 {days} Tage")

        # Price range
        painter.setPen(QColor("#848E9C"))
        painter.drawText(QRectF(bx + 12, by + 48, card_w - 24, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"{self._measure_start_price:.2f} $ \u2192 {current_price:.2f} $")

        painter.restore()

    def _render_candlesticks(self, painter: QPainter, chart_w: float, chart_h: float) -> None:
        """Render OHLC candlesticks (main pane only)."""
        if not self.bars:
            return

        candle_w = self.x_trans.candle_width_px
        is_thin_bar = candle_w < self.config.thin_bar_threshold_px
        configured_wick = self.config.configured_wick_px
        effective_wick_px = max(1, min(configured_wick, max(1, int(candle_w - 1))))

        min_idx = max(0, math.floor(self.x_trans.x_to_bar(0.0) - 1))
        max_idx = min(len(self.bars) - 1, math.ceil(self.x_trans.x_to_bar(chart_w) + 1))

        painter.save()
        painter.setClipRect(0, 0, int(chart_w), int(chart_h))

        for i in range(min_idx, max_idx + 1):
            bar = self.bars[i]
            x_center = self.x_trans.bar_to_x(i)
            if x_center + candle_w < 0 or x_center - candle_w > chart_w:
                continue

            color_info = resolve_bar_color(bar, self.series_style, self.config)
            border_qcolor = QColor(color_info.border_color)

            y_high = self.y_trans.price_to_y(bar.high)
            y_low = self.y_trans.price_to_y(bar.low)
            y_open = self.y_trans.price_to_y(bar.open)
            y_close = self.y_trans.price_to_y(bar.close)

            pen_style = Qt.PenStyle.SolidLine if bar.is_valid else Qt.PenStyle.DashLine

            if is_thin_bar:
                pen = QPen(border_qcolor)
                pen.setStyle(pen_style)
                pen.setWidth(1)
                painter.setPen(pen)
                painter.drawLine(int(x_center), int(y_high), int(x_center), int(y_low))
                tick_len = max(1.0, candle_w * 0.8)
                painter.drawLine(int(x_center - tick_len), int(y_open), int(x_center), int(y_open))
                painter.drawLine(int(x_center), int(y_close), int(x_center + tick_len), int(y_close))
            else:
                body_top = min(y_open, y_close)
                body_bottom = max(y_open, y_close)
                body_h = max(1.0, body_bottom - body_top)
                body_w = max(2.0, candle_w * 0.8)
                body_rect = QRectF(x_center - body_w / 2.0, body_top, body_w, body_h)

                # Wicks: Thin 1px line, drawn outside the body so hollow candles remain pristine
                wick_pen = QPen(border_qcolor)
                wick_pen.setStyle(pen_style)
                wick_pen.setWidth(1)
                painter.setPen(wick_pen)
                if y_high < body_top:
                    painter.drawLine(int(x_center), int(y_high), int(x_center), int(body_top))
                if y_low > body_bottom:
                    painter.drawLine(int(x_center), int(body_bottom), int(x_center), int(y_low))

                # Body: Thin 1px border
                body_pen = QPen(border_qcolor)
                body_pen.setStyle(pen_style)
                body_pen.setWidth(1)
                painter.setPen(body_pen)

                if color_info.is_hollow or color_info.fill_color is None:
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    bg_fill = QColor(self.config.default_background_color)
                    painter.fillRect(body_rect, bg_fill)
                    painter.drawRect(body_rect)
                else:
                    fill_qcolor = QColor(color_info.fill_color)
                    painter.setBrush(QBrush(fill_qcolor))
                    painter.drawRect(body_rect)

        painter.restore()

    def _render_overlays(self, painter: QPainter, chart_w: float, chart_h: float) -> None:
        """Render all overlays assigned to this pane with sub-millisecond bisect slicing.

        Painting order per pane (contract section 3):
        zones -> band -> histogram -> level -> line -> marker. Within one
        category the overlay order is preserved.
        """
        indexed_ovs = getattr(self, "_indexed_overlays", {})
        if not indexed_ovs:
            return

        painter.save()
        painter.setClipRect(0, 0, int(chart_w), int(chart_h))
        aa_enabled = getattr(self.config, "enable_antialiasing", True)
        # Antialiasing spreads a 1px diagonal stroke over ~1.86 rows per column
        # (measured) versus 1.00 crisp, so a thin indicator line looked almost
        # twice as heavy as the crisp 1px candle borders. Thin lines are therefore
        # drawn crisp like the candles; wider lines keep AA for smoothness.
        crisp_thin = getattr(self.config, "crisp_thin_indicator_lines", True)

        min_vis_idx = max(0.0, self.x_trans.x_to_bar(0.0) - 2.0)
        max_vis_idx = self.x_trans.x_to_bar(chart_w) + 2.0
        default_line_width = max(1, int(getattr(self.config, "default_indicator_line_width_px", 1)))

        buckets: Dict[str, list] = {overlay_type: [] for overlay_type in OVERLAY_DRAW_ORDER}
        for item in indexed_ovs.values():
            overlay_type = item["overlay"].type
            if overlay_type in buckets:
                buckets[overlay_type].append(item)
        ordered = [item for overlay_type in OVERLAY_DRAW_ORDER for item in buckets[overlay_type]]

        for item in ordered:
            ov = item["overlay"]
            indices = item["indices"]
            pts = item["pts"]
            if not indices:
                continue

            ov_type = ov.type
            style = ov.style
            default_color = "#546E7A" if ov_type == "level" else "#26A69A"
            color = QColor(style.get("color", default_color))

            # Thin indicator lines: AA off so a 1px stroke stays 1px wide, exactly
            # like the candle borders. Everything else keeps antialiasing.
            line_width = (
                resolve_line_width(style, default_line_width)
                if ov_type in ("line", "level")
                else None
            )
            if aa_enabled:
                painter.setRenderHint(
                    QPainter.RenderHint.Antialiasing,
                    not (crisp_thin and line_width is not None and line_width <= 1),
                )

            if ov_type == "zone":
                # Zones draw from the FULL point list: a zone may start left of
                # the viewport while its end is still visible.
                self._render_zones(painter, ov, pts, chart_h)
                continue

            if ov_type == "level":
                self._render_levels(painter, ov, color, line_width, chart_w)
                continue

            i_start = bisect.bisect_left(indices, min_vis_idx)
            i_end = bisect.bisect_right(indices, max_vis_idx)
            vis_pts = pts[i_start : i_end + 1]
            if not vis_pts:
                continue

            if ov_type == "line":
                # A colour change splits the polyline into segments, each drawn
                # with its own pen; the transition point is shared so the line
                # stays connected.
                for seg_color, seg in self._line_color_segments(vis_pts, color.name()):
                    pen = QPen(QColor(seg_color))
                    # Explicit width = pixel count; otherwise the 1px default applies.
                    pen.setWidth(line_width)
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    painter.setPen(pen)
                    if len(seg) > 1:
                        painter.drawPolyline(seg)
                    elif len(seg) == 1:
                        painter.drawPoint(seg[0])

            elif ov_type == "band":
                band_color = QColor(color)
                band_color.setAlpha(style.get("alpha", 40))
                painter.setBrush(QBrush(band_color))
                painter.setPen(Qt.PenStyle.NoPen)

                upper_points = []
                lower_points = []
                for point in vis_pts:
                    if point.value is not None and point.value2 is not None:
                        x = self.x_trans.bar_to_x(point.idx)
                        y1 = self.y_trans.price_to_y(point.value)
                        y2 = self.y_trans.price_to_y(point.value2)
                        upper_points.append(QPointF(x, y1))
                        lower_points.append(QPointF(x, y2))

                if upper_points and lower_points:
                    poly = QPolygonF(upper_points + list(reversed(lower_points)))
                    painter.drawPolygon(poly)

            elif ov_type == "histogram":
                alpha = int(style.get("alpha", 70))
                painter.setPen(Qt.PenStyle.NoPen)

                candle_w = max(1.0, self.x_trans.candle_width_px * 0.8)
                y_zero = self.y_trans.price_to_y(0.0)
                is_center = (ov.origin == "center")

                for point in vis_pts:
                    if point.value is not None:
                        # Per-bar colour from the point's override, else the style colour.
                        bar_color = QColor(point.color_override) if point.color_override else QColor(color)
                        bar_color.setAlpha(alpha)
                        painter.setBrush(QBrush(bar_color))
                        x = self.x_trans.bar_to_x(point.idx)
                        y = self.y_trans.price_to_y(point.value)
                        if is_center:
                            bar_top = min(y, y_zero)
                            bar_h = abs(y - y_zero)
                        else:
                            bar_top = y
                            bar_h = chart_h - y
                        bar_rect = QRectF(x - candle_w / 2.0, bar_top, candle_w, max(1.0, bar_h))
                        painter.drawRect(bar_rect)

        painter.restore()

    def _line_color_segments(self, vis_pts, default_color: str) -> List[tuple]:
        """Split visible line points into (colour, [QPointF]) segments.

        A change of the point's color_override starts a new segment; the
        transition point is shared so the polyline stays visually connected.
        Points without an override use the overlay's style colour.
        """
        segments: List[tuple] = []
        current: List[QPointF] = []
        current_color: Optional[str] = None
        previous: Optional[QPointF] = None
        for point in vis_pts:
            if point.value is None:
                # Gap: close the running segment, the next point starts a new one.
                if current:
                    segments.append((current_color, current))
                current, current_color, previous = [], None, None
                continue
            point_color = str(point.color_override) if point.color_override else default_color
            xy = QPointF(
                self.x_trans.bar_to_x(point.idx),
                self.y_trans.price_to_y(point.value),
            )
            if current and point_color != current_color:
                segments.append((current_color, current))
                current = [previous] if previous is not None else []
                current_color = point_color
            elif not current:
                current_color = point_color
            current.append(xy)
            previous = xy
        if current:
            segments.append((current_color, current))
        return segments

    def zone_rect(
        self,
        t_idx: float,
        t2_idx: float,
        value,
        value2,
        chart_h: float,
    ) -> QRectF:
        """Pixel rectangle of a zone in data space (t..t2 x value..value2).

        A null value/value2 means the zone spans the full pane height. The
        rectangle is normalised (left/top smaller than right/bottom) so a zone
        may be declared in either direction.
        """
        x1 = self.x_trans.bar_to_x(float(t_idx))
        x2 = self.x_trans.bar_to_x(float(t2_idx))
        left, right = (x1, x2) if x1 <= x2 else (x2, x1)
        if value is None or value2 is None:
            top, bottom = 0.0, float(chart_h)
        else:
            y1 = self.y_trans.price_to_y(float(value))
            y2 = self.y_trans.price_to_y(float(value2))
            top, bottom = (y1, y2) if y1 <= y2 else (y2, y1)
        return QRectF(left, top, max(0.0, right - left), max(0.0, bottom - top))

    def _render_zones(self, painter: QPainter, ov: Overlay, pts, chart_h: float) -> None:
        """Background rectangles of a zone overlay (no border, alpha fill)."""
        style = ov.style or {}
        zone_color = QColor(style.get("color", "#26A69A"))
        zone_color.setAlpha(int(style.get("alpha", 30)))
        painter.setBrush(QBrush(zone_color))
        painter.setPen(Qt.PenStyle.NoPen)
        for point in pts:
            if point.t2_idx is None:
                continue  # a zone needs an end time
            rect = self.zone_rect(point.idx, point.t2_idx, point.value, point.value2, chart_h)
            if rect.width() > 0 and rect.height() > 0:
                painter.drawRect(rect)

    def _render_levels(self, painter: QPainter, ov: Overlay, color: QColor, line_width, chart_w: float) -> None:
        """Horizontal reference lines over the full width, with an optional label.

        Levels are decoration: they draw independently of bar timestamps and
        never influence the Y autoscale.
        """
        style = ov.style or {}
        label = style.get("label")
        label_font = QFont(painter.font())
        label_font.setPointSize(8)
        for pt in (ov.values or []):
            val = pt.value if hasattr(pt, "value") else pt.get("value")
            if val is None:
                continue
            y = self.y_trans.price_to_y(float(val))
            pen = QPen(color)
            pen.setWidth(line_width or 1)
            if style.get("dash"):
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(0, int(y), int(chart_w), int(y))
            if label:
                label_color = QColor(color)
                label_color.setAlpha(170)
                painter.setPen(label_color)
                painter.setFont(label_font)
                label_w = max(10.0, min(220.0, chart_w - 4.0))
                painter.drawText(
                    QRectF(chart_w - label_w - 4.0, y - 8.0, label_w, 16.0),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(label),
                )

    # ── Annotations ────────────────────────────────────────────────────

    def _anchor_to_xy(self, anchor) -> tuple[Optional[float], Optional[float]]:
        """Resolve an Anchor to pixel coordinates (data or pixel mode).

        Data anchors use ``t`` (bar timestamp) for X and ``price`` for Y; either
        component may legitimately be missing (e.g. a price-only horizontal line).
        """
        if anchor is None:
            return None, None

        if (getattr(anchor, "mode", "data") or "data") == "pixel":
            x_px = getattr(anchor, "x_px", None)
            y_px = getattr(anchor, "y_px", None)
            return (
                float(x_px) if x_px is not None else None,
                float(y_px) if y_px is not None else None,
            )

        x: Optional[float] = None
        y: Optional[float] = None
        t = getattr(anchor, "t", None)
        if t is not None:
            x = self.x_trans.bar_to_x(self._timestamp_to_bar_idx(int(t)))
        price = getattr(anchor, "price", None)
        if price is not None:
            y = self.y_trans.price_to_y(float(price))
        return x, y

    def _render_annotations(self, painter: QPainter, chart_w: float, chart_h: float) -> None:
        """Render persistent annotations (hline, trendline, rect, text, trade_marker).

        Unresolvable annotations are skipped instead of raising, so one malformed
        drawing object can never break the chart repaint.
        """
        if not self.annotations:
            return

        painter.save()
        painter.setClipRect(0, 0, int(chart_w), int(chart_h))
        if getattr(self.config, "enable_antialiasing", True):
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        label_font = QFont(painter.font())
        label_font.setPointSize(9)

        for ann in self.annotations.values():
            self._render_annotation(painter, ann, chart_w, chart_h, label_font)

        painter.restore()

    def _render_annotation(self, painter: QPainter, ann, chart_w: float, chart_h: float, label_font: QFont) -> None:
        """Render a single annotation according to its type."""
        style = ann.style or {}
        color = QColor(style.get("color") or "#00E676")
        width = max(1, int(style.get("width") or 2))
        label = style.get("text") or ""
        coords = [self._anchor_to_xy(a) for a in (ann.anchors or [])]
        first = coords[0] if coords else (None, None)

        if ann.type == "hline":
            y = next((c[1] for c in coords if c[1] is not None), None)
            if y is None:
                return
            pen = QPen(color)
            pen.setWidth(width)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(0, int(y), int(chart_w), int(y))
            if label:
                self._draw_annotation_label(painter, label, chart_h, y, color, label_font)
            return

        if ann.type == "trade_marker":
            x, y = first
            if y is None:
                return
            if x is None:
                # Price-only marker: pin it to the most recent bar.
                last_idx = float(max(0, len(self.bars) - 1))
                x = self.x_trans.bar_to_x(last_idx)
            is_buy = str(style.get("action", "BUY")).upper() != "SELL"
            self._draw_trade_marker(painter, float(x), float(y), color, is_buy, label, label_font)
            return

        if ann.type == "trendline":
            if len(coords) < 2:
                return
            x1, y1 = coords[0]
            x2, y2 = coords[1]
            if None in (x1, y1, x2, y2):
                return
            pen = QPen(color)
            pen.setWidth(width)
            painter.setPen(pen)
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))
            return

        if ann.type == "rect":
            if len(coords) < 2:
                return
            x1, y1 = coords[0]
            x2, y2 = coords[1]
            if None in (x1, y1, x2, y2):
                return
            rect = QRectF(QPointF(min(x1, x2), min(y1, y2)), QPointF(max(x1, x2), max(y1, y2)))
            fill = QColor(color)
            fill.setAlpha(40)
            pen = QPen(color)
            pen.setWidth(width)
            painter.setPen(pen)
            painter.setBrush(QBrush(fill))
            painter.drawRect(rect)
            return

        if ann.type == "text":
            x, y = first
            if x is None or y is None:
                return
            painter.setFont(label_font)
            painter.setPen(color)
            painter.drawText(QPointF(x, y), label or ann.id)
            return

    def _draw_annotation_label(
        self,
        painter: QPainter,
        text: str,
        chart_h: float,
        y: float,
        color: QColor,
        font: QFont,
    ) -> None:
        """Draw a small left-anchored label pill next to a horizontal line."""
        painter.save()
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_w = float(metrics.horizontalAdvance(text) + 12)
        text_h = float(metrics.height() + 4)
        by = max(0.0, min(chart_h - text_h, y - text_h / 2.0))
        rect = QRectF(6.0, by, text_w, text_h)
        painter.fillRect(rect, QColor(0, 0, 0, 165))
        painter.setPen(color)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def _draw_trade_marker(
        self,
        painter: QPainter,
        x: float,
        y: float,
        color: QColor,
        is_buy: bool,
        label: str,
        font: QFont,
    ) -> None:
        """Draw a BUY/SELL arrow marker at the given pixel position."""
        size = 8.0
        if is_buy:
            triangle = QPolygonF([
                QPointF(x, y),
                QPointF(x - size, y + size * 1.5),
                QPointF(x + size, y + size * 1.5),
            ])
        else:
            triangle = QPolygonF([
                QPointF(x, y),
                QPointF(x - size, y - size * 1.5),
                QPointF(x + size, y - size * 1.5),
            ])

        painter.save()
        painter.setPen(QPen(color, 1))
        painter.setBrush(QBrush(color))
        painter.drawPolygon(triangle)

        if label:
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(QPointF(x + size + 4.0, y + 4.0), label)
        painter.restore()

    # ── Mouse interaction for Y-axis scaling & measure tool ─────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        chart_w = max(10.0, self.width() - Y_AXIS_WIDTH)
        pos = event.position()

        # Log/Lin-Taste hat Vorrang vor Messwerkzeug und Achsen-Skalierung.
        if event.button() == Qt.MouseButton.LeftButton and self.y_scale_button_rect().contains(pos):
            self.toggle_y_scale_type()
            return

        y_handle = self.y_trans.price_to_y(self.y_trans.p_max)
        is_handle = self.is_main and (chart_w - 5.0 <= pos.x() <= chart_w + 20.0) and (abs(pos.y() - y_handle) <= self.config.y_handle_hit_radius_px)

        if event.button() == Qt.MouseButton.LeftButton and is_handle:
            self._is_dragging_y_handle = True
            self.y_axis_mode = "manual"
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            self.mark_dirty()
            return
        elif event.button() == Qt.MouseButton.LeftButton and pos.x() >= chart_w:
            # Y-axis drag scaling
            self._is_scaling_y = True
            self._drag_start_y = pos.y()
            self.y_axis_mode = "manual"
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            self.mark_dirty()
        elif event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            # Right-click (or Middle-click) X-axis pan
            self._is_panning_x = True
            self._pan_start_x = pos.x()
            self.x_trans.pin_to_right = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        elif event.button() == Qt.MouseButton.LeftButton and pos.x() < chart_w:
            # Start measure tool
            self._is_measuring = True
            self._measure_start_pos = pos
            self._measure_start_price = self.y_trans.y_to_price(pos.y())
            self._measure_start_bar = self.x_trans.x_to_bar(pos.x())
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        chart_w = max(10.0, self.width() - Y_AXIS_WIDTH)
        y_handle = self.y_trans.price_to_y(self.y_trans.p_max)
        is_near_handle = self.is_main and (chart_w - 5.0 <= pos.x() <= chart_w + 20.0) and (abs(pos.y() - y_handle) <= self.config.y_handle_hit_radius_px)

        if getattr(self, "_is_panning_x", False):
            dx = pos.x() - self._pan_start_x
            self._pan_start_x = pos.x()
            self.x_pan_requested.emit(dx)
            return
        elif self._is_dragging_y_handle:
            self.y_trans.set_top_boundary_px(pos.y())
            self.mark_dirty()
            self.y_scale_changed.emit()
            self.update()
            return
        elif self._is_scaling_y:
            dy = pos.y() - self._drag_start_y
            self._drag_start_y = pos.y()
            scale_factor = 1.0 - (dy * 0.008)
            if scale_factor > 0:
                self.y_trans.manual_scale(scale_factor)
                self.mark_dirty()
                self.y_scale_changed.emit()
        else:
            is_over_scale_button = self.y_scale_button_rect().contains(pos)
            if is_over_scale_button != self._is_hovering_scale_button:
                self._is_hovering_scale_button = is_over_scale_button
                self.setToolTip(self._y_scale_tooltip() if is_over_scale_button else "")
                self.update()

            if is_over_scale_button:
                if self._is_hovering_y_handle:
                    self._is_hovering_y_handle = False
                    self.update()
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            elif is_near_handle:
                if not self._is_hovering_y_handle:
                    self._is_hovering_y_handle = True
                    self.update()
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                if self._is_hovering_y_handle:
                    self._is_hovering_y_handle = False
                    self.update()
                if pos.x() >= chart_w:
                    self.setCursor(Qt.CursorShape.SizeVerCursor)
                else:
                    self.setCursor(Qt.CursorShape.CrossCursor)

            # Emit crosshair position to parent canvas
            self.crosshair_moved_signal.emit(self.pane_id, pos.x(), pos.y())
            # Repaint for measure tool update
            if self._is_measuring:
                self.update()
            super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        """Clear crosshair and handle hover when mouse leaves this pane."""
        if self._is_hovering_y_handle:
            self._is_hovering_y_handle = False
            self.update()
        if self._is_hovering_scale_button:
            self._is_hovering_scale_button = False
            self.setToolTip("")
            self.update()
        if getattr(self, "_is_panning_x", False):
            self._is_panning_x = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.crosshair_moved_signal.emit(self.pane_id, -1.0, -1.0)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if getattr(self, "_is_panning_x", False) and event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._is_panning_x = False
            chart_w = max(10.0, self.width() - Y_AXIS_WIDTH)
            self.setCursor(Qt.CursorShape.CrossCursor if event.position().x() < chart_w else Qt.CursorShape.SizeVerCursor)
            return
        elif self._is_dragging_y_handle:
            self._is_dragging_y_handle = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
        elif self._is_scaling_y:
            self._is_scaling_y = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif self._is_measuring and event.button() == Qt.MouseButton.LeftButton:
            self._is_measuring = False
            self._measure_start_pos = None
            self.update()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        chart_w = max(10.0, self.width() - Y_AXIS_WIDTH)
        pos = event.position()
        y_handle = self.y_trans.price_to_y(self.y_trans.p_max)
        is_near_handle = (chart_w - 5.0 <= pos.x() <= chart_w + 20.0) and (abs(pos.y() - y_handle) <= self.config.y_handle_hit_radius_px)

        if pos.x() >= chart_w or is_near_handle:
            # Double-click on Y-axis or handle → reset to auto & default boundary
            self.y_axis_mode = "auto"
            self.y_trans.reset_boundary()
            self.update_y_range()
            self.mark_dirty()
        else:
            super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return

        chart_w = max(10.0, w - Y_AXIS_WIDTH)
        chart_h = float(h)
        content_h = max(10.0, chart_h - X_AXIS_HEIGHT) if self.draw_x_axis else chart_h

        self.y_trans.viewport_height_px = content_h
        self.x_trans.set_viewport_width(chart_w)
        self.update_y_range()
        self.mark_dirty()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) and not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            angle = event.angleDelta().y()
            # Wheel UP (angle > 0) zooms in (candles grow taller, headroom decreases)
            # Wheel DOWN (angle < 0) zooms out (candles compress down, headroom increases)
            factor = (1.0 / 1.15) if angle > 0 else 1.15
            self.y_axis_mode = "manual"
            self.y_trans.manual_scale(factor)
            self.mark_dirty()
            self.y_scale_changed.emit()
        else:
            # Forward to parent for X-axis zoom or Ctrl+wheel scroll
            super().wheelEvent(event)
