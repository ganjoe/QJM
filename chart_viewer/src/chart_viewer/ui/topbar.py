"""Top-Bar metadata grid according to Section 6.3."""

from __future__ import annotations
import html
from typing import Dict
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget, QGridLayout, QLabel, QSizePolicy
from chart_viewer.models.entities import TopBarBlock


class TopBarWidget(QWidget):
    """Freeform grid widget displaying sanitized metadata and status blocks with optional TTL."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.layout = QGridLayout(self)
        self.layout.setContentsMargins(8, 4, 8, 4)
        self.layout.setSpacing(12)
        self._blocks: Dict[str, QLabel] = {}

        # Default dark styling
        self.setStyleSheet("""
            QWidget {
                background-color: #1E222D;
                border-bottom: 1px solid #2A2E39;
            }
            QLabel {
                color: #D1D4DC;
                font-size: 11px;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            }
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
            label = QLabel(self)
            label.setText(sanitized_text)
            self.layout.addWidget(label, row, col)
            self._blocks[block_id] = label

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
