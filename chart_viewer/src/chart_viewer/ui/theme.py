"""Application-wide dark theme (palette + stylesheet) for the chart viewer.

Why this module exists
----------------------
The dark look used to live only in *widget* stylesheets (the control panel sets
``PANEL_STYLESHEET`` on itself, the watchlist table styles itself, ...). A widget
stylesheet is inherited by children of that widget only. Every top-level popup
that is not a child of a styled widget - ``QDialog``/``QMessageBox`` created with
``parent=None``, completer popups, context menus - therefore fell back to the
platform default palette: light window background with the dark-theme label
colour, i.e. grey text on white and unreadable.

The fix is to style the *application*, not the widget: ``apply_dark_theme(app)``
installs the dark palette plus one app-level stylesheet, so every window, dialog
and popup of the viewer renders dark, no matter who opens it or which parent it
gets. Individual widgets may keep their local stylesheets - a widget-level sheet
still wins over the application one.

Colours are the TradingView-ish palette already used across the viewer:

    #131722  window / panel background
    #1E222D  inputs, buttons, toolbars
    #2A2E39  borders, selected rows
    #D1D4DC  primary text
    #787B86  section captions
    #758696  dimmed hint / status text
    #42A5F5  focus accent
    #EF5350  error text
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# ── palette ────────────────────────────────────────────────────────────────────

BG_WINDOW = "#131722"
BG_SURFACE = "#1E222D"
BORDER = "#2A2E39"
TEXT = "#D1D4DC"
TEXT_DIM = "#787B86"
TEXT_MUTED = "#758696"
ACCENT = "#42A5F5"
ERROR = "#EF5350"
DISABLED_TEXT = "#4A4E59"
DISABLED_BORDER = "#22262F"

# ── stylesheet ─────────────────────────────────────────────────────────────────
# Dialog/popup rules live here (they belong to the application); the control panel
# keeps its own widget stylesheet for its specialised selectors (see
# ui/control_panel.py: PANEL_STYLESHEET).
DIALOG_STYLESHEET = """
QDialog, QMessageBox, QInputDialog {
    background-color: #131722;
}
QLabel {
    color: #D1D4DC;
    font-size: 11px;
}
QLabel#dialogHint {
    color: #758696;
    font-size: 10px;
}
QLabel#dialogHint[error="true"] {
    color: #EF5350;
}
QMessageBox QLabel {
    color: #D1D4DC;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background-color: #1E222D;
    color: #D1D4DC;
    border: 1px solid #2A2E39;
    border-radius: 3px;
    padding: 3px 6px;
    font-size: 12px;
    selection-background-color: #2A2E39;
    selection-color: #D1D4DC;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #42A5F5;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: #4A4E59;
}
QComboBox QAbstractItemView {
    background-color: #1E222D;
    color: #D1D4DC;
    selection-background-color: #2A2E39;
    selection-color: #D1D4DC;
    border: 1px solid #2A2E39;
}
QPushButton {
    background-color: #1E222D;
    color: #D1D4DC;
    border: 1px solid #2A2E39;
    border-radius: 3px;
    padding: 4px 8px;
    font-size: 11px;
}
QPushButton:hover {
    border: 1px solid #42A5F5;
}
QPushButton:disabled {
    color: #4A4E59;
    border: 1px solid #22262F;
}
QMenu {
    background-color: #1E222D;
    color: #D1D4DC;
    border: 1px solid #2A2E39;
}
QMenu::item:selected {
    background-color: #2A2E39;
}
QMenu::separator {
    height: 1px;
    background-color: #2A2E39;
    margin: 4px 6px;
}
QToolTip {
    background-color: #1E222D;
    color: #D1D4DC;
    border: 1px solid #2A2E39;
}
QListWidget, QListView, QTreeView, QTableView, QTableWidget {
    background-color: #131722;
    color: #D1D4DC;
    border: 1px solid #2A2E39;
    selection-background-color: #2A2E39;
    selection-color: #D1D4DC;
}
QAbstractItemView::item:hover {
    background-color: #1E222D;
}
QAbstractItemView::item:selected {
    background-color: #2A2E39;
    color: #D1D4DC;
}
QScrollBar:vertical {
    background-color: #131722;
    width: 12px;
    border: none;
}
QScrollBar:horizontal {
    background-color: #131722;
    height: 12px;
    border: none;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background-color: #363A45;
    border-radius: 4px;
}
QScrollBar::handle:vertical {
    min-height: 20px;
}
QScrollBar::handle:horizontal {
    min-width: 20px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
    background-color: #4A4E59;
}
QScrollBar::add-line, QScrollBar::sub-line {
    height: 0px;
    width: 0px;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
}
QCheckBox, QRadioButton, QGroupBox {
    color: #D1D4DC;
}
QGroupBox {
    border: 1px solid #2A2E39;
    border-radius: 3px;
    margin-top: 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 3px;
    color: #787B86;
}
QDialogButtonBox QPushButton {
    min-width: 72px;
}
"""

# Full application stylesheet. Currently the dialog/popup rules; kept as a single
# string so entrypoints have exactly one thing to install.
APP_STYLESHEET = DIALOG_STYLESHEET


def build_dark_palette() -> QPalette:
    """Dark QPalette mirroring the stylesheet colours.

    The palette is what makes native-drawn parts follow the theme: window and
    base backgrounds, text of widgets without an explicit stylesheet rule,
    disabled states and the platform's own dialog chrome. Without it, Qt keeps
    the platform palette (white) for anything the stylesheet does not name.
    """
    palette = QPalette()
    window = QColor(BG_WINDOW)
    surface = QColor(BG_SURFACE)
    text = QColor(TEXT)
    dim = QColor(TEXT_MUTED)
    border = QColor(BORDER)

    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, surface)
    palette.setColor(QPalette.ColorRole.AlternateBase, window)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, surface)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.Highlight, border)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, surface)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.PlaceholderText, dim)
    palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor(ACCENT))

    # Disabled group: dark, but visibly inert.
    disabled = QColor(DISABLED_TEXT)
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, window)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, surface)
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Highlight,
        QColor(DISABLED_BORDER),
    )

    # Qt has no public "error" role; QPalette.ColorRole.ToolTipText is already in
    # use above, so error text is styled by the stylesheet rules only.
    return palette


def apply_dark_theme(app: QApplication | None = None) -> QApplication | None:
    """Install the dark palette + app stylesheet on the QApplication.

    Idempotent and safe to call from every entrypoint (viewer, demo, tests); it
    is a no-op when there is no QApplication yet, so callers may guard-free call
    it before ``QApplication(...)`` exists.
    """
    if app is None:
        app = QApplication.instance()
    if app is None:
        return None

    # Ask the platform for dark mode first (window chrome, native dialogs and
    # title bars on Windows/macOS). Purely cosmetic: silently unavailable on
    # older Qt versions or platforms that do not implement it.
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    except Exception:
        pass

    app.setPalette(build_dark_palette())
    app.setStyleSheet(APP_STYLESHEET)
    return app
