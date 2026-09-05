"""Watchlist Window widget."""

from typing import Dict, Any
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, 
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView
)
from PySide6.QtGui import QCloseEvent, QMoveEvent, QResizeEvent

from chart_viewer.models.entities import WatchlistState
from chart_viewer.ui.color_flag import ColorFlagButton

class WatchlistWindow(QMainWindow):
    window_closed_signal = Signal(str)
    geometry_changed_signal = Signal(str, dict)
    row_selected_signal = Signal(str, str, int)  # window_id, symbol, color_flag
    flag_changed_signal = Signal(str, int)       # window_id, new_flag

    def __init__(self, window_id: str, parent=None):
        super().__init__(parent)
        self.window_id = window_id
        self.color_flag: int = 0
        
        self.setWindowTitle(f"Watchlist — {window_id}")
        self.resize(600, 400)
        
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Toolbar Layout
        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(4, 4, 4, 4)
        top_layout.setSpacing(8)
        
        self.flag_btn = ColorFlagButton(initial_flag=self.color_flag, parent=self)
        self.flag_btn.flag_changed.connect(self._on_flag_changed)
        top_layout.addWidget(self.flag_btn)
        top_layout.addStretch()
        
        layout.addLayout(top_layout)
        
        # Table
        self.table = QTableWidget(self)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        
        # Dark theme stylesheet matching ChartViewer
        self.table.setStyleSheet(
            """
            QTableWidget {
                background-color: #131722;
                color: #D1D4DC;
                gridline-color: #2A2E39;
                border: none;
            }
            QHeaderView::section {
                background-color: #1E222D;
                color: #787B86;
                padding: 4px;
                border: 1px solid #2A2E39;
                font-weight: bold;
            }
            QTableWidget::item:selected {
                background-color: #2A2E39;
            }
            """
        )
        
        self.table.cellClicked.connect(self._on_cell_clicked)
        layout.addWidget(self.table)

    def set_data(self, wl_state: WatchlistState):
        self.setWindowTitle(f"{wl_state.display_name} — {self.window_id}")
        
        self.color_flag = wl_state.color_flag
        self.flag_btn.set_flag(self.color_flag)
        
        self.table.setSortingEnabled(False)
        self.table.clear()
        
        columns = wl_state.columns
        if not columns:
            return
            
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        
        self.table.setRowCount(len(wl_state.rows))
        for row_idx, row_data in enumerate(wl_state.rows):
            for col_idx, col_name in enumerate(columns):
                val = row_data.cells.get(col_name)
                if val is None and col_name.lower() == 'symbol':
                    val = row_data.symbol
                if val is None:
                    val = ""
                    
                item = QTableWidgetItem()
                
                # To support proper sorting for numbers
                if isinstance(val, (int, float)):
                    item.setData(Qt.ItemDataRole.EditRole, val)
                else:
                    item.setText(str(val))
                    
                if col_name.lower() == 'symbol':
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                
                self.table.setItem(row_idx, col_idx, item)
                
        self.table.resizeColumnsToContents()
        
        # Restore sorting if requested
        if wl_state.sort_column and wl_state.sort_column in columns:
            col_idx = columns.index(wl_state.sort_column)
            order = Qt.SortOrder.AscendingOrder if wl_state.sort_ascending else Qt.SortOrder.DescendingOrder
            self.table.sortItems(col_idx, order)
            
        self.table.setSortingEnabled(True)
        
    def _on_flag_changed(self, new_flag: int):
        self.color_flag = new_flag
        self.flag_changed_signal.emit(self.window_id, new_flag)
        
    def _on_cell_clicked(self, row: int, col: int):
        headers = [self.table.horizontalHeaderItem(i).text().lower() for i in range(self.table.columnCount())]
        try:
            symbol_col = headers.index("symbol")
            symbol_item = self.table.item(row, symbol_col)
            if symbol_item:
                symbol = symbol_item.text()
                self.row_selected_signal.emit(self.window_id, symbol, self.color_flag)
        except ValueError:
            pass # No symbol column found

    def closeEvent(self, event: QCloseEvent) -> None:
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
