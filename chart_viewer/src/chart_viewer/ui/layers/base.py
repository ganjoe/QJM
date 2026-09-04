"""Base class for chart rendering layers."""

from __future__ import annotations
from abc import ABC, abstractmethod
from PySide6.QtGui import QPainter
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.coords.y_axis import YAxisTransform


class ChartLayer(ABC):
    """Abstract rendering layer."""

    @abstractmethod
    def render(
        self,
        painter: QPainter,
        x_trans: XAxisTransform,
        y_trans: YAxisTransform,
        width: int,
        height: int,
    ) -> None:
        pass
