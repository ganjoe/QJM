"""Layer 1: Background rendering (Grid, Watermark, Session separators)."""

from __future__ import annotations
import math
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QFont
from chart_viewer.ui.layers.base import ChartLayer
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.coords.y_axis import YAxisTransform
from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig


class BackgroundLayer(ChartLayer):
    """Renders background, gridlines, and symbol watermark."""

    def __init__(self, watermark_text: str = "", config: ViewerConfig | None = None):
        self.watermark_text = watermark_text
        self.config = config or GLOBAL_CONFIG

    def set_watermark(self, text: str) -> None:
        self.watermark_text = text

    def render(
        self,
        painter: QPainter,
        x_trans: XAxisTransform,
        y_trans: YAxisTransform,
        width: int,
        height: int,
    ) -> None:
        # Fill background
        bg_color = QColor(self.config.default_background_color)
        painter.fillRect(0, 0, width, height, bg_color)

        # Watermark
        if self.watermark_text:
            painter.save()
            watermark_color = QColor(self.config.default_text_color)
            watermark_color.setAlpha(20)  # Very subtle
            painter.setPen(watermark_color)
            font = painter.font()
            font.setPointSize(48)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(0, 0, width, height), Qt.AlignmentFlag.AlignCenter, self.watermark_text)
            painter.restore()

        # Gridlines
        grid_pen = QPen(QColor(self.config.default_grid_color))
        grid_pen.setStyle(Qt.PenStyle.DotLine)
        grid_pen.setWidth(1)
        painter.setPen(grid_pen)

        # Horizontal price gridlines (~6-8 lines)
        num_y_ticks = 8
        for i in range(1, num_y_ticks):
            y = (height / num_y_ticks) * i
            painter.drawLine(0, int(y), width, int(y))

        # Vertical time gridlines
        visible_bars = x_trans.visible_bars
        step_bars = max(10, int(visible_bars / 8))
        min_idx = math.floor(x_trans.min_visible_bar_index)
        max_idx = math.ceil(x_trans.right_index)

        # Align step
        first_step = (min_idx // step_bars) * step_bars
        for idx in range(first_step, max_idx + 1, step_bars):
            x = x_trans.bar_to_x(idx)
            if 0 <= x <= width:
                painter.drawLine(int(x), 0, int(x), height)
