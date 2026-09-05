"""ChartWindow widget according to Section 8 & 11."""

from __future__ import annotations
from typing import Callable, Optional
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget, QStatusBar
from PySide6.QtGui import QCloseEvent, QMoveEvent, QResizeEvent, QKeyEvent

from chart_viewer.config import ViewerConfig
from chart_viewer.ui.topbar import TopBarWidget
from chart_viewer.ui.canvas import ChartCanvas
from chart_viewer.ui.color_flag import ColorFlagButton
from chart_viewer.core.state_manager import WindowData


class ChartWindow(QMainWindow):
    """Native desktop window for a single chart view."""

    window_closed_signal = Signal(str)  # window_id
    geometry_changed_signal = Signal(str, dict)  # window_id, {x, y, w, h}
    symbol_change_requested = Signal(str, str)   # window_id, new_symbol
    flag_changed_signal = Signal(str, int)       # window_id, new_flag

    def __init__(
        self,
        window_id: str,
        config: ViewerConfig,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.window_id = window_id
        self.config = config
        self.symbol: str = ""
        self.color_flag: int = 0

        self.setWindowTitle(f"Chart Viewer — {window_id}")
        self.resize(1000, 700)

        # Central widget and layout
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # TopBar Layout (Color Flag + TopBarGrid)
        from PySide6.QtWidgets import QHBoxLayout
        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(4, 4, 4, 4)
        top_layout.setSpacing(8)

        self.flag_btn = ColorFlagButton(initial_flag=self.color_flag, parent=self)
        self.flag_btn.flag_changed.connect(self._on_flag_changed)
        top_layout.addWidget(self.flag_btn)

        self.topbar = TopBarWidget(self)
        top_layout.addWidget(self.topbar, stretch=1)
        
        layout.addLayout(top_layout, stretch=0)

        # Main Canvas
        self.canvas = ChartCanvas(window_id=window_id, config=self.config, parent=self)
        layout.addWidget(self.canvas, stretch=1)

        # Status Bar
        self.status_bar = QStatusBar(self)
        self.status_bar.setStyleSheet("background-color: #1E222D; color: #758696; font-size: 10px;")
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

    def bind_data(self, win_data: WindowData) -> None:
        self.symbol = win_data.symbol
        if hasattr(win_data, "color_flag"):
            self.color_flag = win_data.color_flag
            self.flag_btn.set_flag(self.color_flag)

        self.setWindowTitle(f"{win_data.symbol} ({win_data.timeframe.to_string() if win_data.timeframe else ''}) — {self.window_id}")

        self.canvas.set_window_data(win_data)
        # Apply any initial topbar blocks
        for block in win_data.topbar_blocks.values():
            self.topbar.set_block(block)

    def _on_flag_changed(self, new_flag: int) -> None:
        self.color_flag = new_flag
        self.flag_changed_signal.emit(self.window_id, new_flag)

    def request_symbol_change(self, new_symbol: str) -> None:
        """Called (e.g. by EventHub) when the window should change its symbol."""
        if self.symbol != new_symbol:
            self.symbol_change_requested.emit(self.window_id, new_symbol)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Informative fire-and-forget window.closed event to agent (Section 8)."""
        self.window_closed_signal.emit(self.window_id)
        super().closeEvent(event)

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        self._emit_geometry()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._emit_geometry()

    def _emit_geometry(self) -> None:
        geom = {
            "x": self.x(),
            "y": self.y(),
            "width": self.width(),
            "height": self.height(),
        }
        self.geometry_changed_signal.emit(self.window_id, geom)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Escape):
            self.canvas.keyPressEvent(event)
            if event.isAccepted():
                return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.canvas.keyReleaseEvent(event)
            if event.isAccepted():
                return
        super().keyReleaseEvent(event)
