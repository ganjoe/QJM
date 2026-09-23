"""Farben und Stil. Dunkel, dicht, ohne Verzierung — ein Arbeitsfenster.

Zustaende haben feste Farben. Sie sind die einzige Stelle, an der Farbe
Bedeutung traegt; alles andere bleibt neutral, damit ein Blick auf die Liste
genuegt.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase

BG = "#14161a"
PANEL = "#1b1e24"
PANEL_ALT = "#20242c"
BORDER = "#2b313b"
TEXT = "#d7dce3"
MUTED = "#8b93a1"
ACCENT = "#4c8dff"

STATE_COLORS = {
    "running": "#58a6ff",
    "scheduled": "#d29922",
    "done": "#3fb950",
    "failed": "#f85149",
    "cancelled": "#6e7681",
    "silent": "#6e7681",
    "pending": "#d29922",
    "skipped": "#6e7681",
}

STATE_LABELS = {
    "running": "laeuft",
    "scheduled": "wartet",
    "done": "fertig",
    "failed": "gescheitert",
    "cancelled": "abgeschaltet",
    "silent": "still",
    "pending": "wartend",
    "skipped": "uebersprungen",
}

ITEM_TYPE_LABELS = {
    "initial": "Planung",
    "task": "Aufgabe",
    "review": "Review",
    "template": "Vorlage",
}


def state_color(state: str) -> QColor:
    return QColor(STATE_COLORS.get(state, MUTED))


def state_label(state: str) -> str:
    return STATE_LABELS.get(state, state)


def mono_font(size: int = 13) -> QFont:
    for family in ("JetBrains Mono", "DejaVu Sans Mono", "Ubuntu Mono", "Monospace"):
        if family in QFontDatabase.families():
            font = QFont(family, size)
            font.setStyleHint(QFont.StyleHint.Monospace)
            return font
    font = QFont()
    font.setFamily("monospace")
    font.setPointSize(size)
    return font


STYLESHEET = f"""
/* Schriftgroesse: 16px statt 12px (+33 %). Zeilenhoehen ergeben sich daraus;
   die Spaltenbreiten in window.py sind entsprechend mitgewachsen. */
QWidget {{ background: {BG}; color: {TEXT}; font-size: 16px; }}
QMainWindow::separator {{ background: {BORDER}; width: 1px; height: 1px; }}

QToolBar {{ background: {PANEL}; border: none; border-bottom: 1px solid {BORDER};
            padding: 4px 6px; spacing: 6px; }}
QToolBar QToolButton {{ padding: 5px 12px; border: 1px solid {BORDER}; border-radius: 3px;
                        background: {PANEL_ALT}; }}
QToolBar QToolButton:hover {{ border-color: {ACCENT}; }}
QToolBar QToolButton:disabled {{ color: {MUTED}; border-color: {BORDER}; background: {PANEL}; }}

QLineEdit, QComboBox, QPlainTextEdit, QSpinBox, QDateTimeEdit, QTextEdit {{
    background: {PANEL_ALT}; border: 1px solid {BORDER}; border-radius: 3px;
    padding: 4px 6px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {PANEL_ALT}; border: 1px solid {BORDER};
                               selection-background-color: {ACCENT}; }}

QTreeView, QTableView {{ background: {PANEL}; alternate-background-color: {PANEL_ALT};
    border: none; gridline-color: {BORDER}; selection-background-color: #24344f;
    selection-color: {TEXT}; outline: none; }}
QTreeView::item, QTableView::item {{ padding: 3px 4px; border: none; }}
QHeaderView::section {{ background: {PANEL_ALT}; color: {MUTED}; padding: 5px 6px;
    border: none; border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER}; }}

QSplitter::handle {{ background: {BORDER}; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QStatusBar::item {{ border: none; }}
QLabel#title {{ font-size: 19px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; }}
QTabBar::tab {{ background: {PANEL}; padding: 6px 14px; border: 1px solid {BORDER};
                border-bottom: none; }}
QTabBar::tab:selected {{ background: {PANEL_ALT}; color: {TEXT}; }}
QPushButton {{ background: {PANEL_ALT}; border: 1px solid {BORDER}; border-radius: 3px;
               padding: 6px 14px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:default {{ border-color: {ACCENT}; }}
QDialog {{ background: {BG}; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #333a45; min-height: 24px; border-radius: 5px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: {BG}; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #333a45; min-width: 24px; border-radius: 5px; }}
QCheckBox {{ spacing: 6px; }}
"""
