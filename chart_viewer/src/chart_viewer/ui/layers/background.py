"""Layer 1: Background rendering (Grid, Watermark, Y-Axis Price Scale, X-Axis Time Scale)."""

from __future__ import annotations
import math
from datetime import datetime, timezone
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QFont
from chart_viewer.ui.layers.base import ChartLayer
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.coords.y_axis import YAxisTransform
from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig

Y_AXIS_WIDTH = 70.0
X_AXIS_HEIGHT = 22.0


class BackgroundLayer(ChartLayer):
    """Renders background, gridlines, symbol watermark, price scale, and date scale."""

    def __init__(self, watermark_text: str = "", config: ViewerConfig | None = None):
        self.watermark_text = watermark_text
        self.config = config or GLOBAL_CONFIG
        self.base_timestamp: int = 0
        self.bar_duration: int = 86400
        self.axis_mode: str = "auto"

    def set_watermark(self, text: str) -> None:
        self.watermark_text = text

    def set_time_base(self, base_timestamp: int, bar_duration: int) -> None:
        self.base_timestamp = base_timestamp
        self.bar_duration = max(1, bar_duration)

    def set_axis_mode(self, mode: str) -> None:
        self.axis_mode = mode

    def render(
        self,
        painter: QPainter,
        x_trans: XAxisTransform,
        y_trans: YAxisTransform,
        width: int,
        height: int,
    ) -> None:
        chart_w = max(10.0, width - Y_AXIS_WIDTH)
        chart_h = max(10.0, height - X_AXIS_HEIGHT)

        # 1. Fill background zones
        bg_color = QColor(self.config.default_background_color)
        painter.fillRect(QRectF(0, 0, chart_w, chart_h), bg_color)

        gutter_color = QColor("#161922")
        painter.fillRect(QRectF(chart_w, 0, Y_AXIS_WIDTH, chart_h), gutter_color)
        painter.fillRect(QRectF(0, chart_h, chart_w, X_AXIS_HEIGHT), gutter_color)
        painter.fillRect(QRectF(chart_w, chart_h, Y_AXIS_WIDTH, X_AXIS_HEIGHT), QColor("#12141B"))

        # Axis Separator Lines
        axis_border_pen = QPen(QColor("#2A2E39"))
        axis_border_pen.setWidth(1)
        painter.setPen(axis_border_pen)
        painter.drawLine(int(chart_w), 0, int(chart_w), height)
        painter.drawLine(0, int(chart_h), width, int(chart_h))

        # 2. Watermark
        if self.watermark_text:
            painter.save()
            watermark_color = QColor(self.config.default_text_color)
            watermark_color.setAlpha(18)
            painter.setPen(watermark_color)
            font = QFont(painter.font())
            font.setPointSize(44)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(0, 0, chart_w, chart_h), Qt.AlignmentFlag.AlignCenter, self.watermark_text)
            painter.restore()

        # 3. Horizontal price gridlines & Y-Axis Scale Labels
        num_y_ticks = 8
        scale_font = QFont(painter.font())
        scale_font.setPointSize(9)
        scale_font.setBold(False)
        painter.setFont(scale_font)

        tick_pen = QPen(QColor("#434958"))
        tick_pen.setWidth(1)

        grid_pen = QPen(QColor(self.config.default_grid_color))
        grid_pen.setStyle(Qt.PenStyle.DotLine)
        grid_pen.setWidth(1)

        for i in range(1, num_y_ticks):
            y = (chart_h / num_y_ticks) * i
            price = y_trans.y_to_price(y)

            # Horizontal dotted gridline
            painter.setPen(grid_pen)
            painter.drawLine(0, int(y), int(chart_w), int(y))

            # Y-Axis small tick
            painter.setPen(tick_pen)
            painter.drawLine(int(chart_w), int(y), int(chart_w + 5), int(y))

            # Y-Axis Price label
            painter.setPen(QColor("#9CA3AF"))
            price_text = f"{price:.2f}"
            painter.drawText(
                QRectF(chart_w + 7, y - 8, Y_AXIS_WIDTH - 9, 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                price_text,
            )

        # 4. Y-Axis Visual Scale Grip Handles ("Anfasser")
        gx = int(chart_w + Y_AXIS_WIDTH / 2.0)
        handle_font = QFont(painter.font())
        handle_font.setPointSize(7)
        handle_font.setBold(True)
        painter.setFont(handle_font)

        # Top Scale Handle Pill (↕ SCALE)
        top_pill = QRectF(gx - 25, 4, 50, 16)
        painter.fillRect(top_pill, QColor("#222634"))
        painter.setPen(QPen(QColor("#454E66"), 1))
        painter.drawRoundedRect(top_pill, 3, 3)
        painter.setPen(QColor("#BAC4E2"))
        painter.drawText(top_pill, Qt.AlignmentFlag.AlignCenter, "↕ SCALE")

        # Bottom Scale Status / Mode Badge (AUTO / MANUAL)
        bot_pill = QRectF(gx - 25, chart_h - 20, 50, 16)
        if self.axis_mode == "auto":
            painter.fillRect(bot_pill, QColor("#14241F"))
            painter.setPen(QPen(QColor("#1E5642"), 1))
            painter.drawRoundedRect(bot_pill, 3, 3)
            painter.setPen(QColor("#00E676"))
            painter.drawText(bot_pill, Qt.AlignmentFlag.AlignCenter, "● AUTO")
        else:
            painter.fillRect(bot_pill, QColor("#2A1E14"))
            painter.setPen(QPen(QColor("#663B19"), 1))
            painter.drawRoundedRect(bot_pill, 3, 3)
            painter.setPen(QColor("#FF9100"))
            painter.drawText(bot_pill, Qt.AlignmentFlag.AlignCenter, "● MANUAL")

        # 5. Vertical time gridlines & X-Axis Date Labels
        visible_bars = x_trans.visible_bars
        step_bars = max(8, int(visible_bars / 7))
        min_idx = math.floor(x_trans.min_visible_bar_index)
        max_idx = math.ceil(x_trans.right_index)

        first_step = (min_idx // step_bars) * step_bars
        for idx in range(first_step, max_idx + 1, step_bars):
            x = x_trans.bar_to_x(idx)
            if 0 <= x <= chart_w:
                # Vertical dotted gridline
                painter.setPen(grid_pen)
                painter.drawLine(int(x), 0, int(x), int(chart_h))

                # X-Axis tick
                painter.setPen(tick_pen)
                painter.drawLine(int(x), int(chart_h), int(x), int(chart_h + 3))

                # Date text
                if self.base_timestamp > 0:
                    t_sec = self.base_timestamp + int(idx * self.bar_duration)
                    try:
                        dt = datetime.fromtimestamp(t_sec, tz=timezone.utc)
                        date_str = dt.strftime("%d. %b")
                    except Exception:
                        date_str = f"{idx}"
                    painter.setPen(QColor("#848E9C"))
                    painter.drawText(
                        QRectF(x - 35, chart_h + 3, 70, 16),
                        Qt.AlignmentFlag.AlignCenter,
                        date_str,
                    )
