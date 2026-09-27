"""Tests for the application-wide dark theme.

Regression guard for the "new watchlist" dialog (and every other popup) having
been unreadable: the dark look used to live in widget stylesheets only, so a
dialog opened outside the styled control panel fell back to the platform's light
palette - grey text on white.
"""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton

from chart_viewer.ui import theme
from chart_viewer.ui.watchlist_dialogs import NewWatchlistDialog


def _luminance(color: QColor) -> float:
    return 0.2126 * color.red() + 0.7152 * color.green() + 0.0722 * color.blue()


def test_theme_defines_a_dark_palette():
    palette = theme.build_dark_palette()
    assert _luminance(palette.color(QPalette.ColorRole.Window)) < 60
    assert _luminance(palette.color(QPalette.ColorRole.Base)) < 60
    assert _luminance(palette.color(QPalette.ColorRole.WindowText)) > 150
    assert _luminance(palette.color(QPalette.ColorRole.Button)) < 60


def test_apply_dark_theme_is_idempotent_and_needs_an_app(qapp):
    assert theme.apply_dark_theme(qapp) is qapp
    first = qapp.palette().color(QPalette.ColorRole.Window)
    theme.apply_dark_theme(qapp)
    assert qapp.palette().color(QPalette.ColorRole.Window) == first
    assert _luminance(first) < 60


def test_unstyled_widgets_inherit_the_dark_palette(qapp):
    theme.apply_dark_theme(qapp)
    probe = QPushButton("probe")
    assert _luminance(probe.palette().color(QPalette.ColorRole.Window)) < 60
    assert _luminance(probe.palette().color(QPalette.ColorRole.WindowText)) > 150


def test_new_watchlist_dialog_renders_dark_on_a_dark_background(qapp):
    theme.apply_dark_theme(qapp)
    dialog = NewWatchlistDialog(existing=["10_favorite"], sources=["10_favorite"], parent=None)
    dialog.show()
    qapp.processEvents()

    assert _luminance(dialog.palette().color(QPalette.ColorRole.Window)) < 60

    # The rendered pixels are what the user sees: the window background must be
    # the dark surface colour, not the platform's white.
    image = dialog.grab().toImage()
    corner = image.pixelColor(4, 4)
    assert _luminance(corner) < 70, f"dialog background is not dark: {corner.name()}"

    field = dialog.name_edit.grab().toImage()
    assert _luminance(field.pixelColor(field.width() // 2, field.height() // 2)) < 90

    dialog.close()


def test_dialog_error_state_keeps_the_dark_background(qapp):
    theme.apply_dark_theme(qapp)
    dialog = NewWatchlistDialog(existing=["10_favorite"], sources=["10_favorite"], parent=None)
    dialog.show()
    dialog.name_edit.setText("all")  # reserved -> inline warning
    dialog._on_accept()
    qapp.processEvents()

    assert dialog.result() != QDialog.DialogCode.Accepted
    corner = dialog.grab().toImage().pixelColor(4, 4)
    assert _luminance(corner) < 70, f"error dialog background is not dark: {corner.name()}"
    dialog.close()


def test_message_box_renders_dark(qapp):
    theme.apply_dark_theme(qapp)
    box = QMessageBox(
        QMessageBox.Icon.Question,
        "Watchlist löschen",
        "Watchlist 'x' löschen?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
    )
    box.show()
    qapp.processEvents()
    corner = box.grab().toImage().pixelColor(6, 6)
    assert _luminance(corner) < 70, f"QMessageBox background is not dark: {corner.name()}"
    box.close()


def test_control_panel_stylesheet_still_covers_its_own_selectors():
    """The generic widget rules moved to theme.py - the panel keeps its own."""
    from chart_viewer.ui.control_panel import PANEL_STYLESHEET

    assert "panelRoot" in PANEL_STYLESHEET
    assert "sectionLabel" in PANEL_STYLESHEET
    assert "pinButton" in PANEL_STYLESHEET
    # The application level now owns the generic popup/input rules.
    assert "QDialog" in theme.APP_STYLESHEET
    assert "QComboBox QAbstractItemView" in theme.APP_STYLESHEET
    assert "QPushButton" in theme.APP_STYLESHEET
