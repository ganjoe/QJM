"""Color Flag Widget for routing between Watchlists and Charts."""

from typing import Dict
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QPushButton


class ColorFlagButton(QPushButton):
    """A button that cycles through 4 colors to group windows together."""

    flag_changed = Signal(int)  # Emits the new flag index (0-3)

    COLORS: Dict[int, str] = {
        0: "#EF5350",  # Red
        1: "#66BB6A",  # Green
        2: "#42A5F5",  # Blue
        3: "#FFCA28",  # Yellow
    }

    def __init__(self, initial_flag: int = 0, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        # Using built-in Qt cursor
        from PySide6.QtCore import Qt
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to change color flag group")
        
        self._current_flag = initial_flag
        self._update_style()

        self.clicked.connect(self._cycle_flag)

    def get_flag(self) -> int:
        return self._current_flag

    def set_flag(self, flag: int):
        if flag in self.COLORS and flag != self._current_flag:
            self._current_flag = flag
            self._update_style()
            self.flag_changed.emit(self._current_flag)

    def _cycle_flag(self):
        self._current_flag = (self._current_flag + 1) % 4
        self._update_style()
        self.flag_changed.emit(self._current_flag)

    def _update_style(self):
        color = self.COLORS.get(self._current_flag, "#EF5350")
        self.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {color};
                border: 1px solid #222;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                border: 1px solid #FFF;
            }}
            """
        )
