"""Control panel window: ticker search, watchlist editing and chart presets.

The panel is owned by :class:`ViewerApp` (not by the agent layout ledger): it is
created once when the viewer starts and stays available independently of the chart
windows. All data operations are delegated to the agent via `control.request`
envelopes; the panel never talks to the database directly.
"""

from __future__ import annotations

import re
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from PySide6.QtCore import QEvent, QPoint, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFontMetrics, QKeyEvent, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from chart_viewer.config import ViewerConfig
from chart_viewer.ui import theme
from chart_viewer.ui import watchlist_dialogs
from chart_viewer.ui.collapsible import CollapsibleSection
from chart_viewer.ui.color_flag import ColorFlagButton

# Panel-specific selectors only. The generic widget rules (labels, inputs, buttons,
# lists, popups) live in ui/theme.py on the application level, so dialogs - which
# are children of the panel but separate top-level windows - and context menus get
# the same dark look. Kept as a module constant because tests / callers reference it.
# Panel-specific selectors only - the selector braces are literal CSS, so this is
# built by concatenation (no str.format: a QSS template would need every brace
# escaped, and one missed escape turns into a KeyError at import time).
PANEL_STYLESHEET = (
    """
QMainWindow, QWidget#panelRoot, QWidget#panelPage, QScrollArea, QScrollArea > QWidget > QWidget {
    background-color: """
    + theme.BG_WINDOW
    + """;
}
QLabel#sectionLabel {
    color: """
    + theme.TEXT_DIM
    + """;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 1px;
}
QLabel#statusLabel {
    color: """
    + theme.TEXT_MUTED
    + """;
    font-size: 10px;
}
QLabel#statusLabel[error="true"] {
    color: """
    + theme.ERROR
    + """;
}
/* Eigenstaendig dunkel: das Panel darf nicht davon abhaengen, dass die
   Anwendungspalette (apply_dark_theme) installiert wurde - sonst werden
   Listen/Eingaben hell und die gedaempften Texte unlesbar. Werte entsprechen
   theme.DIALOG_STYLESHEET. */
QListWidget, QComboBox, QLineEdit {
    background-color: """ + theme.BG_WINDOW + """;
    color: """ + theme.TEXT + """;
    border: 1px solid """ + theme.BORDER + """;
}
QListWidget {
    font-size: 12px;
}
QListWidget::item {
    padding: 2px 4px;
}
QListWidget::item:selected {
    background-color: """ + theme.BORDER + """;
    color: """ + theme.TEXT + """;
}
QCheckBox, QLabel {
    color: """ + theme.TEXT + """;
}
QLabel:disabled, QCheckBox:disabled {
    color: """ + theme.DISABLED_TEXT + """;
}
QPushButton {
    background-color: """ + theme.BG_SURFACE + """;
    color: """ + theme.TEXT + """;
    border: 1px solid """ + theme.BORDER + """;
    padding: 2px 8px;
}
QPushButton:disabled {
    color: """ + theme.DISABLED_TEXT + """;
    border: 1px solid """ + theme.DISABLED_BORDER + """;
}
QPushButton#toolButton, QPushButton#pinButton, QPushButton#scaleButton, QToolButton {
    padding: 0px;
}
QPushButton#scaleButton[log="true"] {
    border: 1px solid """ + theme.ACCENT + """;
    color: """ + theme.ACCENT + """;
}
QToolButton {
    background-color: """ + theme.BG_SURFACE + """;
    color: """ + theme.TEXT + """;
    border: 1px solid """ + theme.BORDER + """;
}
QToolButton:disabled {
    color: """ + theme.DISABLED_TEXT + """;
    border: 1px solid """ + theme.DISABLED_BORDER + """;
}
QToolButton::menu-indicator {
    image: none;
}
QToolButton#sectionHeader {
    background-color: transparent;
    border: none;
    color: """ + theme.TEXT_DIM + """;
    font-size: 11px;
    font-weight: bold;
}
QToolButton#sectionHeader:hover {
    color: """ + theme.TEXT + """;
}
QLabel#draftLabel {
    color: #FFCA28;
    font-size: 11px;
}
QTabWidget::pane {
    border: none;
    border-top: 1px solid """ + theme.BORDER + """;
}
QTabBar::tab {
    background-color: """ + theme.BG_WINDOW + """;
    color: """ + theme.TEXT_DIM + """;
    padding: 4px 14px;
    border: none;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:selected {
    color: """ + theme.TEXT + """;
    border-bottom: 2px solid """ + theme.ACCENT + """;
}
QTabBar::tab:hover {
    color: """ + theme.TEXT + """;
}
QPushButton#pinButton[pinned="true"] {
    border: 1px solid #FFCA28;
    background-color: #4A3B12;
    color: #FFCA28;
}
"""
)

# Kurze englische Feldlabels der Builder-/Transferzeilen (eine feste Spalte).
FIELD_LABELS = ("Price", "Add", "Preset", "Title", "Height", "Load")
TARGET_NONE_TEXT = "→ no chart window"
DRAFT_TEXT = "● draft"
# Mindestens so viele Zeichen zeigt eine Combo; laengere Namen kuerzt sie mit "…"
# statt das Panel zu verbreitern (die Aufklappliste zeigt sie vollstaendig).
COMBO_MIN_CHARS = 8


def _shrinkable_combo(combo: QComboBox) -> None:
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(COMBO_MIN_CHARS)
    view = combo.view()
    if view is not None:
        view.setTextElideMode(Qt.TextElideMode.ElideNone)


def _shift_pressed() -> bool:
    return bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


class _ElidingComboBox(QComboBox):
    """Nicht editierbare Combo, die einen zu langen Eintrag mit "…" kuerzt.

    Eine normale QComboBox schneidet den Text am Rand hart ab. Die Aufklappliste
    zeigt die Namen weiterhin vollstaendig, der Tooltip ebenfalls.
    """

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        field = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, self
        )
        option.currentText = self.fontMetrics().elidedText(
            option.currentText, Qt.TextElideMode.ElideRight, max(0, field.width() - 4)
        )
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)


class _ElidedLabel(QLabel):
    """Einzeiliges Label, das zu langen Text mit "…" kuerzt statt das Panel zu verbreitern.

    text() liefert weiterhin den vollen Text; gekuerzt wird nur beim Zeichnen.
    """

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt naming)
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)


# Virtual panes without a database row (contract: docs/architecture/chart-presets.md §1).
BUILTIN_PANE_NAMES = {"builtin:volume": "Volumen"}


def is_price_preset(preset: Dict[str, Any]) -> bool:
    """Preispane-Presets gehoeren ins Preispane-Dropdown.

    Der Vertrag erlaubt genau ein Pane mit role=price (chart-presets.md §1) - als
    regulaere Pane angehaengt entstehen doppelte SMA-Panes oder ein zweites
    Preispane, das der Resolver still in die Rest-Panes schiebt.
    """
    return str(preset.get("role") or "").strip().lower() == "price"


