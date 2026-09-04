"""Layer 4: Interaction Overlay (Crosshair, ephemeral Measure Tool, Drag Preview) according to Section 6.2 & 7."""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont
from chart_viewer.ui.layers.base import ChartLayer
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.coords.y_axis import YAxisTransform
from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig


class InteractionLayer(ChartLayer):
    """Layer 4: Redrawn on mouse movement without repainting Layers 1-3."""

    def __init__(self, config: ViewerConfig | None = None):
        self.config = config or GLOBAL_CONFIG
        self.crosshair_pos: Optional[QPointF] = None
        self.is_crosshair_visible: bool = False

        # Measure tool state (Section 7: Ephemeral, discarded on release)
        self.is_measuring: bool = False
        self.measure_start_pos: Optional[QPointF] = None
        self.measure_start_price: float = 0.0
        self.measure_start_bar: float = 0.0
        self.measure_start_time: int = 0

        self.bar_duration: int = 86400
        self.base_timestamp: int = 0

    def set_time_base(self, base_timestamp: int, bar_duration: int) -> None:
        self.base_timestamp = base_timestamp
        self.bar_duration = max(1, bar_duration)

    def set_crosshair(self, pos: Optional[QPointF]) -> None:
        self.crosshair_pos = pos
        self.is_crosshair_visible = pos is not None

    def start_measuring(self, pos: QPointF, price: float, bar_index: float, timestamp: int) -> None:
        self.is_measuring = True
        self.measure_start_pos = pos
        self.measure_start_price = price
        self.measure_start_bar = bar_index
        self.measure_start_time = timestamp

    def stop_measuring(self) -> None:
        """Immediately discard measurement without persistence (Section 7)."""
        self.is_measuring = False
        self.measure_start_pos = None

    def render(
        self,
        painter: QPainter,
        x_trans: XAxisTransform,
        y_trans: YAxisTransform,
        width: int,
        height: int,
    ) -> None:
        # 1. Render Measuring Tool if active
        if self.is_measuring and self.measure_start_pos and self.crosshair_pos:
            self._render_measure_tool(painter, x_trans, y_trans, width, height)

        # 2. Render Crosshair
        if self.is_crosshair_visible and self.crosshair_pos:
            self._render_crosshair(painter, x_trans, y_trans, width, height)

    def _render_crosshair(
        self,
        painter: QPainter,
        x_trans: XAxisTransform,
        y_trans: YAxisTransform,
        width: int,
        height: int,
    ) -> None:
        cx = int(self.crosshair_pos.x())
        cy = int(self.crosshair_pos.y())

        # Crosshair lines
        ch_pen = QPen(QColor(self.config.default_crosshair_color))
        ch_pen.setStyle(Qt.PenStyle.DashLine)
        ch_pen.setWidth(1)
        painter.setPen(ch_pen)

        # Vertical line
        painter.drawLine(cx, 0, cx, height)
        # Horizontal line
        painter.drawLine(0, cy, width, cy)

        # Price Badge on Y-axis (right side)
        current_price = y_trans.y_to_price(cy)
        price_str = f"{current_price:.2f}"
        badge_w = 60
        badge_h = 20
        badge_rect = QRectF(width - badge_w, cy - badge_h / 2.0, badge_w, badge_h)

        painter.fillRect(badge_rect, QColor("#363A45"))
        painter.setPen(QColor("#FFFFFF"))
        font = painter.font()
        font.setPointSize(9)
        painter.setFont(font)
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, price_str)

        # Time Badge on X-axis (bottom)
        bar_idx = x_trans.x_to_bar(cx)
        t_sec = self.base_timestamp + int(bar_idx * self.bar_duration)
        try:
            dt = datetime.fromtimestamp(t_sec, tz=timezone.utc)
            time_str = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            time_str = f"Bar {int(bar_idx)}"

        time_badge_w = 120
        time_badge_h = 20
        time_rect = QRectF(cx - time_badge_w / 2.0, height - time_badge_h, time_badge_w, time_badge_h)
        painter.fillRect(time_rect, QColor("#363A45"))
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(time_rect, Qt.AlignmentFlag.AlignCenter, time_str)

    def _render_measure_tool(
        self,
        painter: QPainter,
        x_trans: XAxisTransform,
        y_trans: YAxisTransform,
        width: int,
        height: int,
    ) -> None:
        p1 = self.measure_start_pos
        p2 = self.crosshair_pos

        rx = min(p1.x(), p2.x())
        ry = min(p1.y(), p2.y())
        rw = abs(p2.x() - p1.x())
        rh = abs(p2.y() - p1.y())
        rect = QRectF(rx, ry, rw, rh)

        # Translucent bounding box
        fill_color = QColor("#2962FF")
        fill_color.setAlpha(35)
        painter.fillRect(rect, fill_color)

        border_pen = QPen(QColor("#2962FF"))
        border_pen.setWidth(1)
        border_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(border_pen)
        painter.drawRect(rect)

        # Delta metrics calculation
        current_price = y_trans.y_to_price(p2.y())
        delta_price = current_price - self.measure_start_price
        start_p = self.measure_start_price if self.measure_start_price != 0 else 1.0
        delta_pct = (delta_price / start_p) * 100.0

        current_bar = x_trans.x_to_bar(p2.x())
        delta_bars = int(abs(current_bar - self.measure_start_bar))

        sign = "+" if delta_price >= 0 else ""
        info_text = f"{sign}{delta_price:.2f} ({sign}{delta_pct:.2f}%)\n{delta_bars} bars"

        # Draw floating info badge
        info_rect = QRectF(p2.x() + 10, p2.y() - 40, 130, 40)
        painter.fillRect(info_rect, QColor(0, 0, 0, 180))
        painter.setPen(QColor("#00E676" if delta_price >= 0 else "#FF5252"))
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(info_rect, Qt.AlignmentFlag.AlignCenter, info_text)
