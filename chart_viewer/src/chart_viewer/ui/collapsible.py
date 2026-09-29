"""Aufklappbarer Bereich fuer das Control-Panel.

Ein Kopf (Pfeil + Titel, rechts optionale Zusatz-Widgets) und ein Inhalt, der sich
ein- und ausklappen laesst. Der Zustand wird vom Besitzer gespeichert (QSettings);
das Widget selbst meldet nur Aenderungen ueber `toggled`.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QToolButton, QVBoxLayout, QWidget


class CollapsibleSection(QWidget):
    """Section with a clickable header that shows or hides its content."""

    toggled = Signal(bool)  # expanded

    def __init__(self, title: str, parent: Optional[QWidget] = None, expanded: bool = True) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.header_button = QToolButton(self)
        self.header_button.setObjectName("sectionHeader")
        self.header_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header_button.setCheckable(True)
        self.header_button.setAutoRaise(True)
        self.header_button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.header_button.toggled.connect(self._on_toggled)
        header.addWidget(self.header_button)
        header.addStretch(1)
        self._header_layout = header
        outer.addLayout(header)

        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        # Leicht eingerueckt: der Inhalt gehoert sichtbar zum Kopf darueber.
        self.content_layout.setContentsMargins(10, 0, 0, 2)
        self.content_layout.setSpacing(6)
        outer.addWidget(self.content, 1)

        self._title = title
        self.header_button.setChecked(expanded)
        self._apply(expanded)

    def add_header_widget(self, widget: QWidget) -> None:
        """Place a widget at the right end of the header row (e.g. a toggle)."""
        self._header_layout.addWidget(widget)

    def set_title(self, title: str) -> None:
        self._title = title
        self._update_text()

    def title(self) -> str:
        return self._title

    def is_expanded(self) -> bool:
        return self.header_button.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        self.header_button.setChecked(bool(expanded))

    def _on_toggled(self, expanded: bool) -> None:
        self._apply(expanded)
        self.toggled.emit(expanded)

    def _apply(self, expanded: bool) -> None:
        self.content.setVisible(expanded)
        self.header_button.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._update_text()

    def _update_text(self) -> None:
        self.header_button.setText(self._title)
