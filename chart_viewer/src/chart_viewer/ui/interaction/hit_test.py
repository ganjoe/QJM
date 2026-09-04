"""Hit-test algorithm according to Section 7."""

from __future__ import annotations
import math
from typing import Dict, Optional, Tuple
from PySide6.QtCore import QPointF
from chart_viewer.models.entities import Annotation, Anchor
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.coords.y_axis import YAxisTransform


def point_to_line_distance(p: QPointF, p1: QPointF, p2: QPointF) -> float:
    """Compute shortest distance from point p to line segment (p1, p2)."""
    dx = p2.x() - p1.x()
    dy = p2.y() - p1.y()
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(p.x() - p1.x(), p.y() - p1.y())

    # Project point onto line segment, clamped between 0 and 1
    t = max(0.0, min(1.0, ((p.x() - p1.x()) * dx + (p.y() - p1.y()) * dy) / length_sq))
    proj_x = p1.x() + t * dx
    proj_y = p1.y() + t * dy
    return math.hypot(p.x() - proj_x, p.y() - proj_y)


def anchor_to_pos(
    anchor: Anchor,
    x_trans: XAxisTransform,
    y_trans: YAxisTransform,
    base_timestamp: int,
    bar_duration: int,
) -> QPointF:
    if anchor.mode == "pixel":
        return QPointF(anchor.x_px or 0.0, anchor.y_px or 0.0)
    else:
        if anchor.t is not None and bar_duration > 0:
            bar_idx = (anchor.t - base_timestamp) / bar_duration
            x = x_trans.bar_to_x(bar_idx)
        else:
            x = anchor.x_px or 0.0

        if anchor.price is not None:
            y = y_trans.price_to_y(anchor.price)
        else:
            y = anchor.y_px or 0.0
        return QPointF(x, y)


def hit_test_annotations(
    pos: QPointF,
    annotations: Dict[str, Annotation],
    x_trans: XAxisTransform,
    y_trans: YAxisTransform,
    base_timestamp: int,
    bar_duration: int,
    radius: float = 6.0,
) -> Tuple[Optional[str], Optional[int]]:
    """Perform hit-test with priority:

    1. Annotation handles
    2. Annotation lines
    Returns (annotation_id, anchor_index) or (None, None).
    """
    # 1. Priority 1: Handles
    for ann_id, ann in annotations.items():
        for idx, anchor in enumerate(ann.anchors):
            anchor_p = anchor_to_pos(anchor, x_trans, y_trans, base_timestamp, bar_duration)
            dist = math.hypot(pos.x() - anchor_p.x(), pos.y() - anchor_p.y())
            if dist <= radius:
                return ann_id, idx

    # 2. Priority 2: Lines/Edges
    for ann_id, ann in annotations.items():
        if ann.type == "hline" and ann.anchors:
            price = ann.anchors[0].price or 0.0
            y = y_trans.price_to_y(price)
            if abs(pos.y() - y) <= radius:
                return ann_id, None

        elif ann.type == "trendline" and len(ann.anchors) >= 2:
            p1 = anchor_to_pos(ann.anchors[0], x_trans, y_trans, base_timestamp, bar_duration)
            p2 = anchor_to_pos(ann.anchors[1], x_trans, y_trans, base_timestamp, bar_duration)
            if point_to_line_distance(pos, p1, p2) <= radius:
                return ann_id, None

        elif ann.type == "rect" and len(ann.anchors) >= 2:
            p1 = anchor_to_pos(ann.anchors[0], x_trans, y_trans, base_timestamp, bar_duration)
            p2 = anchor_to_pos(ann.anchors[1], x_trans, y_trans, base_timestamp, bar_duration)
            # Rect edges
            rx1, rx2 = min(p1.x(), p2.x()), max(p1.x(), p2.x())
            ry1, ry2 = min(p1.y(), p2.y()), max(p1.y(), p2.y())
            edges = [
                (QPointF(rx1, ry1), QPointF(rx2, ry1)),
                (QPointF(rx2, ry1), QPointF(rx2, ry2)),
                (QPointF(rx2, ry2), QPointF(rx1, ry2)),
                (QPointF(rx1, ry2), QPointF(rx1, ry1)),
            ]
            for ep1, ep2 in edges:
                if point_to_line_distance(pos, ep1, ep2) <= radius:
                    return ann_id, None

    # Empty chart area hit
    return None, None
