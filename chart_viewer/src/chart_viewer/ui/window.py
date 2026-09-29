"""ChartWindow widget according to Section 8 & 11."""

from __future__ import annotations
from typing import Callable, Optional
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget, QStatusBar
from PySide6.QtCore import QEvent
from PySide6.QtGui import QCloseEvent, QMoveEvent, QResizeEvent, QKeyEvent

from chart_viewer.config import ViewerConfig
from chart_viewer.ui.topbar import TopBarWidget
from chart_viewer.ui.canvas import ChartCanvas
from chart_viewer.ui.color_flag import ColorFlagButton
from chart_viewer.core.state_manager import WindowData


class ChartWindow(QMainWindow):
    """Native desktop window for a single chart view."""

    window_closed_signal = Signal(str)  # window_id
    window_activated = Signal(str)  # window_id, emitted when this window gains focus
    geometry_changed_signal = Signal(str, dict)  # window_id, {x, y, w, h}
    symbol_change_requested = Signal(str, str, object)  # window_id, new_symbol, preset|None
    pane_scale_changed = Signal(str, object)     # window_id, {pane_id: "linear"|"log"}
    flag_changed_signal = Signal(str, int)       # window_id, new_flag
    panel_focus_requested = Signal()             # Ctrl+Shift+P brings the control panel to the front

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
        # Erst mit einem Snapshot (Bars) gilt das Fenster als gefuellt. Ein leeres
        # Fenster darf einen erneuten Klick auf denselben Ticker nicht schlucken.
        self.has_data: bool = False
        self._was_active: bool = False

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
        self.canvas.pane_scale_changed.connect(self._on_pane_scale_changed)
        layout.addWidget(self.canvas, stretch=1)

        # Status Bar
        self.status_bar = QStatusBar(self)
        self.status_bar.setStyleSheet("background-color: #1E222D; color: #758696; font-size: 10px;")
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

    def bind_data(self, win_data: WindowData) -> None:
        self.symbol = win_data.symbol
        self.has_data = bool(getattr(win_data, "bars", None))
        if hasattr(win_data, "color_flag"):
            self.color_flag = win_data.color_flag
            self.flag_btn.set_flag(self.color_flag)

        self.setWindowTitle(f"{win_data.symbol} ({win_data.timeframe.to_string() if win_data.timeframe else ''}) — {self.window_id}")

        self.canvas.set_window_data(win_data)
        # Apply any initial topbar blocks
        for block in win_data.topbar_blocks.values():
            self.topbar.set_block(block)

    def _on_pane_scale_changed(self, pane_id: str, scale_type: str) -> None:
        """Pane LOG/LIN button: report the complete pane map to the app.

        The whole map (not just the clicked pane) is sent so the agent can store
        a consistent state and persist it into the preset in one call.
        """
        self.pane_scale_changed.emit(self.window_id, self.canvas.pane_scales())

    def _on_flag_changed(self, new_flag: int) -> None:
        self.color_flag = new_flag
        self.flag_changed_signal.emit(self.window_id, new_flag)

    def request_symbol_change(self, new_symbol: str, preset: str | None = None) -> None:
        """Called (e.g. by EventHub) when the window should change its symbol.

        `preset` comes from the control panel; when it is set the chart is rebuilt
        even if the symbol did not change (preset switch on the same ticker).

        Ein Fenster ohne Daten fragt immer nach: sonst schluckt der Gleichheits-
        check den zweiten Klick auf denselben Ticker, und ein leer gebliebenes
        Fenster bliebe leer.
        """
        if self.symbol != new_symbol or preset is not None or not self.has_data:
            self.symbol_change_requested.emit(self.window_id, new_symbol, preset)

    def changeEvent(self, event: QEvent) -> None:
        """Forward activation changes to the canvas (idle value-box readout).

        While this window is not the active one (the user works in the Control
        Panel or another application) there is no cursor tracking the chart, so
        the canvas drops the crosshair and the panes show the newest value of
        every curve.
        """
        if event.type() in (
            QEvent.Type.ActivationChange,
            QEvent.Type.WindowActivate,
            QEvent.Type.WindowDeactivate,
        ):
            active = self.isActiveWindow()
            canvas = getattr(self, "canvas", None)
            if canvas is not None:
                canvas.set_window_active(active)
            # Focus gained: report it (the "sticky" chart window is owned by the
            # control panel; this signal only announces the event).
            if active and not self._was_active:
                self.window_activated.emit(self.window_id)
            self._was_active = active
        super().changeEvent(event)

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

    def report_geometry(self) -> None:
        """Melde die aktuelle Fenstergeometrie an den Agenten.

        Die Geometrie gehoert dem Client: der Agent merkt sich nur, was hier
        gemeldet wird. Nach dem Oeffnen/Platzieren eines Fensters wird deshalb
        einmal gemeldet, damit der Ledger nicht auf Startwerten stehen bleibt.
        """
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
        if (
            event.key() == Qt.Key.Key_P
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.panel_focus_requested.emit()
            event.accept()
            return
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
