"""Top-Bar metadata grid according to Section 6.3."""

from __future__ import annotations
import html
from typing import Dict
from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtWidgets import QWidget, QGridLayout, QLabel, QSizePolicy
from chart_viewer.models.entities import TopBarBlock


class _ClippingLabel(QLabel):
    """Label, das den Text bei zu wenig Platz beschneidet statt zu blockieren.

    Ein normales QLabel meldet ohne Wortumbruch seine volle Textbreite als
    minimumSizeHint. Lange Info-Zeilen erzwangen damit eine Fenster-
    Mindestbreite von rund 1000 px - das Fenster liess sich nicht mehr
    verkleinern. Hier bleibt die bevorzugte Breite (sizeHint) unveraendert,
    nur die Mindestbreite faellt auf 0: bei genug Platz steht der Text
    vollstaendig da, bei knappem Platz wird er einfach verdeckt.
    """

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())


class TopBarWidget(QWidget):
    """Freeform grid widget displaying sanitized metadata and status blocks with optional TTL."""

    # Info-Zeile: kompakter 13-px-Font (vorher 22 px). Die feste Hoehe waechst
    # im selben Verhaeltnis mit, sonst schneidet das Label den Text vertikal ab.
    FONT_SIZE_PX = 13
    ROW_HEIGHT_PX = 40

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(self.ROW_HEIGHT_PX)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.layout = QGridLayout(self)
        self.layout.setContentsMargins(8, 8, 8, 8)
        self.layout.setSpacing(12)
        self._blocks: Dict[str, QLabel] = {}

        # Default dark styling
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #1E222D;
                border-bottom: 1px solid #2A2E39;
            }}
            QLabel {{
                color: #D1D4DC;
                font-size: {self.FONT_SIZE_PX}px;
                font-weight: bold;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            }}
        """)

    def set_block(self, block: TopBarBlock | dict) -> None:
        """Add or update a grid block with sanitized text."""
        if isinstance(block, dict):
            block_id = block["block_id"]
            pos = block["position"]
            raw_content = block["content"]
            ttl_ms = block.get("ttl_ms")
        else:
            block_id = block.block_id
            pos = block.position
            raw_content = block.content
            ttl_ms = block.ttl_ms

        row = pos.get("row", 0)
        col = pos.get("col", 0)

        # Sanitize text to prevent any external HTML injection (Section 6.3)
        sanitized_text = html.escape(str(raw_content))

        if block_id in self._blocks:
            label = self._blocks[block_id]
            label.setText(sanitized_text)
        else:
            label = _ClippingLabel(self)
            # Shrinkable: Text wird bei knappem Platz beschnitten statt das
            # Fenster in die Breite zu zwingen (siehe _ClippingLabel).
            label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            label.setMinimumWidth(0)
            label.setText(sanitized_text)
            self.layout.addWidget(label, row, col)
            self._blocks[block_id] = label

        # Voller Text bleibt per Tooltip erreichbar, wenn die Zeile beschnitten ist.
        label.setToolTip(sanitized_text)

        # TTL auto-expiration handling
        if ttl_ms and ttl_ms > 0:
            QTimer.singleShot(ttl_ms, lambda: self.remove_block(block_id))

    def remove_block(self, block_id: str) -> None:
        if block_id in self._blocks:
            label = self._blocks.pop(block_id)
            self.layout.removeWidget(label)
            label.deleteLater()

    def clear(self) -> None:
        for block_id in list(self._blocks.keys()):
            self.remove_block(block_id)
