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
        if not p1 or not p2:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 1. Pure straight line (Gerade) connecting start (p1) and end (p2)
        line_pen = QPen(QColor("#2962FF"))
        line_pen.setWidth(2)
        line_pen.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(line_pen)
        painter.drawLine(p1, p2)

        # Distinct endpoint anchor handles (outer ring + inner dot)
        painter.setPen(QPen(QColor("#2962FF"), 2))
        painter.setBrush(QBrush(QColor("#FFFFFF")))
        painter.drawEllipse(p1, 5.0, 5.0)
        painter.drawEllipse(p2, 5.0, 5.0)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#2962FF")))
        painter.drawEllipse(p1, 2.0, 2.0)
        painter.drawEllipse(p2, 2.0, 2.0)

        # 2. Delta metrics calculation
        current_price = y_trans.y_to_price(p2.y())
        delta_price = current_price - self.measure_start_price
        start_p = self.measure_start_price if self.measure_start_price != 0 else 1.0
        delta_pct = (delta_price / start_p) * 100.0

        current_bar = x_trans.x_to_bar(p2.x())
        delta_bars = int(abs(current_bar - self.measure_start_bar))
        days = delta_bars if self.bar_duration == 86400 else max(1, int(delta_bars * self.bar_duration / 86400))

        is_pos = delta_price >= 0
        accent_color = QColor("#00E676" if is_pos else "#FF5252")
        arrow = "▲" if is_pos else "▼"
        sign = "+" if is_pos else ""

        # 3. TC2000-Style Floating Infobox Card
        card_w = 185.0
        card_h = 72.0

        bx = p2.x() + 18.0
        if bx + card_w > width - 72.0:
            bx = p2.x() - card_w - 18.0
        if bx < 10.0:
            bx = 10.0

        by = p2.y() - card_h - 12.0
        if by < 10.0:
            by = p2.y() + 16.0
        if by + card_h > height - 25.0:
            by = height - 25.0 - card_h

        card_rect = QRectF(bx, by, card_w, card_h)

        # Subtle shadow
        painter.fillRect(card_rect.translated(2, 2), QColor(0, 0, 0, 90))

        # Card background with glassmorphism border
        painter.setBrush(QBrush(QColor(18, 22, 30, 245)))
        painter.setPen(QPen(QColor(55, 65, 85), 1.5))
        painter.drawRoundedRect(card_rect, 6, 6)

        # Header: Delta % and Delta $
        header_font = QFont(painter.font())
        header_font.setPointSize(11)
        header_font.setBold(True)
        painter.setFont(header_font)
        painter.setPen(accent_color)
        header_text = f"{arrow} {sign}{delta_pct:.2f}% ({sign}{delta_price:.2f} $)"
        painter.drawText(QRectF(bx + 12, by + 8, card_w - 24, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, header_text)

        # Line 2: Bars & Days
        sub_font = QFont(painter.font())
        sub_font.setPointSize(9)
        sub_font.setBold(False)
        painter.setFont(sub_font)
        painter.setPen(QColor("#D1D4DC"))
        line2_text = f"{delta_bars} Bars • {days} Tage"
        painter.drawText(QRectF(bx + 12, by + 30, card_w - 24, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line2_text)

        # Line 3: Start -> End Price
        painter.setPen(QColor("#848E9C"))
        line3_text = f"{self.measure_start_price:.2f} $ → {current_price:.2f} $"
        painter.drawText(QRectF(bx + 12, by + 48, card_w - 24, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line3_text)

        painter.restore()

