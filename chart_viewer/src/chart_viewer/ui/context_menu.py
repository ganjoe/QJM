"""Context menu according to Section 7."""

from __future__ import annotations
from typing import Callable, Optional
from PySide6.QtWidgets import QMenu, QWidget
from PySide6.QtGui import QAction


class ChartContextMenu:
    """Context menu for right-click on chart canvas."""

    @staticmethod
    def show_menu(
        parent: QWidget,
        global_pos,
        hit_annotation_id: Optional[str],
        on_delete_annotation: Optional[Callable[[str], None]],
        on_reset_axes: Optional[Callable[[], None]],
    ) -> None:
        menu = QMenu(parent)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1E222D;
                color: #D1D4DC;
                border: 1px solid #2A2E39;
            }
            QMenu::item:selected {
                background-color: #2962FF;
                color: #FFFFFF;
            }
        """)

        if hit_annotation_id and on_delete_annotation:
            del_action = QAction(f"Delete Annotation ({hit_annotation_id})", menu)
            del_action.triggered.connect(lambda: on_delete_annotation(hit_annotation_id))
            menu.addAction(del_action)
            menu.addSeparator()

        if on_reset_axes:
            reset_action = QAction("Reset Axes (Auto-Fit)", menu)
            reset_action.triggered.connect(on_reset_axes)
            menu.addAction(reset_action)

        menu.exec(global_pos)
