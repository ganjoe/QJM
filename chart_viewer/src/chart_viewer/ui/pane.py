"""ChartPane: A single vertical pane with its own Y-axis, rendering overlays or candlesticks."""

from __future__ import annotations
import math
import bisect
from typing import List, Dict, Optional
from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QPolygonF, QPixmap, QMouseEvent, QWheelEvent

from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.coords.y_axis import YAxisTransform
from chart_viewer.models.entities import Bar, Overlay, OverlayPoint
from chart_viewer.models.color import resolve_bar_color

Y_AXIS_WIDTH = 70.0


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

    def __init__(
        self,
        pane_id: str,
        x_trans: XAxisTransform,
        is_main: bool = False,
        config: ViewerConfig | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.pane_id = pane_id
        self.x_trans = x_trans  # Shared reference — same object across all panes
        self.y_trans = YAxisTransform(config=config or GLOBAL_CONFIG)
        self.is_main = is_main
        self.config = config or GLOBAL_CONFIG
        self.draw_x_axis = False  # Only the bottom pane draws the X-axis

        # Data
        self.bars: List[Bar] = []
        self.overlays: Dict[str, Overlay] = {}
        self.series_style: dict = {}
        self.y_axis_mode: str = "auto"
        self.watermark_text: str = ""
        self.base_timestamp: int = 0
        self.bar_duration: int = 86400

        # Cache
        self._pixmap: Optional[QPixmap] = None
        self._dirty: bool = True

        # Interaction
        self._drag_start_y: float = 0.0
        self._is_scaling_y: bool = False

        # Crosshair
        self._crosshair_x: Optional[float] = None   # Shared X pixel (vertical line)
        self._crosshair_y: Optional[float] = None   # Local Y pixel (horizontal line, only in active pane)
        self._is_crosshair_active: bool = False      # True if mouse is in THIS pane

        # Measure tool
        self._is_measuring: bool = False
        self._measure_start_pos: Optional[QPointF] = None
        self._measure_start_price: float = 0.0
        self._measure_start_bar: float = 0.0

        self.setMouseTracking(True)

    def set_data(
        self,
        bars: List[Bar],
        overlays: Dict[str, Overlay],
        series_style: dict | None = None,
    ) -> None:
        self.bars = bars
        self.overlays = overlays
        self.series_style = series_style or {}
        self.mark_dirty()

    def mark_dirty(self) -> None:
        self._dirty = True
        self.update()

    def update_y_range(self) -> None:
        """Auto-fit Y-range to visible data."""
        if self.y_axis_mode == "manual":
            return
        if not self.bars and not self.overlays:
            return

        min_idx = max(0, int(self.x_trans.min_visible_bar_index))
        max_idx_val = int(self.x_trans.right_index)

        p_min = float("inf")
        p_max = float("-inf")

        # Price range from candlesticks (main pane only)
        if self.is_main and self.bars:
            bar_max_idx = min(len(self.bars) - 1, max_idx_val)
            if min_idx <= bar_max_idx:
                visible_bars = self.bars[min_idx : bar_max_idx + 1]
                p_min = min(p_min, min(b.low for b in visible_bars))
                p_max = max(p_max, max(b.high for b in visible_bars))

        # Value range from overlays
        for ov in self.overlays.values():
            for pt in ov.values:
                t = pt.t if hasattr(pt, "t") else pt.get("t", 0)
                val = pt.value if hasattr(pt, "value") else pt.get("value", 0)
                # Rough check if point is in visible range
                if self.bars:
                    bar_idx = self._timestamp_to_bar_idx(int(t))
                    if bar_idx < min_idx or bar_idx > max_idx_val:
                        continue
                if val is not None:
                    p_min = min(p_min, float(val))
                    p_max = max(p_max, float(val))
                    val2 = getattr(pt, "value2", None) if hasattr(pt, "value2") else pt.get("value2")
                    if val2 is not None:
                        p_min = min(p_min, float(val2))
                        p_max = max(p_max, float(val2))

        if p_min == float("inf") or p_max == float("-inf"):
            return

        self.y_trans.fit_range(p_min, p_max, requested_mode="linear")

    def _timestamp_to_bar_idx(self, t: int) -> float:
        """Convert timestamp to fractional bar index using bar timestamps."""
        if not self.bars:
            return 0.0
        ts_list = [b.t_open for b in self.bars]
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
        self.y_trans.viewport_height_px = chart_h

        if self._dirty or self._pixmap is None or self._pixmap.size() != self.size():
            pixmap = QPixmap(self.size())
            pp = QPainter(pixmap)
            try:
                pp.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                self._render_background(pp, chart_w, chart_h, w, h)

                if self.is_main:
                    self._render_candlesticks(pp, chart_w, chart_h)

                self._render_overlays(pp, chart_w, chart_h)
                self._render_y_axis(pp, chart_w, chart_h)
            finally:
                pp.end()
            self._pixmap = pixmap
            self._dirty = False

        screen_painter = QPainter(self)
        try:
            screen_painter.drawPixmap(0, 0, self._pixmap)
            chart_w_live = max(10.0, w - Y_AXIS_WIDTH)
            chart_h_live = float(h)
            # Crosshair is drawn live (not cached) for sub-ms response
            self._render_crosshair(screen_painter, chart_w_live, chart_h_live)
            # Measure tool drawn live
            if self._is_measuring and self._measure_start_pos and self._crosshair_y is not None:
                self._render_measure_tool(screen_painter, chart_w_live, chart_h_live)
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

    def _render_y_axis(self, painter: QPainter, chart_w: float, chart_h: float) -> None:
        """Render Y-axis labels, ticks, and scale handle for this pane."""
        num_ticks = max(2, int(chart_h / 80))
        scale_font = QFont(painter.font())
        scale_font.setPointSize(9)
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

        # Scale handle pill
        gx = int(chart_w + Y_AXIS_WIDTH / 2.0)
        handle_font = QFont(painter.font())
        handle_font.setPointSize(7)
        handle_font.setBold(True)
        painter.setFont(handle_font)

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

    def set_crosshair(self, x: Optional[float], y: Optional[float], is_active: bool) -> None:
        """Set crosshair position. x = shared X pixel, y = local Y pixel (only if active)."""
        self._crosshair_x = x
        self._crosshair_y = y if is_active else None
        self._is_crosshair_active = is_active
        self.update()  # Trigger repaint (only live layer, pixmap stays cached)

    def _render_crosshair(self, painter: QPainter, chart_w: float, chart_h: float) -> None:
        """Draw crosshair lines live (not cached) for instant response."""
        if self._crosshair_x is None:
            return

        from datetime import datetime, timezone

        cx = int(self._crosshair_x)

        ch_pen = QPen(QColor(self.config.default_crosshair_color))
        ch_pen.setStyle(Qt.PenStyle.DashLine)
        ch_pen.setWidth(1)
        painter.setPen(ch_pen)

        # Vertical line — always drawn in all panes
        painter.drawLine(cx, 0, cx, int(chart_h))

        # Horizontal line + price badge — only in the active pane
        if self._is_crosshair_active and self._crosshair_y is not None:
            cy = int(self._crosshair_y)
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

        min_idx = max(0, math.floor(self.x_trans.min_visible_bar_index))
        max_idx = min(len(self.bars) - 1, math.ceil(self.x_trans.right_index))

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
                wick_pen = QPen(border_qcolor)
                wick_pen.setStyle(pen_style)
                wick_pen.setWidth(effective_wick_px)
                painter.setPen(wick_pen)
                painter.drawLine(int(x_center), int(y_high), int(x_center), int(y_low))

                body_top = min(y_open, y_close)
                body_bottom = max(y_open, y_close)
                body_h = max(1.0, body_bottom - body_top)
                body_w = max(2.0, candle_w * 0.8)
                body_rect = QRectF(x_center - body_w / 2.0, body_top, body_w, body_h)

                body_pen = QPen(border_qcolor)
                body_pen.setStyle(pen_style)
                body_pen.setWidth(color_info.border_width)
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
        """Render all overlays assigned to this pane."""
        if not self.overlays:
            return

        painter.save()
        painter.setClipRect(0, 0, int(chart_w), int(chart_h))

        # Pre-build timestamp → bar index map
        ts_map = {b.t_open: i for i, b in enumerate(self.bars)} if self.bars else {}
        bar_ts = [b.t_open for b in self.bars] if self.bars else []

        def get_bar_idx(t_val: int) -> float:
            if t_val in ts_map:
                return float(ts_map[t_val])
            if not bar_ts:
                return 0.0
            pos = bisect.bisect_left(bar_ts, t_val)
            if pos <= 0:
                return 0.0
            if pos >= len(bar_ts):
                return float(len(bar_ts) - 1)
            t_prev, t_next = bar_ts[pos - 1], bar_ts[pos]
            if t_next > t_prev:
                frac = (t_val - t_prev) / (t_next - t_prev)
                return float(pos - 1) + frac
            return float(pos)

        def get_pt_values(pt):
            t = pt.t if hasattr(pt, "t") else pt.get("t")
            val = pt.value if hasattr(pt, "value") else pt.get("value")
            val2 = getattr(pt, "value2", None) if hasattr(pt, "value2") else pt.get("value2")
            return int(t), float(val), (float(val2) if val2 is not None else float(val))

        for ov in self.overlays.values():
            if not ov.values:
                continue

            ov_type = ov.type
            style = ov.style
            color = QColor(style.get("color", "#26A69A"))

            if ov_type == "line":
                pen = QPen(color)
                pen.setWidth(style.get("width", 2))
                painter.setPen(pen)

                points = []
                for pt in ov.values:
                    try:
                        t, val, _ = get_pt_values(pt)
                        idx = get_bar_idx(t)
                        x = self.x_trans.bar_to_x(idx)
                        y = self.y_trans.price_to_y(val)
                        points.append(QPointF(x, y))
                    except Exception:
                        continue

                for j in range(len(points) - 1):
                    painter.drawLine(points[j], points[j + 1])

            elif ov_type == "band":
                band_color = QColor(color)
                band_color.setAlpha(style.get("alpha", 40))
                painter.setBrush(QBrush(band_color))
                painter.setPen(Qt.PenStyle.NoPen)

                upper_points = []
                lower_points = []
                for pt in ov.values:
                    try:
                        t, val, val2 = get_pt_values(pt)
                        idx = get_bar_idx(t)
                        x = self.x_trans.bar_to_x(idx)
                        y1 = self.y_trans.price_to_y(val)
                        y2 = self.y_trans.price_to_y(val2)
                        upper_points.append(QPointF(x, y1))
                        lower_points.append(QPointF(x, y2))
                    except Exception:
                        continue

                if upper_points and lower_points:
                    poly = QPolygonF(upper_points + list(reversed(lower_points)))
                    painter.drawPolygon(poly)

            elif ov_type == "histogram":
                hist_color = QColor(color)
                hist_color.setAlpha(style.get("alpha", 70))
                painter.setBrush(QBrush(hist_color))
                painter.setPen(Qt.PenStyle.NoPen)

                candle_w = max(1.0, self.x_trans.candle_width_px * 0.8)
                for pt in ov.values:
                    try:
                        t, val, _ = get_pt_values(pt)
                        idx = get_bar_idx(t)
                        x = self.x_trans.bar_to_x(idx)
                        # Use the pane's own Y-axis for proper scaling
                        y = self.y_trans.price_to_y(val)
                        y_zero = self.y_trans.price_to_y(0.0)

                        if ov.origin == "center":
                            # Bars grow from center (zero line)
                            bar_top = min(y, y_zero)
                            bar_h = abs(y - y_zero)
                        else:
                            # Bars grow from bottom
                            bar_top = y
                            bar_h = chart_h - y

                        bar_rect = QRectF(x - candle_w / 2.0, bar_top, candle_w, max(1.0, bar_h))
                        painter.drawRect(bar_rect)
                    except Exception:
                        continue

        painter.restore()

    # ── Mouse interaction for Y-axis scaling & measure tool ─────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        chart_w = max(10.0, self.width() - Y_AXIS_WIDTH)
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton and pos.x() >= chart_w:
            # Y-axis drag scaling
            self._is_scaling_y = True
            self._drag_start_y = pos.y()
            self.y_axis_mode = "manual"
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            self.mark_dirty()
        elif event.button() == Qt.MouseButton.LeftButton and pos.x() < chart_w:
            # Start measure tool
            self._is_measuring = True
            self._measure_start_pos = pos
            self._measure_start_price = self.y_trans.y_to_price(pos.y())
            self._measure_start_bar = self.x_trans.x_to_bar(pos.x())
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_scaling_y:
            dy = event.position().y() - self._drag_start_y
            self._drag_start_y = event.position().y()
            scale_factor = 1.0 - (dy * 0.008)
            if scale_factor > 0:
                self.y_trans.manual_scale(scale_factor)
                self.mark_dirty()
                self.y_scale_changed.emit()
        else:
            pos = event.position()
            chart_w = max(10.0, self.width() - Y_AXIS_WIDTH)
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
        """Clear crosshair when mouse leaves this pane."""
        self.crosshair_moved_signal.emit(self.pane_id, -1.0, -1.0)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._is_scaling_y:
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
        if event.position().x() >= chart_w:
            # Double-click on Y-axis → reset to auto
            self.y_axis_mode = "auto"
            self.update_y_range()
            self.mark_dirty()
        else:
            super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            angle = event.angleDelta().y()
            factor = 1.15 if angle > 0 else (1.0 / 1.15)
            self.y_axis_mode = "manual"
            self.y_trans.manual_scale(factor)
            self.mark_dirty()
            self.y_scale_changed.emit()
        else:
            # Forward to parent for X-axis zoom
            super().wheelEvent(event)
