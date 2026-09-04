"""Layer 3: Persistent annotations rendering (HLine, Trendline, Rect, Text, Trade Marker)."""

from __future__ import annotations
from typing import Dict, List, Optional
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QPolygonF
from chart_viewer.ui.layers.base import ChartLayer
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.coords.y_axis import YAxisTransform
from chart_viewer.models.entities import Annotation, Anchor
from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig


class AnnotationsLayer(ChartLayer):
    """Renders persistent drawing objects on Layer 3."""

    def __init__(self, config: ViewerConfig | None = None):
        self.config = config or GLOBAL_CONFIG
        self.annotations: Dict[str, Annotation] = {}
        self.selected_annotation_id: Optional[str] = None
        self.bar_duration: int = 86400
        self.base_timestamp: int = 0

    def set_annotations(
        self,
        annotations: Dict[str, Annotation],
        base_timestamp: int = 0,
        bar_duration: int = 86400,
    ) -> None:
        self.annotations = annotations
        self.base_timestamp = base_timestamp
        self.bar_duration = max(1, bar_duration)

    def _anchor_to_pos(
        self, anchor: Anchor | dict, x_trans: XAxisTransform, y_trans: YAxisTransform
    ) -> QPointF:
        mode = anchor.get("mode", "data") if isinstance(anchor, dict) else anchor.mode
        t_val = anchor.get("t") if isinstance(anchor, dict) else anchor.t
        price_val = anchor.get("price") if isinstance(anchor, dict) else anchor.price
        x_px_val = anchor.get("x_px") if isinstance(anchor, dict) else anchor.x_px
        y_px_val = anchor.get("y_px") if isinstance(anchor, dict) else anchor.y_px

        if mode == "pixel":
            return QPointF(x_px_val or 0.0, y_px_val or 0.0)
        else:
            # Data mode
            if t_val is not None and self.bar_duration > 0:
                bar_idx = (t_val - self.base_timestamp) / self.bar_duration
                x = x_trans.bar_to_x(bar_idx)
            else:
                x = x_px_val or 0.0

            if price_val is not None:
                y = y_trans.price_to_y(price_val)
            else:
                y = y_px_val or 0.0
            return QPointF(x, y)

    def render(
        self,
        painter: QPainter,
        x_trans: XAxisTransform,
        y_trans: YAxisTransform,
        width: int,
        height: int,
    ) -> None:
        for ann_id, ann in self.annotations.items():
            is_selected = (ann_id == self.selected_annotation_id)
            self._render_single(painter, ann, x_trans, y_trans, width, height, is_selected)

    def _render_single(
        self,
        painter: QPainter,
        ann: Annotation,
        x_trans: XAxisTransform,
        y_trans: YAxisTransform,
        width: int,
        height: int,
        is_selected: bool,
    ) -> None:
        style = ann.style
        color = QColor(style.get("color", "#FF9800"))
        line_w = style.get("width", 2)

        pen = QPen(color)
        pen.setWidth(line_w)
        painter.setPen(pen)

        if ann.type == "hline":
            # Horizontal line across full canvas at price
            if ann.anchors:
                a0 = ann.anchors[0]
                price = a0.get("price", 0.0) if isinstance(a0, dict) else (a0.price or 0.0)
            else:
                price = 0.0
            y = y_trans.price_to_y(price)
            painter.drawLine(0, int(y), width, int(y))

        elif ann.type == "trendline" and len(ann.anchors) >= 2:
            p1 = self._anchor_to_pos(ann.anchors[0], x_trans, y_trans)
            p2 = self._anchor_to_pos(ann.anchors[1], x_trans, y_trans)
            painter.drawLine(p1, p2)

        elif ann.type == "rect" and len(ann.anchors) >= 2:
            p1 = self._anchor_to_pos(ann.anchors[0], x_trans, y_trans)
            p2 = self._anchor_to_pos(ann.anchors[1], x_trans, y_trans)
            rx = min(p1.x(), p2.x())
            ry = min(p1.y(), p2.y())
            rw = abs(p2.x() - p1.x())
            rh = abs(p2.y() - p1.y())
            rect = QRectF(rx, ry, rw, rh)

            fill_color = QColor(color)
            fill_color.setAlpha(style.get("alpha", 30))
            painter.setBrush(QBrush(fill_color))
            painter.drawRect(rect)

        elif ann.type == "text" and ann.anchors:
            p = self._anchor_to_pos(ann.anchors[0], x_trans, y_trans)
            text = style.get("text", "")
            painter.drawText(int(p.x()), int(p.y()), text)

        elif ann.type == "trade_marker" and ann.anchors:
            # Buy or Sell marker
            p = self._anchor_to_pos(ann.anchors[0], x_trans, y_trans)
            action = style.get("action", "BUY").upper()
            marker_color = QColor("#00E676" if action == "BUY" else "#FF5252")
            painter.setBrush(QBrush(marker_color))
            painter.setPen(QPen(marker_color))

            # Draw triangle pointing up (BUY) or down (SELL)
            size = 8.0
            poly = QPolygonF()
            if action == "BUY":
                poly.append(QPointF(p.x(), p.y() - size))
                poly.append(QPointF(p.x() - size, p.y() + size))
                poly.append(QPointF(p.x() + size, p.y() + size))
            else:
                poly.append(QPointF(p.x(), p.y() + size))
                poly.append(QPointF(p.x() - size, p.y() - size))
                poly.append(QPointF(p.x() + size, p.y() - size))
            painter.drawPolygon(poly)

        # Draw handles if selected
        if is_selected:
            handle_color = QColor("#FFFFFF")
            painter.setBrush(QBrush(handle_color))
            painter.setPen(QPen(QColor("#000000"), 1))
            radius = self.config.hit_test_radius_px
            for anchor in ann.anchors:
                pos = self._anchor_to_pos(anchor, x_trans, y_trans)
                painter.drawEllipse(pos, radius, radius)
