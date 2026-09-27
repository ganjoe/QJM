"""Regression: die Topbar darf das Fenster nicht in die Breite zwingen.

Die Info-Zeile (Symbol + Kennzahlen + Fundamentals) ist oft deutlich breiter
als ein kleiner Monitor. Ein normales QLabel meldet ohne Wortumbruch seine
volle Textbreite als minimumSizeHint - das Chart-Fenster liess sich dadurch
nicht unter ~1000 px verkleinern. Erwartet wird jetzt: der Text wird bei
knappem Platz einfach verdeckt (beschnitten), die Mindestbreite bleibt klein.
"""

from chart_viewer.config import ViewerConfig
from chart_viewer.ui.topbar import TopBarWidget
from chart_viewer.ui.window import ChartWindow

LONG_INFO = "PLNT | Last: $18.42 | " + " | ".join(
    f"Kennzahl {i}: 123.45" for i in range(12)
)


def _make_window(qapp, content: str = LONG_INFO) -> ChartWindow:
    win = ChartWindow("win_test_1d", ViewerConfig())
    win.topbar.set_block(
        {"block_id": "info_block", "position": {"row": 0, "col": 0}, "content": content}
    )
    # show(): ohne sichtbares Fenster laufen die Layouts nicht durch, dann
    # blieben Breiten-Vergleiche zufaellig (und damit wertlos).
    win.show()
    qapp.processEvents()
    return win


def _resize(qapp, win: ChartWindow, width: int, height: int = 400) -> None:
    win.resize(width, height)
    qapp.processEvents()


def test_long_info_line_does_not_raise_minimum_width(qapp):
    """Die Textbreite der Info-Zeile darf nicht die Fenster-Mindestbreite sein."""
    win = _make_window(qapp)
    label = win.topbar._blocks["info_block"]

    assert label.sizeHint().width() > 600, "Testaufbau: Info-Zeile muss lang sein"
    assert label.minimumSizeHint().width() == 0
    assert win.minimumSizeHint().width() < 300, (
        f"Fenster-Mindestbreite {win.minimumSizeHint().width()} px - "
        "lange Info-Zeilen blockieren wieder das Verkleinern"
    )


def test_narrow_window_clips_instead_of_growing(qapp):
    """Bei zu wenig Platz wird der Text verdeckt, das Fenster bleibt schmal."""
    win = _make_window(qapp)
    _resize(qapp, win, 320)

    label = win.topbar._blocks["info_block"]
    assert win.width() <= 340
    assert label.width() > 0
    assert label.width() < label.sizeHint().width(), "Text muss beschnitten sein"
    assert label.text() == LONG_INFO, "Der volle Text bleibt im Label erhalten"


def test_wide_window_shows_the_full_info_line(qapp):
    """Bei genug Platz steht die Info-Zeile unbeschnitten da."""
    win = _make_window(qapp)
    _resize(qapp, win, 2000)

    label = win.topbar._blocks["info_block"]
    assert label.width() >= label.sizeHint().width(), (
        "Bei genug Platz darf die Info-Zeile nicht beschnitten werden"
    )


def test_topbar_font_stays_compact(qapp):
    """Die Info-Zeile bleibt eine kompakte Statuszeile, kein Riesenbanner."""
    assert TopBarWidget.FONT_SIZE_PX <= 14
    assert TopBarWidget.ROW_HEIGHT_PX <= 44

    win = _make_window(qapp)
    assert win.topbar.height() == TopBarWidget.ROW_HEIGHT_PX
