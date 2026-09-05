"""Layer 2: Data rendering (Candlesticks, Thin-bars, Volume, Overlays) according to Section 2.2 & 6.1."""

from __future__ import annotations
import math
from typing import List, Dict, Optional
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPolygonF
from chart_viewer.ui.layers.base import ChartLayer
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.coords.y_axis import YAxisTransform
from chart_viewer.models.entities import Bar, Overlay, OverlayPoint
from chart_viewer.models.color import resolve_bar_color, ResolvedBarColor
from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig


class DataLayer(ChartLayer):
    """Renders candlesticks, volume, and indicator overlays."""

    def __init__(self, config: ViewerConfig | None = None):
        self.config = config or GLOBAL_CONFIG
        self.bars: List[Bar] = []
        self.overlays: Dict[str, Overlay] = {}
        self.series_style: dict = {}

    def set_data(
        self,
        bars: List[Bar],
        overlays: Dict[str, Overlay],
        series_style: dict | None = None,
    ) -> None:
        self.bars = bars
        self.overlays = overlays
        self.series_style = series_style or {}

    def render(
        self,
        painter: QPainter,
        x_trans: XAxisTransform,
        y_trans: YAxisTransform,
        width: int,
        height: int,
    ) -> None:
        if not self.bars:
            return

        chart_w = x_trans.viewport_width_px
        chart_h = y_trans.viewport_height_px

        painter.save()
        painter.setClipRect(0, 0, int(chart_w), int(chart_h))

        candle_w = x_trans.candle_width_px
        is_thin_bar = candle_w < self.config.thin_bar_threshold_px

        # Effective wick width clamp: clamp(configured_wick_px, 1, max(1, candle_width_px - 1))
        configured_wick = self.config.configured_wick_px
        effective_wick_px = max(1, min(configured_wick, max(1, int(candle_w - 1))))

        # Determine visible bar index range
        min_idx = max(0, math.floor(x_trans.min_visible_bar_index))
        max_idx = min(len(self.bars) - 1, math.ceil(x_trans.right_index))

        # Volume baseline (bottom 20% of chart area)
        vol_height = chart_h * 0.20
        max_vol = max((b.volume for b in self.bars[min_idx : max_idx + 1]), default=1.0)
        if max_vol <= 0:
            max_vol = 1.0

        # 1. Render Volume Bars first
        for i in range(min_idx, max_idx + 1):
            bar = self.bars[i]
            x_center = x_trans.bar_to_x(i)
            if x_center + candle_w < 0 or x_center - candle_w > chart_w:
                continue

            color_info = resolve_bar_color(bar, self.series_style, self.config)
            vol_h = (bar.volume / max_vol) * vol_height
            vol_y = chart_h - vol_h

            vol_color = QColor(color_info.border_color)
            vol_color.setAlpha(60)  # Subtle translucent volume

            painter.fillRect(
                QRectF(x_center - candle_w * 0.4, vol_y, max(1.0, candle_w * 0.8), vol_h),
                vol_color,
            )

        # 2. Render Candlesticks / Thin-Bars
        for i in range(min_idx, max_idx + 1):
            bar = self.bars[i]
            x_center = x_trans.bar_to_x(i)
            if x_center + candle_w < 0 or x_center - candle_w > width:
                continue

            color_info = resolve_bar_color(bar, self.series_style, self.config)
            border_qcolor = QColor(color_info.border_color)

            y_high = y_trans.price_to_y(bar.high)
            y_low = y_trans.price_to_y(bar.low)
            y_open = y_trans.price_to_y(bar.open)
            y_close = y_trans.price_to_y(bar.close)

            # Warning flag (invalid bar): dashed outline (Section 2.3)
            pen_style = Qt.PenStyle.SolidLine if bar.is_valid else Qt.PenStyle.DashLine

            if is_thin_bar:
                # Thin-bar mode (Section 6.1): vertical line + left tick (open) + right tick (close)
                pen = QPen(border_qcolor)
                pen.setStyle(pen_style)
                pen.setWidth(1)
                painter.setPen(pen)

                # High-Low vertical line
                painter.drawLine(int(x_center), int(y_high), int(x_center), int(y_low))

                # Left tick for Open
                tick_len = max(1.0, candle_w * 0.8)
                painter.drawLine(int(x_center - tick_len), int(y_open), int(x_center), int(y_open))

                # Right tick for Close
                painter.drawLine(int(x_center), int(y_close), int(x_center + tick_len), int(y_close))

            else:
                # Regular Candlestick Mode
                # Wick
                wick_pen = QPen(border_qcolor)
                wick_pen.setStyle(pen_style)
                wick_pen.setWidth(effective_wick_px)
                painter.setPen(wick_pen)
                painter.drawLine(int(x_center), int(y_high), int(x_center), int(y_low))

                # Body
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
                    # For hollow down-candle with background color inside
                    bg_fill = QColor(self.config.default_background_color)
                    painter.fillRect(body_rect, bg_fill)
                    painter.drawRect(body_rect)
                else:
                    fill_qcolor = QColor(color_info.fill_color)
                    painter.setBrush(QBrush(fill_qcolor))
                    painter.drawRect(body_rect)

        # 3. Render Overlays (lines, bands, histograms)
        for ov_id, ov in self.overlays.items():
            self._render_overlay(painter, ov, x_trans, y_trans, width, height)

    def _render_overlay(
        self,
        painter: QPainter,
        overlay: Overlay,
        x_trans: XAxisTransform,
        y_trans: YAxisTransform,
        width: int,
        height: int,
    ) -> None:
        if not overlay.values:
            return

        ov_type = overlay.type
        style = overlay.style
        color = QColor(style.get("color", "#26A69A"))
        width_px = style.get("width", 2)

        # Prebuild timestamp -> bar index map for exact candle alignment across weekends/holidays
        ts_map = {b.t_open: i for i, b in enumerate(self.bars)}
        bar_ts = [b.t_open for b in self.bars]
        import bisect

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

        if ov_type == "line":
            pen = QPen(color)
            pen.setWidth(width_px)
            painter.setPen(pen)

            points = []
            for pt in overlay.values:
                try:
                    t, val, _ = get_pt_values(pt)
                    idx = get_bar_idx(t)
                    x = x_trans.bar_to_x(idx)
                    y = y_trans.price_to_y(val)
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
            for pt in overlay.values:
                try:
                    t, val, val2 = get_pt_values(pt)
                    idx = get_bar_idx(t)
                    x = x_trans.bar_to_x(idx)
                    y1 = y_trans.price_to_y(val)
                    y2 = y_trans.price_to_y(val2)
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
            
            # Find max value for scaling at the bottom 20%
            valid_vals = [get_pt_values(pt)[1] for pt in overlay.values if get_pt_values(pt)[1] is not None]
            max_val = max(valid_vals) if valid_vals else 1.0
            if max_val == 0: max_val = 1.0
            hist_height = height * 0.2
            y_base = float(height)

            candle_w = max(1.0, x_trans.candle_width_px * 0.8)
            for pt in overlay.values:
                try:
                    t, val, _ = get_pt_values(pt)
                    idx = get_bar_idx(t)
                    x = x_trans.bar_to_x(idx)
                    
                    bar_h = (val / max_val) * hist_height
                    bar_rect = QRectF(x - candle_w / 2.0, y_base - bar_h, candle_w, bar_h)
                    painter.drawRect(bar_rect)
                except Exception:
                    continue