def derive_chart_id(raw: str) -> str:
    """Turn a typed chart name into a valid chart id ('Mein Chart' -> 'mein_chart').

    An input that already looks like an id is kept verbatim; everything else is
    slugified (lower case, spaces to underscores, unknown characters dropped).
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        return text
    slug = re.sub(r"\s+", "_", text.lower())
    slug = re.sub(r"[^a-z0-9_.-]", "", slug)
    return slug.strip("._-")


class ControlPanelWindow(QMainWindow):
    """Always-available control surface for the chart viewer."""

    control_request = Signal(str, str, dict)  # request_id, op, params
    symbol_activated = Signal(str, int, str)  # symbol, color_flag, preset_id

    def __init__(self, config: ViewerConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        self._connected = False
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._search_inflight: Optional[str] = None
        self._queued_query: Optional[str] = None
        self._watchlists: List[Dict[str, Any]] = []
        # Watchlists that exist only in this panel session: the database stores
        # list membership, so a list without tickers has no row to persist yet.
        self._pending_watchlists: List[str] = []
        self._select_after_refresh: str = ""
        self._watchlist_editable = False
        self._current_watchlist: str = ""
        self._suppress_selection_activation = False
        self._preset_id: str = ""
        self._chart_windows: List[Dict[str, Any]] = []
        # Chart builder (pane presets + chart definition of the focused window).
        # The target window is sticky: only a real window activation or an
        # explicit set_target_window() moves it - never the panel's own refresh.
        self._builder_target: str = ""
        self._chart_symbols: Dict[str, str] = {}
        self._pane_catalog: List[Dict[str, Any]] = []
        self._chart_catalog: List[Dict[str, Any]] = []
        self._builder_panes: List[Dict[str, Any]] = []
        self._chart_state: Dict[str, Any] = {}
        self._builder_draft = False
        # Eine get_chart_state-Antwort ist unterwegs, waehrend schon wieder ein
        # neuer Stand angefragt wurde: nach dem Eintreffen einmal nachlesen.
        self._chart_state_pending_more = False
        self._populating = False
        # Zuletzt benutzte Ziele fuer Copy/Move (oben im Rechtsklick-Menue).
        self._recent_targets: List[str] = []
        # Breite der Label-Spalte im Builder (aus der Schrift berechnet, _field_label).
        self._label_column_width = 0
        self._settings = QSettings("QJM", "ChartViewer")
        self._shutting_down = False

        self.setWindowTitle("Chart Control")
        self._apply_pin(self._read_pinned_setting())
        self.resize(
            self.config.control_panel_width,
            self.config.control_panel_height,
        )
        self._build_ui()
        self._restore_settings()
        self.set_connection_state(False)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._emit_search)

        self._timeout_timer = QTimer(self)
        self._timeout_timer.setInterval(1000)
        self._timeout_timer.timeout.connect(self._check_timeouts)
        self._timeout_timer.start()

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("panelRoot")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(6)

        # Fester Kopf (in beiden Tabs gleich): Flag, Zielfenster des Builders,
        # Entwurfsmarke und Pin.
        header = QHBoxLayout()
        header.setSpacing(6)
        self.flag_btn = ColorFlagButton(initial_flag=0, parent=self)
        self.flag_btn.setToolTip(
            "Flag-Gruppe: Ticker öffnen sich in den Charts dieser Farbe (Klick wechselt die Farbe)"
        )
        self.flag_btn.flag_changed.connect(self._on_flag_changed)
        header.addWidget(self.flag_btn)
        self.builder_target_label = _ElidedLabel(TARGET_NONE_TEXT, root)
        self.builder_target_label.setToolTip(
            "Fokussiertes Chartfenster (klebrig – bleibt stehen, auch wenn das Panel den Fokus hat)"
        )
        header.addWidget(self.builder_target_label, 1)
        self.builder_draft_label = QLabel("", root)
        self.builder_draft_label.setObjectName("draftLabel")
        self.builder_draft_label.setToolTip("Noch nicht gespeicherter Entwurf (Speichern: Ctrl+S)")
        header.addWidget(self.builder_draft_label)
        self.pin_btn = QPushButton("📌", root)
        self.pin_btn.setObjectName("pinButton")
        self.pin_btn.setFixedWidth(28)
        self.pin_btn.setToolTip("Immer im Vordergrund")
        self.pin_btn.clicked.connect(self._toggle_pin)
        header.addWidget(self.pin_btn)
        layout.addLayout(header)

        self.tabs = QTabWidget(root)
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.addTab(self._scroll_page(self._build_symbols_tab()), "Symbols")
        self.tabs.addTab(self._scroll_page(self._build_chart_tab()), "Chart")
        self.tabs.setTabToolTip(0, "Ticker-Suche und Watchlists (Ctrl+1)")
        self.tabs.setTabToolTip(1, "Chart-Builder des Zielfensters (Ctrl+2)")
        self.tabs.currentChanged.connect(lambda index: self._settings.setValue("panel/tab", index))
        layout.addWidget(self.tabs, 1)

        # Statuszeile: offene Charts der Flag-Gruppe + letzte Meldung.
        self.charts_label = QLabel("Charts: –", root)
        self.charts_label.setObjectName("statusLabel")
        self.charts_label.setWordWrap(True)
        layout.addWidget(self.charts_label)
        self.status_label = QLabel("", root)
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._install_shortcuts()
        self.setStyleSheet(PANEL_STYLESHEET)
        self._update_buttons()

    def _scroll_page(self, page: QWidget) -> QScrollArea:
        """Tab-Seite nur senkrecht scrollbar: nichts wird seitlich abgeschnitten.

        Die Seiten sind so gebaut, dass ihre Mindestbreite ins 360-px-Panel passt
        (siehe test_control_panel_fits_the_default_width); lange Namen kuerzen die
        Combos und Listen mit "…" statt das Panel zu verbreitern.
        """
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _build_symbols_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("panelPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 4, 0)
        layout.setSpacing(6)

        # Ticker search: die Trefferliste nimmt nur Platz ein, solange gesucht wird.
        self.search_edit = QLineEdit(page)
        self.search_edit.setPlaceholderText("Search ticker…")
        self.search_edit.setToolTip("Ticker suchen (Ctrl+F) – Enter öffnet den ersten Treffer im Chart")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        self.search_edit.returnPressed.connect(self._on_search_return)
        layout.addWidget(self.search_edit)

        self.results_section = CollapsibleSection("Results", page, expanded=False)
        self.result_list = QListWidget(page)
        self.result_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.result_list.setFixedHeight(96)
        self.result_list.itemSelectionChanged.connect(self._update_buttons)
        self.result_list.itemDoubleClicked.connect(lambda _item: self._activate_selected_symbol())
        self.results_section.content_layout.addWidget(self.result_list)
        search_buttons = QHBoxLayout()
        search_buttons.setSpacing(6)
        self.open_chart_btn = QPushButton("Open", page)
        self.open_chart_btn.setToolTip("Ticker im Chart der Flag-Gruppe öffnen (Enter)")
        self.open_chart_btn.clicked.connect(self._activate_selected_symbol)
        search_buttons.addWidget(self.open_chart_btn)
        self.add_btn = QPushButton("Add", page)
        self.add_btn.setToolTip("Ausgewählten Ticker zur aktuellen Watchlist hinzufügen")
        self.add_btn.clicked.connect(self._add_selected_ticker)
        search_buttons.addWidget(self.add_btn)
        search_buttons.addStretch(1)
        self.search_status = QLabel("", page)
        self.search_status.setObjectName("statusLabel")
        search_buttons.addWidget(self.search_status)
        self.results_section.content_layout.addLayout(search_buttons)
        layout.addWidget(self.results_section)

        # Watchlist: Auswahl + Menue fuer die seltenen Aktionen (neu/loeschen/laden).
        layout.addWidget(self._section_label("WATCHLIST"))
        wl_row = QHBoxLayout()
        wl_row.setSpacing(6)
        self.watchlist_combo = QComboBox(page)
        self.watchlist_combo.setEditable(True)
        self.watchlist_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        _shrinkable_combo(self.watchlist_combo)
        completer = self.watchlist_combo.completer()
        if completer is not None:
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.watchlist_combo.activated.connect(self._on_watchlist_activated)
        # An editable combo hands mouse presses to its line edit, which only places
        # the text cursor; the arrow remains the only spot that opens the popup.
        # The filter below makes a click anywhere in the field open it (eventFilter).
        if self.watchlist_combo.lineEdit() is not None:
            self.watchlist_combo.lineEdit().installEventFilter(self)
            self.watchlist_combo.lineEdit().returnPressed.connect(self._commit_watchlist_text)
        wl_row.addWidget(self.watchlist_combo, 1)

        self.watchlist_menu_btn = QToolButton(page)
        self.watchlist_menu_btn.setText("⋯")
        self.watchlist_menu_btn.setFixedWidth(28)
        self.watchlist_menu_btn.setToolTip("Watchlist anlegen, löschen oder neu laden")
        self.watchlist_menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        wl_menu = QMenu(self.watchlist_menu_btn)
        self.new_watchlist_btn = wl_menu.addAction("New list…")
        self.new_watchlist_btn.setToolTip("Neue Watchlist anlegen (leer oder als Kopie einer bestehenden)")
        self.new_watchlist_btn.triggered.connect(self._create_watchlist)
        self.delete_watchlist_btn = wl_menu.addAction("Delete list…")
        self.delete_watchlist_btn.setToolTip("Watchlist mit allen Tickern löschen")
        self.delete_watchlist_btn.triggered.connect(self._delete_watchlist)
        wl_menu.addSeparator()
        self.watchlist_reload_btn = wl_menu.addAction("Reload")
        self.watchlist_reload_btn.setToolTip("Watchlisten neu laden")
        self.watchlist_reload_btn.triggered.connect(lambda: self.refresh_watchlists())
        self.watchlist_menu_btn.setMenu(wl_menu)
        wl_row.addWidget(self.watchlist_menu_btn)
        layout.addLayout(wl_row)

        self.watchlist_list = QListWidget(page)
        self.watchlist_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.watchlist_list.setToolTip(
            "C kopieren · V verschieben (Ziel unten) · Shift+C/V mit Zielauswahl · "
            "T Ziel wählen · Entf entfernen · Rechtsklick für alle Ziele"
        )
        # Marking a row is enough to load it - no double click required.
        self.watchlist_list.itemSelectionChanged.connect(self._on_watchlist_selection_changed)
        self.watchlist_list.installEventFilter(self)
        self.watchlist_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.watchlist_list.customContextMenuRequested.connect(self._show_watchlist_context_menu)
        layout.addWidget(self.watchlist_list, 1)

        wl_buttons = QHBoxLayout()
        wl_buttons.setSpacing(6)
        self.remove_btn = QPushButton("Remove", page)
        self.remove_btn.setToolTip("Ausgewählten Ticker aus der Watchlist entfernen (Entf)")
        self.remove_btn.clicked.connect(self._remove_selected_ticker)
        wl_buttons.addWidget(self.remove_btn)
        wl_buttons.addStretch(1)
        self.watchlist_info = QLabel("", page)
        self.watchlist_info.setObjectName("statusLabel")
        wl_buttons.addWidget(self.watchlist_info)
        layout.addLayout(wl_buttons)

        # Kopieren/Verschieben ohne Dialog: das Ziel steht fest und wird gemerkt.
        transfer_row = QHBoxLayout()
        transfer_row.setSpacing(6)
        transfer_row.addWidget(QLabel("To", page))
        self.transfer_target_combo = _ElidingComboBox(page)
        _shrinkable_combo(self.transfer_target_combo)
        self.transfer_target_combo.setToolTip(
            "Ziel-Watchlist für Copy/Move (T in der Liste springt hierher)"
        )
        self.transfer_target_combo.activated.connect(self._on_transfer_target_activated)
        self.transfer_target_combo.currentTextChanged.connect(
            lambda text: self.transfer_target_combo.setToolTip(
                f"Ziel für Copy (C) / Move (V): {text}\nT in der Liste springt hierher"
                if text
                else "Ziel-Watchlist für Copy/Move"
            )
        )
        transfer_row.addWidget(self.transfer_target_combo, 1)
        self.copy_btn = QPushButton("Copy", page)
        self.copy_btn.setToolTip(
            "Ausgewählten Ticker in die Ziel-Watchlist kopieren (C) – Shift: anderes Ziel wählen"
        )
        self.copy_btn.clicked.connect(self._copy_selected_ticker)
        transfer_row.addWidget(self.copy_btn)
        self.move_btn = QPushButton("Move", page)
        self.move_btn.setToolTip(
            "Ausgewählten Ticker in die Ziel-Watchlist verschieben (V) – Shift: anderes Ziel wählen"
        )
        self.move_btn.clicked.connect(self._move_selected_ticker)
        transfer_row.addWidget(self.move_btn)
        layout.addLayout(transfer_row)
        return page

    def _build_chart_tab(self) -> QWidget:
        """Chart builder: pane presets + chart definition of the focused chart window."""
        page = QWidget()
        page.setObjectName("panelPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 4, 0)
        layout.setSpacing(8)

        # Vorlage: gespeichertes Chart laden und den Entwurf speichern - beides an
        # einer Stelle, weil es denselben Chart-Bestand betrifft.
        self.template_section = self._section("template", "Template")
        load_row = QHBoxLayout()
        load_row.setSpacing(6)
        load_row.addWidget(self._field_label("Load", page))
        self.preset_combo = QComboBox(page)
        self.preset_combo.setEditable(True)
        self.preset_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.preset_combo.setToolTip("Gespeichertes Chart auf die Charts der Flag-Gruppe anwenden")
        _shrinkable_combo(self.preset_combo)
        preset_completer = self.preset_combo.completer()
        if preset_completer is not None:
            preset_completer.setFilterMode(Qt.MatchFlag.MatchContains)
            preset_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.preset_combo.activated.connect(self._on_preset_activated)
        load_row.addWidget(self.preset_combo, 1)
        self.template_section.content_layout.addLayout(load_row)

        save_chart_row = QHBoxLayout()
        save_chart_row.setSpacing(6)
        self.save_inplace_btn = QPushButton("Save", page)
        self.save_inplace_btn.setToolTip("Entwurf im aktuellen Chart speichern, ohne Namensdialog (Ctrl+S)")
        self.save_inplace_btn.clicked.connect(self._save_chart_in_place)
        save_chart_row.addWidget(self.save_inplace_btn)
        self.save_chart_btn = QPushButton("Save as…", page)
        self.save_chart_btn.setToolTip("Entwurf als neue Chart-Definition speichern")
        self.save_chart_btn.clicked.connect(self._save_chart_as)
        save_chart_row.addWidget(self.save_chart_btn)
        self.discard_chart_btn = QPushButton("Revert", page)
        self.discard_chart_btn.setToolTip("Änderungen verwerfen und letzten gespeicherten Stand rendern")
        self.discard_chart_btn.clicked.connect(self._discard_builder)
        save_chart_row.addWidget(self.discard_chart_btn)
        save_chart_row.addStretch(1)
        self.template_section.content_layout.addLayout(save_chart_row)
        layout.addWidget(self.template_section)

        # Panes des Zielcharts: Preispane, Liste mit Werkzeugleiste, Anhaengen.
        self.panes_section = self._section("panes", "Panes")
        self.volume_check = QCheckBox("Volume", page)
        self.volume_check.setToolTip("Eingebautes Volumen-Pane hinzufügen/entfernen")
        self.volume_check.toggled.connect(self._on_volume_toggled)
        self.panes_section.add_header_widget(self.volume_check)
        self.builder_reload_btn = QToolButton(page)
        self.builder_reload_btn.setText("⟳")
        self.builder_reload_btn.setFixedWidth(28)
        self.builder_reload_btn.setToolTip("Pane- und Chart-Katalog neu laden")
        self.builder_reload_btn.clicked.connect(self.refresh_catalog)
        self.panes_section.add_header_widget(self.builder_reload_btn)
        panes_layout = self.panes_section.content_layout

        price_row = QHBoxLayout()
        price_row.setSpacing(6)
        price_row.addWidget(self._field_label("Price", page))
        self.price_pane_combo = _ElidingComboBox(page)
        self.price_pane_combo.setToolTip("Pane-Preset für das Preispane (pane_id 'main')")
        _shrinkable_combo(self.price_pane_combo)
        self.price_pane_combo.activated.connect(self._on_price_pane_activated)
        price_row.addWidget(self.price_pane_combo, 1)
        panes_layout.addLayout(price_row)

        list_row = QHBoxLayout()
        list_row.setSpacing(6)
        self.builder_pane_list = QListWidget(page)
        self.builder_pane_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Lange Zeilen werden mit "…" gekuerzt (voller Text im Tooltip), statt eine
        # horizontale Scrollleiste zu erzwingen.
        self.builder_pane_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.builder_pane_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.builder_pane_list.setMinimumHeight(96)
        self.builder_pane_list.setMaximumHeight(160)
        self.builder_pane_list.setToolTip(
            "Panes dieses Charts in Reihenfolge – Alt+↑/↓ verschiebt die markierte Pane"
        )
        self.builder_pane_list.installEventFilter(self)
        list_row.addWidget(self.builder_pane_list, 1)
        tools = QVBoxLayout()
        tools.setSpacing(4)
        self.pane_up_btn = self._tool_button("▲", "Markierte Pane nach oben (Alt+↑)", page)
        self.pane_up_btn.clicked.connect(lambda: self._move_selected_pane(-1))
        tools.addWidget(self.pane_up_btn)
        self.pane_down_btn = self._tool_button("▼", "Markierte Pane nach unten (Alt+↓)", page)
        self.pane_down_btn.clicked.connect(lambda: self._move_selected_pane(1))
        tools.addWidget(self.pane_down_btn)
        self.duplicate_pane_btn = self._tool_button(
            "⧉", "Markierte Pane duplizieren (eigener Slot, gleiche Einstellungen)", page
        )
        self.duplicate_pane_btn.clicked.connect(self._on_duplicate_pane_clicked)
        tools.addWidget(self.duplicate_pane_btn)
        self.remove_pane_btn = self._tool_button("✕", "Markierte Pane entfernen (das Preispane bleibt)", page)
        self.remove_pane_btn.clicked.connect(self._on_remove_pane_clicked)
        tools.addWidget(self.remove_pane_btn)
        tools.addStretch(1)
        list_row.addLayout(tools)
        panes_layout.addLayout(list_row)

        add_pane_row = QHBoxLayout()
        add_pane_row.setSpacing(6)
        add_pane_row.addWidget(self._field_label("Add", page))
        self.add_pane_combo = QComboBox(page)
        self.add_pane_combo.setEditable(True)
        self.add_pane_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.add_pane_combo.setToolTip("Pane-Preset wählen und mit ＋ ans Ende anhängen (Enter im Feld)")
        _shrinkable_combo(self.add_pane_combo)
        add_completer = self.add_pane_combo.completer()
        if add_completer is not None:
            add_completer.setFilterMode(Qt.MatchFlag.MatchContains)
            add_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            add_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        if self.add_pane_combo.lineEdit() is not None:
            self.add_pane_combo.lineEdit().setPlaceholderText("Preset…")
            self.add_pane_combo.lineEdit().returnPressed.connect(self._on_add_pane_clicked)
        add_pane_row.addWidget(self.add_pane_combo, 1)
        self.add_pane_btn = self._tool_button("＋", "Pane ans Ende anhängen (Enter im Feld)", page)
        self.add_pane_btn.clicked.connect(self._on_add_pane_clicked)
        add_pane_row.addWidget(self.add_pane_btn)
        panes_layout.addLayout(add_pane_row)
        layout.addWidget(self.panes_section)

        # Pane-Editor: Preset, Titel, Hoehe (Gewicht) und Skala der markierten Pane.
        # Der Preset-Wechsel referenziert ein anderes geteiltes Preset, der Titel landet
        # als Override nur in diesem Chart - das Preset selbst bleibt sauber.
        self.pane_section = self._section("pane", "Selected pane")
        pane_layout = self.pane_section.content_layout
        pane_preset_row = QHBoxLayout()
        pane_preset_row.setSpacing(6)
        pane_preset_row.addWidget(self._field_label("Preset", page))
        self.pane_preset_combo = _ElidingComboBox(page)
        self.pane_preset_combo.setToolTip(
            "Pane-Preset der markierten Pane wechseln - der Slot bleibt, das Preispane "
            "wechselt oben (Price)"
        )
        _shrinkable_combo(self.pane_preset_combo)
        self.pane_preset_combo.activated.connect(self._on_pane_preset_activated)
        pane_preset_row.addWidget(self.pane_preset_combo, 1)
        pane_layout.addLayout(pane_preset_row)

        pane_title_row = QHBoxLayout()
        pane_title_row.setSpacing(6)
        pane_title_row.addWidget(self._field_label("Title", page))
        self.pane_title_edit = QLineEdit(page)
        self.pane_title_edit.setToolTip("Titel der markierten Pane (Override nur in diesem Chart)")
        self.pane_title_edit.editingFinished.connect(self._on_pane_title_changed)
        pane_title_row.addWidget(self.pane_title_edit, 1)
        pane_layout.addLayout(pane_title_row)

        pane_edit_row = QHBoxLayout()
        pane_edit_row.setSpacing(6)
        pane_edit_row.addWidget(self._field_label("Height", page))
        self.pane_weight_spin = QSpinBox(page)
        self.pane_weight_spin.setRange(1, 20)
        self.pane_weight_spin.setFixedWidth(52)
        self.pane_weight_spin.setToolTip("Gewicht der markierten Pane (sofort angewendet)")
        self.pane_weight_spin.editingFinished.connect(self._on_pane_weight_changed)
        pane_edit_row.addWidget(self.pane_weight_spin)
        pane_edit_row.addStretch(1)
        pane_edit_row.addWidget(QLabel("Scale", page))
        self.scale_toggle_btn = QPushButton("LIN", page)
        self.scale_toggle_btn.setObjectName("scaleButton")
        self.scale_toggle_btn.setToolTip("Y-Skala der markierten Pane umschalten (LIN ↔ LOG)")
        self.scale_toggle_btn.setFixedWidth(
            QFontMetrics(self.scale_toggle_btn.font()).horizontalAdvance("LOG") + 24
        )
        self.scale_toggle_btn.clicked.connect(self._toggle_selected_pane_scale)
        pane_edit_row.addWidget(self.scale_toggle_btn)
        pane_layout.addLayout(pane_edit_row)
        layout.addWidget(self.pane_section)

        self.builder_pane_list.itemSelectionChanged.connect(self._on_pane_selection_changed)
        layout.addStretch(1)
        return page

    def _section(self, key: str, title: str) -> CollapsibleSection:
        """Aufklappbarer Bereich, dessen Zustand ueber Neustarts erhalten bleibt."""
        expanded = _as_bool(self._settings.value(f"panel/section/{key}", True), True)
        section = CollapsibleSection(title, expanded=expanded)
        section.toggled.connect(lambda state: self._settings.setValue(f"panel/section/{key}", state))
        return section

    @staticmethod
    def _tool_button(text: str, tooltip: str, parent: QWidget) -> QPushButton:
        button = QPushButton(text, parent)
        button.setObjectName("toolButton")
        button.setFixedWidth(28)
        button.setToolTip(tooltip)
        return button

    def _install_shortcuts(self) -> None:
        """Panel-weite Tastenkuerzel (nur solange das Panel den Fokus hat)."""
        bindings = (
            ("Ctrl+1", lambda: self.tabs.setCurrentIndex(0)),
            ("Ctrl+2", lambda: self.tabs.setCurrentIndex(1)),
            ("Ctrl+F", self._focus_search),
            ("Ctrl+S", self._save_shortcut),
        )
        self._shortcuts = []
        for keys, slot in bindings:
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    def _focus_search(self) -> None:
        self.tabs.setCurrentIndex(0)
        self.search_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_edit.selectAll()

    def _save_shortcut(self) -> None:
        if self.save_inplace_btn.isEnabled():
            self._save_chart_in_place()

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        return label

    def _field_label(self, text: str, parent: Optional[QWidget] = None) -> QLabel:
        """Label einer Builder-Zeile in einer festen Spalte.

        Eine feste Spalte haelt Combos und Spinboxen untereinander ausgerichtet; die
        kurzen englischen Labels kosten wenig Breite: das Panel ist nur ~360 px breit,
        die Preset-Zusammenfassungen brauchen den Platz im Combo. Die Breite kommt
        aus der Schrift, damit sie bei anderer Skalierung nicht abschneidet.
        """
        if self._label_column_width <= 0:
            metrics = QFontMetrics(self.font())
            self._label_column_width = (
                max(
                    metrics.horizontalAdvance(text)
                    for text in FIELD_LABELS
                )
                + 6
            )
        label = QLabel(text, parent)
        label.setFixedWidth(self._label_column_width)
        return label

    def _pane_is_price(self, pane: Dict[str, Any]) -> bool:
        """Preispane-Erkennung: Slot 'main' oder Rolle aus dem Katalog.

        Die Serverantwort zu get_chart_state liefert keine Rolle mit; dann entscheidet
        das referenzierte Pane-Preset.
        """
        if str(pane.get("pane_id") or "") == "main":
            return True
        role = str(pane.get("role") or "").strip().lower()
        if not role:
            preset = self._pane_preset(str(pane.get("pane_preset_id") or ""))
            role = str(preset.get("role") or "").strip().lower()
        return role == "price"

    def _pane_preset_switchable(self) -> bool:
        """Nur Nicht-Preis-Panes wechseln ihr Preset (Preispane: Dropdown oben)."""
        pane = self._selected_pane()
        return pane is not None and not self._pane_is_price(pane)

    # ── public API ─────────────────────────────────────────────────────────

    def set_connection_state(self, connected: bool) -> None:
        """Called by ViewerApp on (re-)connect/disconnect; reloads on connect."""
        was_connected = self._connected
        self._connected = connected
        if connected:
            self._set_status("● verbunden")
            if not was_connected:
                self.refresh_all()
        else:
            self._set_status("○ getrennt – warte auf Agent", error=True)
            self.open_chart_btn.setEnabled(False)
            self.add_btn.setEnabled(False)
            self._update_buttons()

    def apply_response(self, payload: Dict[str, Any]) -> None:
        """Handle a `control.response` envelope (GUI thread)."""
        if not isinstance(payload, dict):
            return
        request_id = str(payload.get("request_id") or "")
        meta = self._pending.pop(request_id, None)
        if meta is None:
            return  # stale/unknown response
        op = str(payload.get("op") or meta.get("op") or "")
        ok = bool(payload.get("ok"))
        data = payload.get("data") or {}
        error = payload.get("error") or {}

        if op == "search_symbols":
            self._search_inflight = None
        if not ok:
            message = str(error.get("message") or "unbekannter Fehler")
            # A failed compose/apply must never disable the builder: the local
            # preview stays, "Verwerfen" resynchronises it with the server.
            hint = " (Verwerfen lädt den Serverstand)" if op in ("compose_chart", "apply_chart") else ""
            self._set_status(f"⚠ {op}: {message}{hint}", error=True)
            if (
                op == "mutate_watchlist"
                and meta.get("kind") == "move"
                and meta.get("list_name") == self._current_watchlist
            ):
                # Move nimmt die Zeile sofort heraus; schlaegt er fehl, zeigt die
                # neu geladene Liste wieder den Serverstand.
                self._select_watchlist(self._current_watchlist, reload_content=True)
            if op == "search_symbols":
                self._search_inflight = None
            self._update_buttons()
            return

        if op == "search_symbols":
            self._on_search_response(meta, data)
        elif op == "list_watchlists":
            self._on_watchlists_response(data)
        elif op == "get_watchlist":
            self._on_watchlist_response(data)
        elif op == "mutate_watchlist":
            self._on_mutation_response(meta, data)
        elif op == "apply_preset":
            self._on_apply_preset_response(data)
        elif op == "list_panes":
            self._on_panes_response(data)
        elif op == "list_charts":
            self._on_charts_response(data)
        elif op == "get_chart_state":
            self._on_chart_state_response(data, meta)
        elif op == "compose_chart":
            self._on_compose_response(meta, data)
        elif op == "apply_chart":
            self._on_apply_chart_response(meta, data)
        elif op == "save_chart":
            self._on_save_chart_response(meta, data)
        self._update_buttons()

    def set_chart_windows(self, windows: List[Dict[str, Any]]) -> None:
        """Called by ViewerApp whenever chart windows open/close/change flag.

        Keeps the symbol map for the builder target line. The target itself stays
        sticky: an existing target survives every refresh as long as its window is
        open; only an empty target is filled (first window) and only a closed
        window clears it.
        """
        self._chart_windows = list(windows or [])
        self._chart_symbols = {
            str(w.get("window_id") or ""): str(w.get("symbol") or "?")
            for w in self._chart_windows
            if str(w.get("window_id") or "")
        }
        flag = self.flag_btn.get_flag()
        symbols = [
            str(w.get("symbol") or "?")
            for w in self._chart_windows
            if int(w.get("color_flag") or 0) == flag
        ]
        self.charts_label.setText(f"Charts (flag {flag}): {', '.join(symbols) if symbols else '–'}")

        target_changed = False
        if self._builder_target and self._builder_target not in self._chart_symbols:
            # The focused window is gone: forget its draft and start clean.
            self._builder_target = ""
            self._chart_state = {}
            self._builder_panes = []
            self._builder_draft = False
            target_changed = True
        if not self._builder_target and self._chart_symbols:
            self._builder_target = next(iter(self._chart_symbols))
            target_changed = True
        self._rebuild_builder_widgets()
        if target_changed and self._connected:
            self._refresh_chart_state()

    def set_target_window(self, window_id: str) -> None:
        """Sticky builder target: only an activated chart window moves it.

        Called by ViewerApp when a chart window reports activation (it also stays
        on target while the panel itself holds the focus). Unknown windows are
        ignored so a late signal cannot point the builder at a closed window.
        """
        window_id = str(window_id or "")
        if not window_id or window_id == self._builder_target:
            return
        # Reject stale signals for a window that is no longer open - but accept the
        # very first activation, when the panel has no window list yet.
        if self._chart_symbols and window_id not in self._chart_symbols:
            return
        self._builder_target = window_id
        self._chart_state = {}
        self._builder_panes = []
        self._builder_draft = False
        self._rebuild_builder_widgets()
        if self._connected:
            self._refresh_chart_state()

    def refresh_all(self) -> None:
        self.refresh_watchlists()
        self.refresh_catalog()

    def refresh_watchlists(self, select: str = "") -> None:
        """Reload the list of watchlists; optionally select one afterwards."""
        if not self._connected:
            return
        if select:
            self._select_after_refresh = select
        self._request("list_watchlists", {})

    def refresh_catalog(self) -> None:
        """Load the pane/chart catalog once - dropdowns never re-request it.

        Der Chart-Katalog speist seit dem Builder-Umbau auch die Anwenden-Liste:
        anwendbar ist genau das, was der Builder als Chart kennt und zerlegen kann.
        """
        if not self._connected:
            return
        self._request("list_panes", {})
        self._request("list_charts", {})

    def refresh_charts(self) -> None:
        if not self._connected:
            return
        self._request("list_charts", {})

    def shutdown(self) -> None:
        """Stop timers so a torn-down viewer leaves no Qt events behind.

        The flag is set first: viewer teardown is the only state in which a close
        request for the panel is accepted (see closeEvent).
        """
        self._shutting_down = True
        self._debounce.stop()
        self._timeout_timer.stop()
        self._save_settings()
        self.hide()

    def focus_panel(self) -> None:
        """Bring the panel to the front - without ever hiding it (Ctrl+Shift+P).

        The shortcut used to toggle visibility. Hiding is gone on purpose: a
        hidden panel can only be recovered from a chart window, so with no chart
        open it was unreachable. The panel is permanent while the viewer runs and
        is only taken down by shutdown().
        """
        self.show()
        self.raise_()
        self.activateWindow()

    # ── requests ───────────────────────────────────────────────────────────

    def _request(self, op: str, params: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> str:
        request_id = f"cp_{uuid.uuid4().hex[:12]}"
        entry = {"op": op, "sent_at": time.time()}
        entry.update(meta or {})
        self._pending[request_id] = entry
        try:
            self.control_request.emit(request_id, op, params)
        except Exception:
            self._pending.pop(request_id, None)
            raise
        return request_id

    def _check_timeouts(self) -> None:
        limit = max(1.0, self.config.control_request_timeout_ms / 1000.0)
        now = time.time()
        expired = [rid for rid, meta in self._pending.items() if now - meta.get("sent_at", now) > limit]
        for rid in expired:
            meta = self._pending.pop(rid, {})
            self._set_status(f"⚠ {meta.get('op', '?')}: keine Antwort vom Agent", error=True)
            if meta.get("op") == "search_symbols" and self._search_inflight == rid:
                self._search_inflight = None
        if expired:
            self._update_buttons()

    # ── ticker search ──────────────────────────────────────────────────────

    def _on_search_text_changed(self, text: str) -> None:
        if not text.strip():
            self._debounce.stop()
            self.result_list.clear()
            self.search_status.setText("")
            self.results_section.set_title("Results")
            self.results_section.set_expanded(False)
            self._update_buttons()
            return
        self.results_section.set_expanded(True)
        self._debounce.start(self.config.control_search_debounce_ms)

    def _on_search_return(self) -> None:
        if self.result_list.currentItem() is None and self.result_list.count() > 0:
            self.result_list.setCurrentRow(0)
        self._activate_selected_symbol()

    def _emit_search(self) -> None:
        query = self.search_edit.text().strip()
        if not query:
            return
        if not self._connected:
            self._set_status("⚠ nicht verbunden", error=True)
            return
        if self._search_inflight is not None:
            self._queued_query = query
            return
        self._search_inflight = self._request(
            "search_symbols",
            {"query": query, "limit": self.config.control_search_limit},
            {"query": query},
        )

    def _on_search_response(self, meta: Dict[str, Any], data: Dict[str, Any]) -> None:
        query = str(meta.get("query") or "")
        current = self.search_edit.text().strip()
        queued = self._queued_query
        self._queued_query = None

        if query == current:
            results = data.get("results") or []
            self.result_list.clear()
            for row in results:
                ticker = str(row.get("ticker") or "")
                if not ticker:
                    continue
                kind = str(row.get("type") or "")
                marker = "●" if row.get("has_parquet") else "○"
                item = QListWidgetItem(f"{ticker}   {kind}   {marker}".rstrip())
                item.setData(Qt.ItemDataRole.UserRole, ticker)
                item.setToolTip(
                    f"{ticker}: Chart-Daten vorhanden" if row.get("has_parquet")
                    else f"{ticker}: keine Chart-Daten – Download wird beim Hinzufügen angestoßen"
                )
                self.result_list.addItem(item)
            self.search_status.setText(f"{len(results)} Treffer")
            self.results_section.set_title(f"Results ({len(results)})")
            self.results_section.set_expanded(True)
            if results:
                self.result_list.setCurrentRow(0)

        if queued and queued != current:
            self._queued_query = None
            self._emit_search()

    def _selected_search_ticker(self) -> str:
        item = self.result_list.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    def _activate_selected_symbol(self) -> None:
        ticker = self._selected_search_ticker()
        if not ticker:
            return
        self.symbol_activated.emit(ticker, self.flag_btn.get_flag(), self._preset_id)

    # ── watchlist ──────────────────────────────────────────────────────────

    @contextmanager
    def _selection_guard(self) -> Iterator[None]:
        """Suppress chart routing while the ticker list is rebuilt programmatically.

        QListWidget.clear()/takeItem() move the current row and emit
        itemSelectionChanged; without the guard, removing a ticker would silently
        switch the chart to whatever row becomes current afterwards.
        """
        previous = self._suppress_selection_activation
        self._suppress_selection_activation = True
        try:
            yield
        finally:
            self._suppress_selection_activation = previous

    def _on_watchlists_response(self, data: Dict[str, Any]) -> None:
        watchlists = [w for w in (data.get("watchlists") or []) if isinstance(w, dict)]
        # Lists created "empty" in this session have no row in the database yet
        # (pca_watchlists stores ticker membership); keep them visible locally
        # until the first ticker materializes them.
        stored = {str(w.get("name") or "") for w in watchlists}
        self._pending_watchlists = [n for n in self._pending_watchlists if n and n not in stored]
        for name in self._pending_watchlists:
            watchlists.append({"name": name, "editable": True, "pending": True})
        self._watchlists = watchlists
        self._populate_watchlist_combo()

        previous = (
            self._select_after_refresh
            or self._current_watchlist
            or str(self._settings.value("panel/watchlist", "") or "")
        )
        self._select_after_refresh = ""
        available = self._watchlist_names()
        target = previous if previous in available else (available[0] if available else "")
        if target:
            self._select_watchlist(target, reload_content=True)
        else:
            self._current_watchlist = ""
            self._watchlist_editable = False
            with self._selection_guard():
                self.watchlist_list.clear()
            self.watchlist_info.setText("")
        self._update_buttons()

    def _populate_watchlist_combo(self) -> None:
        self._populating = True
        try:
            self.watchlist_combo.clear()
            for entry in self._watchlists:
                name = str(entry.get("name") or "")
                if not name:
                    continue
                editable = bool(entry.get("editable", True))
                if entry.get("pending"):
                    label = f"{name} (neu)"
                else:
                    label = name if editable else f"{name} 🔒"
                self.watchlist_combo.addItem(label, name)
        finally:
            self._populating = False
        self._populate_transfer_combo()

    def _watchlist_names(self, editable_only: bool = False) -> List[str]:
        names: List[str] = []
        for entry in self._watchlists:
            name = str(entry.get("name") or "")
            if not name:
                continue
            if editable_only and not bool(entry.get("editable", True)):
                continue
            names.append(name)
        return names

    def _transfer_targets(self) -> List[str]:
        """Editable lists a ticker can be copied/moved to (without the source)."""
        return [n for n in self._watchlist_names(editable_only=True) if n != self._current_watchlist]

    def _forget_pending_watchlist(self, name: str) -> None:
        """The list exists server-side now: drop the local-only marker."""
        if not name:
            return
        self._pending_watchlists = [n for n in self._pending_watchlists if n != name]
        for entry in self._watchlists:
            if str(entry.get("name") or "") == name:
                entry.pop("pending", None)
        self._populate_watchlist_combo()

    def _on_watchlist_activated(self, index: int) -> None:
        if self._populating or index < 0:
            return
        name = str(self.watchlist_combo.itemData(index) or self.watchlist_combo.itemText(index))
        self._select_watchlist(name, reload_content=True)

    def _commit_watchlist_text(self) -> None:
        """Enter in the editable combo: resolve typed text to a known list."""
        text = self.watchlist_combo.currentText().strip()
        if not text:
            return
        match = None
        for entry in self._watchlists:
            if str(entry.get("name", "")).lower() == text.lower():
                match = str(entry["name"])
                break
        if match is None:
            for entry in self._watchlists:
                if str(entry.get("name", "")).lower().startswith(text.lower()):
                    match = str(entry["name"])
                    break
        if match is None:
            self._set_status(f"⚠ Watchlist '{text}' nicht gefunden", error=True)
            return
        index = self.watchlist_combo.findData(match)
        if index >= 0:
            self.watchlist_combo.setCurrentIndex(index)
        self._select_watchlist(match, reload_content=True)

    def _select_watchlist(self, name: str, reload_content: bool = True) -> None:
        index = self.watchlist_combo.findData(name)
        if index >= 0 and self.watchlist_combo.currentIndex() != index:
            self._populating = True
            try:
                self.watchlist_combo.setCurrentIndex(index)
            finally:
                self._populating = False
        self._current_watchlist = name
        entry = next((w for w in self._watchlists if str(w.get("name")) == name), {"editable": True})
        self._watchlist_editable = bool(entry.get("editable", True))
        self._populate_transfer_combo()
        if reload_content:
            with self._selection_guard():
                self.watchlist_list.clear()
            self.watchlist_info.setText("lade…")
            if self._connected:
                self._request("get_watchlist", {"list_name": name}, {"list_name": name})
        self._update_buttons()

    def _on_watchlist_response(self, data: Dict[str, Any]) -> None:
        name = str(data.get("list_name") or "")
        if name and name != self._current_watchlist:
            return  # stale response for a previously selected list
        self._watchlist_editable = bool(data.get("editable", True))
        tickers = [t for t in (data.get("tickers") or []) if isinstance(t, dict)]
        with self._selection_guard():
            self.watchlist_list.clear()
            for row in tickers:
                ticker = str(row.get("ticker") or "")
                if not ticker:
                    continue
                item = QListWidgetItem(ticker)
                item.setData(Qt.ItemDataRole.UserRole, ticker)
                self.watchlist_list.addItem(item)

        count = int(data.get("count") or len(tickers))
        if data.get("truncated"):
            self.watchlist_info.setText(f"{count} Ticker · schreibgeschützt")
            self._set_status(f"Master-Universe: {count} Ticker (nicht editierbar)")
        else:
            self.watchlist_info.setText(f"{count} Ticker")
        self._update_buttons()

    def _on_watchlist_selection_changed(self) -> None:
        """Marking a ticker in the list is enough - no double click needed."""
        self._update_buttons()
        if self._suppress_selection_activation or self._populating:
            return
        self._activate_watchlist_symbol()

    def _remove_row(self, ticker: str) -> None:
        for row in range(self.watchlist_list.count()):
            item = self.watchlist_list.item(row)
            if item is not None and str(item.data(Qt.ItemDataRole.UserRole)) == ticker:
                with self._selection_guard():
                    self.watchlist_list.takeItem(row)
                break

    def _copy_selected_ticker(self) -> None:
        self._transfer_selected_ticker("copy", ask=_shift_pressed())

    def _move_selected_ticker(self) -> None:
        self._transfer_selected_ticker("move", ask=_shift_pressed())

    # Kopieren/Verschieben: das Ziel steht in transfer_target_combo und wird gemerkt,
    # damit C/V (bzw. Copy/Move) ohne Dialog arbeiten. Shift fragt nach einem Ziel.

    def _populate_transfer_combo(self) -> None:
        if not hasattr(self, "transfer_target_combo"):
            return
        targets = self._transfer_targets()
        wanted = self._transfer_target() or str(self._settings.value("panel/transfer_target", "") or "")
        previous = self._populating
        self._populating = True
        try:
            self.transfer_target_combo.clear()
            for name in targets:
                self.transfer_target_combo.addItem(name, name)
            index = self.transfer_target_combo.findData(wanted) if wanted else -1
            if index < 0 and targets:
                index = 0
            self.transfer_target_combo.setCurrentIndex(index)
        finally:
            self._populating = previous
        self.transfer_target_combo.setEnabled(self._connected and bool(targets))

    def _transfer_target(self) -> str:
        if not hasattr(self, "transfer_target_combo"):
            return ""
        index = self.transfer_target_combo.currentIndex()
        if index < 0:
            return ""
        return str(self.transfer_target_combo.itemData(index) or "")

    def _set_transfer_target(self, name: str) -> None:
        """Ziel merken (Combo + QSettings) und an die Spitze der Zuletzt-Liste setzen."""
        if not name:
            return
        self._recent_targets = [name] + [n for n in self._recent_targets if n != name][:4]
        self._settings.setValue("panel/transfer_target", name)
        index = self.transfer_target_combo.findData(name)
        if index >= 0:
            previous = self._populating
            self._populating = True
            try:
                self.transfer_target_combo.setCurrentIndex(index)
            finally:
                self._populating = previous

    def _on_transfer_target_activated(self, index: int) -> None:
        if self._populating or index < 0:
            return
        self._set_transfer_target(str(self.transfer_target_combo.itemData(index) or ""))
        # Zurueck in die Liste, damit gleich weiter mit C/V gearbeitet werden kann.
        self.watchlist_list.setFocus(Qt.FocusReason.OtherFocusReason)

    def _focus_transfer_target(self) -> None:
        if not self.transfer_target_combo.isEnabled():
            return
        self.transfer_target_combo.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.transfer_target_combo.showPopup()

    def _transfer_selected_ticker(self, action: str, ask: bool = False, target: str = "") -> None:
        """Copy/move the selected row into the target watchlist.

        Without an explicit ``target`` the remembered target (transfer_target_combo)
        is used; ``ask`` (Shift) or a missing target opens the picker dialog. The
        marker then moves on to the next ticker so a list can be worked through
        with repeated C/V presses.
        """
        item = self.watchlist_list.currentItem()
        if item is None or not self._current_watchlist or not self._watchlist_editable:
            return
        ticker = str(item.data(Qt.ItemDataRole.UserRole) or item.text()).strip()
        if not ticker:
            return
        targets = self._transfer_targets()
        if not targets:
            self._set_status("⚠ Keine andere Watchlist als Ziel vorhanden", error=True)
            return
        verb = "kopieren" if action == "copy" else "verschieben"
        if target not in targets:
            target = "" if ask else self._transfer_target()
        if not target:
            target = watchlist_dialogs.ask_watchlist(
                self,
                f"Ticker {verb}",
                f"Ziel-Watchlist für {ticker}:",
                self._ordered_targets(),
                preselect=self._transfer_target(),
            )
            if not target:
                return
        self._set_transfer_target(target)
        self._set_status(f"{verb} {ticker} nach {target}…")
        # Die Liste vor dem Request weiterschalten: die Antwort kann synchron kommen
        # (und die Zeile dann schon entfernen oder die Liste neu laden).
        row = self.watchlist_list.row(item)
        if action == "move":
            # Sofort aus der Liste nehmen: ein zweites V trifft so schon den naechsten
            # Ticker statt denselben noch einmal (ein Fehler laedt die Liste neu).
            with self._selection_guard():
                self.watchlist_list.takeItem(row)
                self.watchlist_list.setCurrentRow(-1)
            next_row = min(row, self.watchlist_list.count() - 1)
        else:
            next_row = row + 1 if row + 1 < self.watchlist_list.count() else -1
        if next_row >= 0:
            # Ohne Guard: der naechste Ticker laedt wie bei ↑/↓ in den Chart.
            self.watchlist_list.setCurrentRow(next_row)
        self._request(
            "mutate_watchlist",
            {
                "action": action,
                "list_name": self._current_watchlist,
                "ticker": ticker,
                "target_list": target,
            },
            {
                "kind": action,
                "ticker": ticker,
                "list_name": self._current_watchlist,
                "target_list": target,
            },
        )
        self._update_buttons()

    def _ordered_targets(self) -> List[str]:
        """Transfer targets, recently used ones first."""
        targets = self._transfer_targets()
        recent = [n for n in self._recent_targets if n in targets]
        return recent + [n for n in targets if n not in recent]

    def _build_watchlist_context_menu(self) -> Optional[QMenu]:
        """Rechtsklick-Menue einer Watchlist-Zeile: Copy to / Move to / Remove."""
        if self.watchlist_list.currentItem() is None or not self._watchlist_editable:
            return None
        menu = QMenu(self)
        targets = self._ordered_targets()
        for label, action in (("Copy to", "copy"), ("Move to", "move")):
            submenu = menu.addMenu(label)
            submenu.setEnabled(bool(targets) and self._connected)
            recent = [n for n in self._recent_targets if n in targets]
            for index, name in enumerate(targets):
                if recent and index == len(recent):
                    submenu.addSeparator()
                entry = submenu.addAction(name)
                entry.triggered.connect(
                    lambda _checked=False, a=action, t=name: self._transfer_selected_ticker(a, target=t)
                )
        menu.addSeparator()
        remove = menu.addAction("Remove")
        remove.setEnabled(self._connected)
        remove.triggered.connect(self._remove_selected_ticker)
        return menu

    def _show_watchlist_context_menu(self, pos: QPoint) -> None:
        item = self.watchlist_list.itemAt(pos)
        if item is not None and item is not self.watchlist_list.currentItem():
            self.watchlist_list.setCurrentItem(item)
        menu = self._build_watchlist_context_menu()
        if menu is not None:
            menu.exec(self.watchlist_list.viewport().mapToGlobal(pos))

    def _create_watchlist(self) -> None:
        """Ask for a new watchlist (name and optional source) and create it."""
        existing = self._watchlist_names()
        result = watchlist_dialogs.ask_new_watchlist(
            self,
            existing,
            self._watchlist_names(editable_only=True),
        )
        if result is None:
            return
        name, source = result
        if not name:
            return
        if name in existing:
            self._select_watchlist(name, reload_content=True)
            self._set_status(f"Watchlist '{name}' existiert bereits")
            return
        if source:
            self._set_status(f"erstelle Watchlist {name} aus {source}…")
            self._request(
                "mutate_watchlist",
                {"action": "copy_list", "list_name": source, "target_list": name},
                {"kind": "copy_list", "list_name": source, "target_list": name},
            )
            return
        # A list without tickers has no row in pca_watchlists: keep it locally
        # until the first ticker is added, then a refresh picks it up from the DB.
        if name not in self._pending_watchlists:
            self._pending_watchlists.append(name)
        if name not in existing:
            self._watchlists.append({"name": name, "editable": True, "pending": True})
        self._populate_watchlist_combo()
        self._select_watchlist(name, reload_content=False)
        with self._selection_guard():
            self.watchlist_list.clear()
        self.watchlist_info.setText("0 Ticker · neu")
        self._set_status(f"Watchlist '{name}' angelegt – der erste Ticker speichert sie")
        self._update_buttons()

    def _delete_watchlist(self) -> None:
        """Ask which watchlist to remove and delete it after confirmation."""
        pending = list(self._pending_watchlists)
        stored = [n for n in self._watchlist_names(editable_only=True) if n not in pending]
        choices = pending + stored
        if not choices:
            self._set_status("⚠ Keine Watchlist zum Löschen vorhanden", error=True)
            return
        name = watchlist_dialogs.ask_watchlist(
            self,
            "Watchlist löschen",
            "Watchlist:",
            choices,
            preselect=self._current_watchlist,
        )
        if not name:
            return
        if name in self._pending_watchlists:
            self._discard_pending_watchlist(name)
            self._set_status(f"− Watchlist '{name}' verworfen (war noch leer)")
            return
        if not watchlist_dialogs.confirm_delete(self, name):
            return
        self._set_status(f"lösche Watchlist {name}…")
        self._request(
            "mutate_watchlist",
            {"action": "delete", "list_name": name},
            {"kind": "delete", "list_name": name},
        )

    def _discard_pending_watchlist(self, name: str) -> None:
        """Drop a list that only ever existed in this panel session."""
        was_current = name == self._current_watchlist
        self._pending_watchlists = [n for n in self._pending_watchlists if n != name]
        self._watchlists = [w for w in self._watchlists if str(w.get("name") or "") != name]
        self._populate_watchlist_combo()
        if was_current:
            self._current_watchlist = ""
            self._watchlist_editable = False
            with self._selection_guard():
                self.watchlist_list.clear()
            self.watchlist_info.setText("")
            remaining = self._watchlist_names()
            if remaining:
                self._select_watchlist(remaining[0], reload_content=True)
        self._update_buttons()

    def _add_selected_ticker(self) -> None:
        ticker = self._selected_search_ticker()
        if not ticker or not self._current_watchlist or not self._watchlist_editable:
            return
        self._set_status(f"füge {ticker} zu {self._current_watchlist} hinzu…")
        self.add_btn.setEnabled(False)
        self._request(
            "mutate_watchlist",
            {"action": "add", "list_name": self._current_watchlist, "ticker": ticker},
            {"kind": "add", "ticker": ticker, "list_name": self._current_watchlist},
        )

    def _remove_selected_ticker(self) -> None:
        item = self.watchlist_list.currentItem()
        if item is None or not self._current_watchlist or not self._watchlist_editable:
            return
        ticker = str(item.data(Qt.ItemDataRole.UserRole) or item.text())
        self._set_status(f"entferne {ticker} aus {self._current_watchlist}…")
        self.remove_btn.setEnabled(False)
        self._request(
            "mutate_watchlist",
            {"action": "remove", "list_name": self._current_watchlist, "ticker": ticker},
            {"kind": "remove", "ticker": ticker, "list_name": self._current_watchlist},
        )

    def _on_mutation_response(self, meta: Dict[str, Any], data: Dict[str, Any]) -> None:
        kind = str(meta.get("kind") or "")
        ticker = str(meta.get("ticker") or data.get("ticker") or "")
        list_name = str(meta.get("list_name") or data.get("list_name") or "")
        status = str(data.get("status") or "")
        count = data.get("count")

        target = str(meta.get("target_list") or data.get("target_list") or "")
        target_count = data.get("target_count")

        if kind == "delete":
            self._forget_pending_watchlist(list_name)
            if self._current_watchlist == list_name:
                self._current_watchlist = ""
                self._watchlist_editable = False
                with self._selection_guard():
                    self.watchlist_list.clear()
                self.watchlist_info.setText("")
            self._set_status(f"− Watchlist '{list_name}' gelöscht")
            self.refresh_watchlists()
            return

        if kind == "copy_list":
            self._forget_pending_watchlist(target)
            copied = count if isinstance(count, int) else "?"
            self._set_status(f"＋ Watchlist '{target}' aus '{list_name}' angelegt ({copied} Ticker)")
            self.refresh_watchlists(select=target)
            return

        if kind in ("copy", "move"):
            self._forget_pending_watchlist(target)
            already = " – war dort bereits vorhanden" if status == "already_exists" else ""
            if kind == "move":
                self._remove_row(ticker)
                if isinstance(count, int):
                    self.watchlist_info.setText(f"{count} Ticker")
                self._set_status(f"⇄ {ticker} nach {target} verschoben{already}")
            else:
                extra = f" ({target_count} Ticker)" if isinstance(target_count, int) else ""
                self._set_status(f"⧉ {ticker} nach {target} kopiert{extra}{already}")
            self._update_buttons()
            return

        if kind == "remove":
            self._remove_row(ticker)
            self._set_status(f"− {ticker} aus {list_name} entfernt")
            self._forget_pending_watchlist(list_name)
        else:
            self._forget_pending_watchlist(list_name)
            if status == "already_exists":
                self._set_status(f"{ticker} ist bereits in {list_name}")
            else:
                existing = {
                    str(self.watchlist_list.item(i).data(Qt.ItemDataRole.UserRole))
                    for i in range(self.watchlist_list.count())
                    if self.watchlist_list.item(i) is not None
                }
                if ticker and ticker not in existing and list_name == self._current_watchlist:
                    item = QListWidgetItem(ticker)
                    item.setData(Qt.ItemDataRole.UserRole, ticker)
                    self.watchlist_list.addItem(item)
                self._set_status(f"＋ {ticker} zu {list_name} hinzugefügt")

        if isinstance(count, int) and list_name == self._current_watchlist:
            self.watchlist_info.setText(f"{count} Ticker")
        self._update_buttons()

    def _activate_watchlist_symbol(self) -> None:
        item = self.watchlist_list.currentItem()
        if item is None:
            return
        ticker = str(item.data(Qt.ItemDataRole.UserRole) or item.text()).strip()
        if ticker:
            self.symbol_activated.emit(ticker, self.flag_btn.get_flag(), self._preset_id)

    # ── presets ────────────────────────────────────────────────────────────

    def _on_preset_activated(self, index: int) -> None:
        if self._populating or index < 0:
            return
        preset_id = str(self.preset_combo.itemData(index) or "")
        if not preset_id:
            return
        self._preset_id = preset_id
        self._settings.setValue("panel/preset", preset_id)
        if not self._connected:
            self._set_status(f"Preset {preset_id} gemerkt (nicht verbunden)", error=True)
            return
        self._set_status(f"wende Preset {preset_id} an…")
        self._request(
            "apply_preset",
            {"preset_id": preset_id, "color_flag": self.flag_btn.get_flag()},
            {"preset_id": preset_id},
        )

    def _on_apply_preset_response(self, data: Dict[str, Any]) -> None:
        preset_id = str(data.get("preset_id") or self._preset_id)
        applied = data.get("applied") or []
        skipped = data.get("skipped") or []
        # Das Fenster rendert jetzt ein anderes Chart: der Builder liest den neuen
        # Stand (Panes/Overrides) sofort neu, statt das alte Chart weiterzuzeigen.
        if self._builder_target and self._builder_target in applied:
            self._refresh_chart_state()
        if applied:
            self._set_status(f"Preset {preset_id}: {len(applied)} Chart(s) aktualisiert")
        else:
            reason = ""
            if skipped:
                reason = f" ({skipped[0].get('reason', '')})"
            self._set_status(
                f"Preset {preset_id} gemerkt – kein Chart mit Flag {self.flag_btn.get_flag()} offen{reason}"
            )

    # ── chart builder ──────────────────────────────────────────────────────

    def _refresh_chart_state(self) -> None:
        """Re-read the server-side chart state (single source of truth).

        Mehrere Ausloeser im selben Moment (Anwenden, Snapshot, Zielwechsel) ergeben
        eine Anfrage; kommt waehrend der Antwort ein weiterer Ausloeser dazu, wird
        danach genau einmal nachgelesen (siehe _chart_state_pending_more).
        """
        target = self._builder_target
        if not target or not self._connected:
            return
        for meta in self._pending.values():
            if meta.get("op") == "get_chart_state" and meta.get("window_id") == target:
                # Schon unterwegs - die Antwort kann aber einen Stand tragen, der
                # vor der letzten Aenderung entstanden ist. Danach einmal nachlesen.
                self._chart_state_pending_more = True
                return
        self._chart_state_pending_more = False
        self._request("get_chart_state", {"window_id": target}, {"window_id": target})

    def notify_window_content_changed(self, window_id: str) -> None:
        """A chart window got new content - re-read it if it is the builder target.

        The agent rebuilds windows on its own (MCP apply/compose, symbol change,
        setup restore). Without this the builder would keep showing the components
        of the previously rendered chart, while the window already draws another.
        """
        window_id = str(window_id or "")
        if not window_id or window_id != self._builder_target:
            return
        self._refresh_chart_state()

    def _on_panes_response(self, data: Dict[str, Any]) -> None:
        panes: List[Dict[str, Any]] = []
        for raw in data.get("panes") or []:
            if not isinstance(raw, dict):
                continue
            pane_id = str(raw.get("id") or "")
            if not pane_id:
                continue
            panes.append(dict(raw))
        self._pane_catalog = panes
        self._populate_add_pane_combo()
        self._rebuild_builder_widgets()

    def _on_charts_response(self, data: Dict[str, Any]) -> None:
        """Chart-Katalog: speist Anwenden-Liste und Builder aus derselben Quelle.

        Die Liste "GESPEICHERTES CHART ANWENDEN" ist der Chart-Bestand - nicht ein
        zweiter, aliasbehafteter Preset-Bestand. Dadurch gilt: was anwendbar ist,
        kennt der Builder, und was der Builder speichert, ist sofort anwendbar.
        """
        self._chart_catalog = [
            dict(c)
            for c in (data.get("charts") or [])
            if isinstance(c, dict) and str(c.get("id") or "")
        ]
        previous = self._populating
        self._populating = True
        try:
            self.preset_combo.clear()
            for chart in self._chart_catalog:
                chart_id = str(chart.get("id") or "")
                label = str(chart.get("display_name") or chart_id)
                self.preset_combo.addItem(label, chart_id)
                summary = str(chart.get("summary") or "").strip()
                tooltip = f"{chart_id} · {int(chart.get('pane_count') or 0)} Panes"
                if summary:
                    tooltip = f"{tooltip}\n{summary}"
                self.preset_combo.setItemData(
                    self.preset_combo.count() - 1, tooltip, Qt.ItemDataRole.ToolTipRole
                )
        finally:
            self._populating = previous
        self._show_active_chart_in_combo()

    def _show_active_chart_in_combo(self) -> None:
        """Anwenden-Liste auf das Chart stellen, das das Zielfenster zeigt.

        Ein eigener Override (`_preset_id`) hat Vorrang - er gilt fuer den naechsten
        Symbolwechsel. Ohne Override zeigt die Liste den Ist-Stand des Fensters,
        damit Anwenden-Liste und Builder nicht auseinanderlaufen.
        """
        if not hasattr(self, "preset_combo"):
            return
        override = self._preset_id or str(self._settings.value("panel/preset", "") or "")
        display_id = override or str(self._chart_state.get("chart_id") or "")
        if not display_id and self.preset_combo.count():
            display_id = str(self.preset_combo.itemData(0) or "")
        index = self.preset_combo.findData(display_id) if display_id else -1
        if index < 0:
            return
        previous = self._populating
        self._populating = True
        try:
            self.preset_combo.setCurrentIndex(index)
        finally:
            self._populating = previous
        if override:
            self._preset_id = override

    def _on_chart_state_response(self, data: Dict[str, Any], meta: Dict[str, Any]) -> None:
        window_id = str(data.get("window_id") or meta.get("window_id") or "")
        if window_id != self._builder_target:
            return  # stale: the builder target moved on
        self._chart_state = dict(data)
        symbol = str(data.get("symbol") or "")
        if symbol:
            self._chart_symbols[window_id] = symbol
        if "draft" in data:
            self._builder_draft = bool(data.get("draft"))
        if isinstance(data.get("panes"), list):
            self._builder_panes = [
                self._normalize_pane(pane)
                for pane in data.get("panes") or []
                if isinstance(pane, dict)
            ]
        self._rebuild_builder_widgets()
        self._show_active_chart_in_combo()

        if self._chart_state_pending_more:
            # Waehrend die Antwort lief, kam eine weitere Aenderung an: jetzt den
            # aktuellen Stand nachlesen (einmal, kein Loop).
            self._chart_state_pending_more = False
            self._refresh_chart_state()

        if str(meta.get("intent") or "") == "discard":
            chart_id = str(self._chart_state.get("chart_id") or "")
            if chart_id:
                self._request(
                    "apply_chart",
                    {"window_id": window_id, "chart_id": chart_id},
                    {"window_id": window_id, "chart_id": chart_id},
                )
            else:
                self._set_status("verworfen – dieses Chart ist noch nicht gespeichert")

    @staticmethod
    def _warning_suffix(data: Dict[str, Any]) -> str:
        """Warnungen/Uebersprungenes als Statusanhang (Plan Phase 4)."""
        warnings = data.get("warnings") or []
        skipped = data.get("skipped") or []
        parts = []
        if skipped:
            parts.append(
                "übersprungen: "
                + ", ".join(str(s.get("pane_preset_id") or s.get("symbol") or "?") for s in skipped)
            )
        if warnings:
            parts.append(
                "Warnungen: "
                + ", ".join(
                    str(w.get("detail") or w.get("code") or "?") for w in warnings[:4]
                )
            )
        return (" · ⚠ " + "; ".join(parts)) if parts else ""

    def _on_compose_response(self, meta: Dict[str, Any], data: Dict[str, Any]) -> None:
        panes = data.get("panes") if isinstance(data.get("panes"), list) else self._builder_panes
        self._set_status(
            f"Vorschau aktualisiert ({len(panes)} Panes){self._warning_suffix(data)}"
        )
        self._refresh_chart_state()

    def _on_apply_chart_response(self, meta: Dict[str, Any], data: Dict[str, Any]) -> None:
        chart_id = str(meta.get("chart_id") or data.get("chart_id") or "")
        self._set_status(f"Chart {chart_id} geladen{self._warning_suffix(data)}")
        self._refresh_chart_state()

    def _on_save_chart_response(self, meta: Dict[str, Any], data: Dict[str, Any]) -> None:
        chart_id = str(meta.get("chart_id") or data.get("chart_id") or "")
        display = str(meta.get("display_name") or data.get("display_name") or "")
        label = display or chart_id
        self._set_status(f"✓ Chart '{label}' gespeichert ({chart_id})")
        self.refresh_charts()
        self._refresh_chart_state()

    def _compose(self, panes: Optional[List[Dict[str, Any]]] = None) -> None:
        """Render the current pane list immediately (preview/draft)."""
        target = self._builder_target
        if not target:
            self._set_status("⚠ kein Chartfenster als Ziel", error=True)
            return
        if not self._connected:
            self._set_status("⚠ nicht verbunden", error=True)
            return
        params: Dict[str, Any] = {
            "window_id": target,
            "panes": self._compose_panes(panes),
            "draft": True,
        }
        base = str(self._chart_state.get("chart_id") or "")
        if base:
            params["base_chart_id"] = base
        self._request("compose_chart", params, {"window_id": target})

    def _compose_panes(self, panes: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        rows = panes if panes is not None else self._builder_panes
        payload: List[Dict[str, Any]] = []
        for pane in rows:
            entry: Dict[str, Any] = {
                "pane_id": str(pane.get("pane_id") or ""),
                "pane_preset_id": str(pane.get("pane_preset_id") or ""),
                "scale": str(pane.get("scale") or "linear"),
                "weight": int(pane.get("weight") or 2),
            }
            # Pane-Overrides (Titel, Serien-Stil, ...) reisen mit: sie gehoeren zur
            # Chart-Ebene und nicht ins geteilte Pane-Preset.
            if isinstance(pane.get("overrides"), dict) and pane["overrides"]:
                entry["overrides"] = dict(pane["overrides"])
            payload.append(entry)
        return payload

    def _on_price_pane_activated(self, index: int) -> None:
        if self._populating or index < 0:
            return
        pane_preset_id = str(self.price_pane_combo.itemData(index) or "")
        if not pane_preset_id or pane_preset_id == self._main_preset_id():
            return
        preset = self._pane_preset(pane_preset_id)
        panes = [dict(pane) for pane in self._builder_panes]
        for pane in panes:
            if str(pane.get("pane_id")) == "main":
                pane["pane_preset_id"] = pane_preset_id
                break
        else:
            # No price pane yet (old chart): the price pane is always "main".
            panes.insert(
                0,
                {
                    "pane_id": "main",
                    "pane_preset_id": pane_preset_id,
                    "title": str(preset.get("display_name") or pane_preset_id),
                    "role": "price",
                    "scale": "linear",
                    "weight": 4,
                },
            )
        self._builder_panes = panes
        self._rebuild_builder_widgets()
        self._compose()

    def _on_pane_preset_activated(self, index: int) -> None:
        """Pane-Preset der markierten Pane wechseln (Draft, Slot-Id bleibt)."""
        if self._populating or index < 0:
            return
        row = self._selected_pane_index()
        if row < 0:
            return
        pane_preset_id = str(self.pane_preset_combo.itemData(index) or "")
        pane = self._builder_panes[row]
        if not pane_preset_id or pane_preset_id == str(pane.get("pane_preset_id") or ""):
            return
        if self._pane_is_price(pane):
            # Das Preispane wechselt ueber das Preispane-Dropdown; Auswahl zurueckdrehen.
            self._sync_pane_editor()
            self._set_status("⚠ das Preispane wird oben gewechselt", error=True)
            return
        preset = self._pane_preset(pane_preset_id)
        if not preset:
            self._set_status(f"⚠ Pane-Preset '{pane_preset_id}' unbekannt", error=True)
            self._sync_pane_editor()
            return
        old_preset = self._pane_preset(str(pane.get("pane_preset_id") or ""))
        panes = [dict(p) for p in self._builder_panes]
        target = panes[row]
        target["scale"] = self._scale_after_preset_swap(target, old_preset, preset)
        # Slot-Id bleibt: sie ist der Schluessel in pca_chart_preset_panes, in den
        # Overrides und in x_axis_pane. Nur das referenzierte Preset wandert.
        target["pane_preset_id"] = pane_preset_id
        target["role"] = str(preset.get("role") or "any")
        overrides = dict(target.get("overrides") or {})
        # Ein scale-Override beschreibt die Default-Skala des ALTEN Presets und faellt
        # mit ihm. Titel und Serien-Overrides bleiben: Serien, die das neue Preset
        # nicht mehr hat, melden sich als Warnung in der Statuszeile.
        overrides.pop("scale", None)
        target["title"] = str(overrides.get("title") or preset.get("display_name") or pane_preset_id)
        if overrides:
            target["overrides"] = overrides
        else:
            target.pop("overrides", None)
        self._builder_panes = panes
        self._rebuild_builder_widgets()
        self._select_pane_row(row)
        self._compose()

    @staticmethod
    def _scale_after_preset_swap(
        pane: Dict[str, Any], old_preset: Dict[str, Any], new_preset: Dict[str, Any]
    ) -> str:
        """Skala beim Preset-Wechsel: nur mitziehen, wenn sie noch der Default war.

        Hat der Nutzer LIN/LOG bewusst gesetzt, bleibt seine Wahl stehen.
        """
        current = str(pane.get("scale") or "linear").strip().lower()
        old_default = str(old_preset.get("default_scale") or "linear").strip().lower()
        if current != old_default:
            return current if current in ("linear", "log") else "linear"
        new_default = str(new_preset.get("default_scale") or "linear").strip().lower()
        return new_default if new_default in ("linear", "log") else "linear"

    def _on_add_pane_clicked(self) -> None:
        pane_preset_id = self._resolve_add_pane_id()
        if not pane_preset_id:
            self._set_status("⚠ Pane-Preset wählen", error=True)
            return
        preset = self._pane_preset(pane_preset_id)
        scale = str(preset.get("default_scale") or "linear").strip().lower()
        if scale not in ("linear", "log"):
            scale = "linear"
        pane = {
            "pane_id": self._unique_pane_id(pane_preset_id),
            "pane_preset_id": pane_preset_id,
            "title": str(preset.get("display_name") or pane_preset_id),
            "role": str(preset.get("role") or "any"),
            "scale": scale,
            "weight": 2,
        }
        self._builder_panes = [dict(p) for p in self._builder_panes] + [pane]
        self.add_pane_combo.setCurrentIndex(-1)
        self.add_pane_combo.setEditText("")
        self._rebuild_builder_widgets()
        self._select_pane_row(len(self._builder_panes) - 1)
        self._compose()

    def _on_remove_pane_clicked(self) -> None:
        row = self._selected_pane_index()
        if row < 0:
            self._set_status("⚠ keine Pane markiert", error=True)
            return
        pane = self._builder_panes[row]
        if str(pane.get("pane_id")) == "main" or str(pane.get("role")) == "price":
            self._set_status("⚠ das Preispane kann nicht entfernt werden", error=True)
            return
        self._builder_panes = [
            dict(p) for index, p in enumerate(self._builder_panes) if index != row
        ]
        self._rebuild_builder_widgets()
        self._select_pane_row(min(row, len(self._builder_panes) - 1))
        self._compose()

    def _move_selected_pane(self, delta: int) -> None:
        row = self._selected_pane_index()
        destination = row + delta
        if row < 0 or destination < 0 or destination >= len(self._builder_panes):
            return
        panes = [dict(p) for p in self._builder_panes]
        panes[row], panes[destination] = panes[destination], panes[row]
        self._builder_panes = panes
        self._rebuild_builder_widgets()
        self._select_pane_row(destination)
        self._compose()

    def _toggle_selected_pane_scale(self) -> None:
        row = self._selected_pane_index()
        if row < 0:
            self._set_status("⚠ keine Pane markiert", error=True)
            return
        panes = [dict(p) for p in self._builder_panes]
        pane = panes[row]
        pane["scale"] = "linear" if str(pane.get("scale")) == "log" else "log"
        self._builder_panes = panes
        self._rebuild_builder_widgets()
        self._select_pane_row(row)
        self._compose()

    def _selected_pane(self) -> Optional[Dict[str, Any]]:
        row = self._selected_pane_index()
        if row < 0 or row >= len(self._builder_panes):
            return None
        return self._builder_panes[row]

    def _on_pane_selection_changed(self) -> None:
        self._sync_pane_editor()

    def _sync_pane_editor(self) -> None:
        """Preset/Hoehe/Titel/Duplizieren an die markierte Pane anpassen."""
        if not hasattr(self, "pane_weight_spin"):
            return
        pane = self._selected_pane()
        editable = bool(self._connected) and bool(self._builder_target)
        previous = self._populating
        self._populating = True
        try:
            self._show_scale(pane)
            if pane is None:
                self.pane_weight_spin.setValue(2)
                self.pane_title_edit.setText("")
                self.pane_weight_spin.setEnabled(False)
                self.pane_title_edit.setEnabled(False)
                self.duplicate_pane_btn.setEnabled(False)
                self._set_pane_preset_selection("")
                return
            self.pane_weight_spin.setValue(int(pane.get("weight") or 2))
            self.pane_title_edit.setText(str(pane.get("title") or ""))
            is_price = self._pane_is_price(pane)
            self.pane_weight_spin.setEnabled(editable)
            self.pane_title_edit.setEnabled(editable)
            self.duplicate_pane_btn.setEnabled(editable and not is_price)
            self._set_pane_preset_selection(
                str(pane.get("pane_preset_id") or ""),
                pane=pane,
                selectable=editable and not is_price,
            )
        finally:
            self._populating = previous

    def _show_scale(self, pane: Optional[Dict[str, Any]]) -> None:
        """Der Skala-Knopf zeigt die aktive Skala der markierten Pane (LIN oder LOG)."""
        is_log = pane is not None and str(pane.get("scale")) == "log"
        self.scale_toggle_btn.setText("LOG" if is_log else "LIN")
        self.scale_toggle_btn.setProperty("log", "true" if is_log else "false")
        self.scale_toggle_btn.style().unpolish(self.scale_toggle_btn)
        self.scale_toggle_btn.style().polish(self.scale_toggle_btn)

    def _set_pane_preset_selection(
        self,
        pane_preset_id: str,
        pane: Optional[Dict[str, Any]] = None,
        selectable: bool = False,
    ) -> None:
        """Preset-Combo an die markierte Pane anpassen.

        Ein archiviertes oder unbekanntes Preset bleibt als eigener Eintrag sichtbar -
        sonst zeigt das Feld ein anderes Preset als das Chart rendert. Das Preispane
        zeigt sein Preset nur an: gewechselt wird es oben.
        """
        combo = self.pane_preset_combo
        index = combo.findData(pane_preset_id) if pane_preset_id else -1
        if pane_preset_id and index < 0:
            combo.addItem(self._pane_display_name(pane or {}), pane_preset_id)
            index = combo.count() - 1
        combo.setCurrentIndex(index)
        combo.setEnabled(selectable)

    def _on_pane_weight_changed(self) -> None:
        if self._populating:
            return
        row = self._selected_pane_index()
        if row < 0:
            return
        weight = int(self.pane_weight_spin.value())
        if int(self._builder_panes[row].get("weight") or 0) == weight:
            return
        panes = [dict(pane) for pane in self._builder_panes]
        panes[row]["weight"] = weight
        self._builder_panes = panes
        self._rebuild_builder_widgets()
        self._compose()

    def _on_pane_title_changed(self) -> None:
        if self._populating:
            return
        row = self._selected_pane_index()
        if row < 0:
            return
        title = str(self.pane_title_edit.text() or "").strip()
        panes = [dict(pane) for pane in self._builder_panes]
        pane = panes[row]
        if str(pane.get("title") or "") == title and not title:
            return
        overrides = dict(pane.get("overrides") or {})
        if title:
            overrides["title"] = title
        else:
            overrides.pop("title", None)
        pane["title"] = title
        if overrides:
            pane["overrides"] = overrides
        else:
            pane.pop("overrides", None)
        self._builder_panes = panes
        self._rebuild_builder_widgets()
        self._compose()

    def _on_duplicate_pane_clicked(self) -> None:
        pane = self._selected_pane()
        if pane is None:
            self._set_status("⚠ keine Pane markiert", error=True)
            return
        if str(pane.get("role")) == "price" or str(pane.get("pane_id")) == "main":
            self._set_status("⚠ das Preispane kann nicht dupliziert werden", error=True)
            return
        copy = dict(pane)
        copy["pane_id"] = self._unique_pane_id(str(pane.get("pane_preset_id") or "pane"))
        overrides = dict(pane.get("overrides") or {})
        if str(pane.get("title") or ""):
            overrides["title"] = str(pane["title"])
        if overrides:
            copy["overrides"] = overrides
        panes = [dict(p) for p in self._builder_panes] + [copy]
        self._builder_panes = panes
        self._rebuild_builder_widgets()
        self._select_pane_row(len(panes) - 1)
        self._compose()

    def _on_volume_toggled(self, checked: bool) -> None:
        if self._populating:
            return
        panes = [dict(p) for p in self._builder_panes]
        has_volume = any(str(p.get("pane_preset_id")) == "builtin:volume" for p in panes)
        if checked and not has_volume:
            panes.append(
                {
                    "pane_id": self._unique_pane_id("volume"),
                    "pane_preset_id": "builtin:volume",
                    "title": BUILTIN_PANE_NAMES["builtin:volume"],
                    "role": "volume",
                    "scale": "linear",
                    "weight": 2,
                }
            )
        elif not checked and has_volume:
            panes = [p for p in panes if str(p.get("pane_preset_id")) != "builtin:volume"]
        else:
            return
        self._builder_panes = panes
        self._rebuild_builder_widgets()
        self._compose()

    def _save_chart_as(self) -> None:
        target = self._builder_target
        if not target:
            self._set_status("⚠ kein Chartfenster als Ziel", error=True)
            return
        if not self._connected:
            self._set_status("⚠ nicht verbunden", error=True)
            return
        default = str(self._chart_state.get("chart_id") or "")
        if not default:
            default = f"{str(self._chart_symbols.get(target) or 'chart').lower()}_chart"
        raw = self._ask_chart_name(default)
        if not raw:
            return
        chart_id = derive_chart_id(raw)
        if not chart_id:
            self._set_status(f"⚠ '{raw}' ergibt keine gültige Chart-ID", error=True)
            return
        params: Dict[str, Any] = {"window_id": target, "chart_id": chart_id}
        if raw != chart_id:
            params["display_name"] = raw
        self._set_status(f"speichere Chart {chart_id}…")
        self._request(
            "save_chart",
            params,
            {"window_id": target, "chart_id": chart_id, "display_name": raw},
        )

    def _save_chart_in_place(self) -> None:
        """Entwurf im aktuellen Chart speichern - ohne Namensdialog (Plan Phase 7)."""
        target = self._builder_target
        if not target:
            self._set_status("⚠ kein Chartfenster als Ziel", error=True)
            return
        if not self._connected:
            self._set_status("⚠ nicht verbunden", error=True)
            return
        chart_id = str(self._chart_state.get("chart_id") or "")
        if not chart_id:
            self._save_chart_as()
            return
        display = str(self._chart_state.get("display_name") or "")
        params: Dict[str, Any] = {"window_id": target, "chart_id": chart_id}
        if display and display != chart_id:
            params["display_name"] = display
        self._set_status(f"speichere Chart {chart_id}…")
        self._request(
            "save_chart",
            params,
            {"window_id": target, "chart_id": chart_id, "display_name": display},
        )

    def _ask_chart_name(self, default: str) -> str:
        """Ask for a chart id (or a name to derive one from). Patched in tests."""
        text, ok = QInputDialog.getText(self, "Chart speichern", "Chart-ID oder Name:", text=default)
        if not ok:
            return ""
        return str(text or "").strip()

    def _discard_builder(self) -> None:
        """Reload the server state and render the last saved chart."""
        target = self._builder_target
        if not target:
            self._set_status("⚠ kein Chartfenster als Ziel", error=True)
            return
        if not self._connected:
            self._set_status("⚠ nicht verbunden", error=True)
            return
        self._set_status("verwerfe Änderungen…")
        self._request(
            "get_chart_state",
            {"window_id": target},
            {"window_id": target, "intent": "discard"},
        )

    def _pane_preset(self, pane_preset_id: str) -> Dict[str, Any]:
        for pane in self._pane_catalog:
            if str(pane.get("id") or "") == pane_preset_id:
                return pane
        return {}

    def _main_preset_id(self) -> str:
        for pane in self._builder_panes:
            if str(pane.get("pane_id")) == "main":
                return str(pane.get("pane_preset_id") or "")
        return ""

    def _pane_display_name(self, pane: Dict[str, Any]) -> str:
        """Anzeigename der Pane: der Titel-Override schlaegt den Preset-Namen.

        Sonst steht in der Zeile "RS-Monitor", waehrend das Chart "RS neu" rendert.
        """
        override = str((pane.get("overrides") or {}).get("title") or "").strip()
        if override:
            return override
        pane_preset_id = str(pane.get("pane_preset_id") or "")
        if pane_preset_id in BUILTIN_PANE_NAMES:
            return BUILTIN_PANE_NAMES[pane_preset_id]
        preset = self._pane_preset(pane_preset_id)
        if preset:
            return str(preset.get("display_name") or pane_preset_id)
        return str(pane.get("title") or pane_preset_id or pane.get("pane_id") or "?")

    def _pane_row_text(self, pane: Dict[str, Any]) -> str:
        """Kompakte Zeile: Slot · Name · Skala · Hoehe (h = Gewicht)."""
        scale = "LOG" if str(pane.get("scale")) == "log" else "LIN"
        return (
            f"{pane.get('pane_id')} · {self._pane_display_name(pane)} · {scale}"
            f" · h{pane.get('weight', 2)}"
        )

    def _normalize_pane(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        pane_preset_id = str(raw.get("pane_preset_id") or raw.get("pane_id") or "")
        pane_id = str(raw.get("pane_id") or pane_preset_id)
        scale = str(raw.get("scale") or "linear").strip().lower()
        if scale not in ("linear", "log"):
            scale = "linear"
        weight = raw.get("weight")
        try:
            weight = int(weight) if weight is not None else 2
        except (TypeError, ValueError):
            weight = 2
        # Overrides reisen mit (get_chart_state liefert sie): ohne sie verliert das
        # Panel Titel- und Serien-Overrides beim naechsten Compose still.
        overrides = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else {}
        override_title = str(overrides.get("title") or "").strip()
        return {
            "pane_id": pane_id,
            "pane_preset_id": pane_preset_id,
            "title": override_title or str(raw.get("title") or ""),
            "role": str(raw.get("role") or ""),
            "scale": scale,
            "weight": weight,
            "overrides": dict(overrides),
        }

    def _unique_pane_id(self, base: str) -> str:
        """pane_id = pane-preset id; collisions get the '_2' suffix (contract §1)."""
        used = {str(pane.get("pane_id") or "") for pane in self._builder_panes}
        if base not in used:
            return base
        suffix = 2
        while f"{base}_{suffix}" in used:
            suffix += 1
        return f"{base}_{suffix}"

    def _selected_pane_index(self) -> int:
        row = self.builder_pane_list.currentRow()
        if row < 0 or row >= len(self._builder_panes):
            return -1
        return row

    def _select_pane_row(self, row: int) -> None:
        if 0 <= row < self.builder_pane_list.count():
            self.builder_pane_list.setCurrentRow(row)

    @staticmethod
    def _pane_choice_label(pane: Dict[str, Any]) -> str:
        name = str(pane.get("display_name") or pane.get("id") or "")
        summary = str(pane.get("summary") or "").strip()
        return f"{name} — {summary}" if summary else name

    def _switchable_panes(self) -> List[Dict[str, Any]]:
        """Presets, die als regulaere Pane angehaengt oder gewechselt werden koennen.

        Ohne role=price (genau ein Preispane, Vertrag §1), nach Anzeigename sortiert -
        Anhaengen- und Wechsel-Dropdown zeigen so dieselbe Reihenfolge.
        """
        return sorted(
            (pane for pane in self._pane_catalog if not is_price_preset(pane)),
            key=lambda p: str(p.get("display_name") or p.get("id") or "").lower(),
        )

    def _populate_add_pane_combo(self) -> None:
        if not hasattr(self, "add_pane_combo"):
            return
        previous = self._populating
        self._populating = True
        try:
            self.add_pane_combo.clear()
            for pane in self._switchable_panes():
                pane_id = str(pane.get("id") or "")
                if not pane_id:
                    continue
                self.add_pane_combo.addItem(self._pane_choice_label(pane), pane_id)
            self.add_pane_combo.setCurrentIndex(-1)
            self.add_pane_combo.setEditText("")
        finally:
            self._populating = previous

    def _resolve_add_pane_id(self) -> str:
        """Resolve the add-combo selection, also for typed (editable) text."""
        index = self.add_pane_combo.currentIndex()
        if index >= 0:
            pane_id = str(self.add_pane_combo.itemData(index) or "")
            if pane_id and self.add_pane_combo.currentText().strip() == self.add_pane_combo.itemText(index).strip():
                return pane_id
        text = self.add_pane_combo.currentText().strip().split(" — ")[0].strip().lower()
        if not text:
            return ""
        # Auch getippte Namen duerfen kein Preispane-Preset anhängen.
        candidates = [pane for pane in self._pane_catalog if not is_price_preset(pane)]
        for pane in candidates:
            if str(pane.get("display_name") or "").strip().lower() == text:
                return str(pane.get("id") or "")
        for pane in candidates:
            if text in str(pane.get("display_name") or "").strip().lower():
                return str(pane.get("id") or "")
        found = self.add_pane_combo.findData(self.add_pane_combo.currentText().strip())
        if found >= 0:
            return str(self.add_pane_combo.itemData(found) or "")
        return ""

    def _rebuild_builder_widgets(self) -> None:
        if not hasattr(self, "builder_pane_list"):
            return
        selected = ""
        item = self.builder_pane_list.currentItem()
        if item is not None:
            selected = str(item.data(Qt.ItemDataRole.UserRole) or "")

        previous = self._populating
        self._populating = True
        try:
            if self._builder_target and self._builder_target in self._chart_symbols:
                symbol = self._chart_symbols.get(self._builder_target) or "?"
                self.builder_target_label.setText(f"→ {self._builder_target} · {symbol}")
            else:
                self.builder_target_label.setText(TARGET_NONE_TEXT)
            self.builder_draft_label.setText(DRAFT_TEXT if self._builder_draft else "")

            current_price = self._main_preset_id()
            self.price_pane_combo.clear()
            for pane in self._pane_catalog:
                role = str(pane.get("role") or "any").strip().lower()
                if role not in ("price", "any"):
                    continue
                pane_id = str(pane.get("id") or "")
                if not pane_id:
                    continue
                self.price_pane_combo.addItem(str(pane.get("display_name") or pane_id), pane_id)
            if current_price:
                index = self.price_pane_combo.findData(current_price)
                if index < 0:
                    self.price_pane_combo.addItem(current_price, current_price)
                    index = self.price_pane_combo.count() - 1
                self.price_pane_combo.setCurrentIndex(index)

            # Kandidaten fuer den Preset-Wechsel der markierten Pane; das aktuelle
            # Preset setzt _sync_pane_editor (inkl. Fallback fuer archivierte Presets).
            self.pane_preset_combo.clear()
            for pane in self._switchable_panes():
                pane_id = str(pane.get("id") or "")
                if not pane_id:
                    continue
                self.pane_preset_combo.addItem(str(pane.get("display_name") or pane_id), pane_id)

            self.builder_pane_list.clear()
            for pane in self._builder_panes:
                row = QListWidgetItem(self._pane_row_text(pane))
                row.setToolTip(
                    f"{self._pane_row_text(pane)}\n"
                    f"Preset: {pane.get('pane_preset_id') or '?'} · Höhe (Gewicht) {pane.get('weight', 2)}"
                )
                row.setData(Qt.ItemDataRole.UserRole, str(pane.get("pane_id") or ""))
                self.builder_pane_list.addItem(row)
            if selected:
                for index in range(self.builder_pane_list.count()):
                    if str(self.builder_pane_list.item(index).data(Qt.ItemDataRole.UserRole)) == selected:
                        self.builder_pane_list.setCurrentRow(index)
                        break
            self.volume_check.setChecked(
                any(str(pane.get("pane_preset_id")) == "builtin:volume" for pane in self._builder_panes)
            )
        finally:
            self._populating = previous
        self._update_builder_enabled()
        self._sync_pane_editor()

    def _update_builder_enabled(self) -> None:
        if not hasattr(self, "builder_pane_list"):
            return
        has_target = bool(self._builder_target) and self._builder_target in self._chart_symbols
        enabled = bool(self._connected) and has_target
        for widget in (
            self.builder_pane_list,
            self.price_pane_combo,
            self.add_pane_combo,
            self.add_pane_btn,
            self.remove_pane_btn,
            self.pane_up_btn,
            self.pane_down_btn,
            self.scale_toggle_btn,
            self.volume_check,
            self.save_chart_btn,
            self.save_inplace_btn,
            self.pane_weight_spin,
            self.pane_title_edit,
            self.duplicate_pane_btn,
            self.discard_chart_btn,
        ):
            widget.setEnabled(enabled)
        # Ausserhalb der Schleife: das Preispane hat kein wechselbares Preset.
        self.pane_preset_combo.setEnabled(enabled and self._pane_preset_switchable())
        self.builder_reload_btn.setEnabled(bool(self._connected))

    # ── flag / pin / buttons ───────────────────────────────────────────────

    def _on_flag_changed(self, flag: int) -> None:
        self._settings.setValue("panel/flag", flag)
        self.set_chart_windows(self._chart_windows)

    def is_pinned(self) -> bool:
        """True while the panel carries the 'always on top' flag."""
        return bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

    def _apply_pin(self, pinned: bool) -> None:
        """Toggle 'always on top' - without ever letting the panel disappear.

        setWindowFlags()/setWindowFlag() re-create the native window and therefore
        *hide* the widget - Qt documents this explicitly ("you must call show() to
        make the widget visible again"). The old code asked isVisible() only after
        the flag change, always saw False and never re-showed the panel, so a click
        on the pin made the whole panel vanish. Hence: remember the visibility
        first, then restore it.
        """
        was_visible = self.isVisible()
        flags = self.windowFlags()
        if pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        # The panel is permanent while the viewer runs: no close button in the title bar.
        flags &= ~Qt.WindowType.WindowCloseButtonHint
        self.setWindowFlags(flags)

        if hasattr(self, "pin_btn"):
            self.pin_btn.setProperty("pinned", "true" if pinned else "false")
            self.pin_btn.setToolTip("Immer im Vordergrund: " + ("an" if pinned else "aus"))
            self.pin_btn.style().unpolish(self.pin_btn)
            self.pin_btn.style().polish(self.pin_btn)

        if was_visible:
            # show() restores geometry and visibility after the native window swap
            self.show()
            self.raise_()
            if pinned:
                self.activateWindow()

    def _toggle_pin(self) -> None:
        pinned = not self.is_pinned()
        self._settings.setValue("panel/pinned_v2", pinned)
        self._apply_pin(pinned)

    def _update_buttons(self) -> None:
        ticker = self._selected_search_ticker()
        has_row = self.watchlist_list.currentItem() is not None
        can_edit_row = (
            self._connected
            and self._watchlist_editable
            and bool(self._current_watchlist)
            and has_row
        )
        self.open_chart_btn.setEnabled(bool(ticker) and self._connected)
        self.add_btn.setEnabled(
            bool(ticker) and self._connected and bool(self._current_watchlist) and self._watchlist_editable
        )
        self.remove_btn.setEnabled(can_edit_row)
        self.copy_btn.setEnabled(can_edit_row)
        self.move_btn.setEnabled(can_edit_row)
        self.transfer_target_combo.setEnabled(self._connected and self.transfer_target_combo.count() > 0)
        self.new_watchlist_btn.setEnabled(self._connected)
        self.delete_watchlist_btn.setEnabled(self._connected and bool(self._watchlists))
        self.watchlist_reload_btn.setEnabled(self._connected)
        self._update_builder_enabled()

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("error", "true" if error else "false")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    # ── settings / lifecycle ───────────────────────────────────────────────

    def _read_pinned_setting(self) -> bool:
        """Read the persisted 'always on top' preference.

        The key is versioned (pinned_v2): the pre-fix build stored "false" on every
        pin click - the click hid the panel before the state could be kept - and
        that broken value would silently unpin the panel on the next start.
        """
        value = self._settings.value("panel/pinned_v2", None)
        if value is None:
            return bool(self.config.control_panel_always_on_top)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on")

    def _restore_settings(self) -> None:
        geometry = self._settings.value("panel/geometry")
        if geometry is not None:
            try:
                self.restoreGeometry(geometry)
            except Exception:
                pass
        try:
            flag = int(self._settings.value("panel/flag", 0) or 0)
        except (TypeError, ValueError):
            flag = 0
        self.flag_btn.set_flag(flag)
        self._preset_id = str(self._settings.value("panel/preset", "") or "")
        try:
            tab = int(self._settings.value("panel/tab", 0) or 0)
        except (TypeError, ValueError):
            tab = 0
        if 0 <= tab < self.tabs.count():
            self.tabs.setCurrentIndex(tab)

    def _save_settings(self) -> None:
        self._settings.setValue("panel/geometry", self.saveGeometry())
        self._settings.setValue("panel/flag", self.flag_btn.get_flag())
        self._settings.setValue("panel/watchlist", self._current_watchlist)
        self._settings.setValue("panel/preset", self._preset_id)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Delete and self.watchlist_list.hasFocus():
            self._remove_selected_ticker()
            event.accept()
            return
        # Escape deliberately no longer hides the panel: it stays available while
        # the viewer runs (only ViewerApp.shutdown() takes it down).
        super().keyPressEvent(event)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt naming)
        # Waehrend _build_ui laufen schon Events durch den Filter: Widgets, die noch
        # nicht existieren, per getattr abfragen.
        if obj is getattr(self, "watchlist_list", None) and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_Delete:
                self._remove_selected_ticker()
                return True
            modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            plain = modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier)
            if plain and event.key() in (Qt.Key.Key_C, Qt.Key.Key_V):
                # C kopiert, V verschiebt in das gemerkte Ziel; Shift fragt nach dem Ziel.
                action = "copy" if event.key() == Qt.Key.Key_C else "move"
                ask = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
                if self.copy_btn.isEnabled():
                    self._transfer_selected_ticker(action, ask=ask)
                return True
            if modifiers == Qt.KeyboardModifier.NoModifier and event.key() == Qt.Key.Key_T:
                self._focus_transfer_target()
                return True
        if obj is getattr(self, "builder_pane_list", None) and event.type() == event.Type.KeyPress:
            if event.modifiers() & Qt.KeyboardModifier.AltModifier and event.key() in (
                Qt.Key.Key_Up,
                Qt.Key.Key_Down,
            ):
                if self.pane_up_btn.isEnabled():
                    self._move_selected_pane(-1 if event.key() == Qt.Key.Key_Up else 1)
                return True
        combo = getattr(self, "watchlist_combo", None)
        if combo is not None and obj is combo.lineEdit() and event.type() == QEvent.Type.MouseButtonPress:
            # Clicking the text area of an editable combo only places the cursor;
            # open the list like a click on the arrow does. Returning False keeps
            # the normal text handling (selection, cursor) intact.
            if combo.view().isVisible():
                combo.hidePopup()
            else:
                combo.showPopup()
        return super().eventFilter(obj, event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt naming)
        """The control panel cannot be closed - only shutdown() takes it down.

        The title bar carries no close button any more (see _apply_pin); this
        handler covers Alt+F4 / Cmd+W and any programmatic close() call.
        """
        self._save_settings()
        if self._shutting_down:
            event.accept()
            return
        event.ignore()
        # A few window managers hide the window before delivering the close event;
        # a refused close must never leave the panel gone.
        self.show()
        self.raise_()
