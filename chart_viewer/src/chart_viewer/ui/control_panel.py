"""Control panel window: ticker search, watchlist editing and chart presets.

The panel is owned by :class:`ViewerApp` (not by the agent layout ledger): it is
created once when the viewer starts and stays available independently of the chart
windows. All data operations are delegated to the agent via `control.request`
envelopes; the panel never talks to the database directly.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from PySide6.QtCore import QEvent, Qt, QSettings, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from chart_viewer.config import ViewerConfig
from chart_viewer.ui import theme
from chart_viewer.ui import watchlist_dialogs
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
QMainWindow, QWidget#panelRoot {
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
QListWidget {
    font-size: 12px;
}
QListWidget::item {
    padding: 2px 4px;
}
QPushButton#pinButton[pinned="true"] {
    border: 1px solid #FFCA28;
    background-color: #4A3B12;
    color: #FFCA28;
}
"""
)


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
        self._populating = False
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
        root = QWidget(self)
        root.setObjectName("panelRoot")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(6)

        # Header: colour flag + pin toggle
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(QLabel("FLAG", root))
        self.flag_btn = ColorFlagButton(initial_flag=0, parent=self)
        self.flag_btn.flag_changed.connect(self._on_flag_changed)
        header.addWidget(self.flag_btn)
        header.addStretch(1)
        self.pin_btn = QPushButton("📌", root)
        self.pin_btn.setObjectName("pinButton")
        self.pin_btn.setFixedWidth(28)
        self.pin_btn.setToolTip("Immer im Vordergrund")
        self.pin_btn.clicked.connect(self._toggle_pin)
        header.addWidget(self.pin_btn)
        layout.addLayout(header)

        # Ticker search
        layout.addWidget(self._section_label("TICKER-SUCHE"))
        self.search_edit = QLineEdit(root)
        self.search_edit.setPlaceholderText("Ticker suchen (z. B. NV…)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        self.search_edit.returnPressed.connect(self._on_search_return)
        layout.addWidget(self.search_edit)

        self.result_list = QListWidget(root)
        self.result_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.result_list.setFixedHeight(96)
        self.result_list.itemSelectionChanged.connect(self._update_buttons)
        self.result_list.itemDoubleClicked.connect(lambda _item: self._activate_selected_symbol())
        layout.addWidget(self.result_list)

        search_buttons = QHBoxLayout()
        search_buttons.setSpacing(6)
        self.open_chart_btn = QPushButton("▸ Chart", root)
        self.open_chart_btn.setToolTip("Ticker im Chart der Flag-Gruppe öffnen (Enter)")
        self.open_chart_btn.clicked.connect(self._activate_selected_symbol)
        search_buttons.addWidget(self.open_chart_btn)
        self.add_btn = QPushButton("＋ Watchlist", root)
        self.add_btn.setToolTip("Ausgewählten Ticker zur Watchlist hinzufügen (Ctrl+Enter)")
        self.add_btn.clicked.connect(self._add_selected_ticker)
        search_buttons.addWidget(self.add_btn)
        search_buttons.addStretch(1)
        self.search_status = QLabel("", root)
        self.search_status.setObjectName("statusLabel")
        search_buttons.addWidget(self.search_status)
        layout.addLayout(search_buttons)

        # Watchlist
        wl_header = QHBoxLayout()
        wl_header.setSpacing(6)
        wl_header.addWidget(self._section_label("WATCHLIST"))
        wl_header.addStretch(1)
        self.new_watchlist_btn = QPushButton("＋ Neu", root)
        self.new_watchlist_btn.setToolTip("Neue Watchlist anlegen (leer oder als Kopie einer bestehenden)")
        self.new_watchlist_btn.clicked.connect(self._create_watchlist)
        wl_header.addWidget(self.new_watchlist_btn)
        self.delete_watchlist_btn = QPushButton("− Löschen", root)
        self.delete_watchlist_btn.setToolTip("Watchlist mit allen Tickern löschen")
        self.delete_watchlist_btn.clicked.connect(self._delete_watchlist)
        wl_header.addWidget(self.delete_watchlist_btn)
        layout.addLayout(wl_header)

        wl_row = QHBoxLayout()
        wl_row.setSpacing(6)
        self.watchlist_combo = QComboBox(root)
        self.watchlist_combo.setEditable(True)
        self.watchlist_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.watchlist_combo.setSizePolicy(self.watchlist_combo.sizePolicy().horizontalPolicy(), self.watchlist_combo.sizePolicy().verticalPolicy())
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
        self.watchlist_reload_btn = QPushButton("⟳", root)
        self.watchlist_reload_btn.setFixedWidth(28)
        self.watchlist_reload_btn.setToolTip("Watchlisten neu laden")
        self.watchlist_reload_btn.clicked.connect(self.refresh_watchlists)
        wl_row.addWidget(self.watchlist_reload_btn)
        layout.addLayout(wl_row)

        self.watchlist_list = QListWidget(root)
        self.watchlist_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Marking a row is enough to load it - no double click required.
        self.watchlist_list.itemSelectionChanged.connect(self._on_watchlist_selection_changed)
        self.watchlist_list.installEventFilter(self)
        layout.addWidget(self.watchlist_list, 1)

        wl_buttons = QHBoxLayout()
        wl_buttons.setSpacing(6)
        self.remove_btn = QPushButton("− Entfernen", root)
        self.remove_btn.setToolTip("Ausgewählten Ticker aus der Watchlist entfernen (Del)")
        self.remove_btn.clicked.connect(self._remove_selected_ticker)
        wl_buttons.addWidget(self.remove_btn)
        self.copy_btn = QPushButton("⧉ Kopieren", root)
        self.copy_btn.setToolTip("Ausgewählten Ticker in eine andere Watchlist kopieren")
        self.copy_btn.clicked.connect(self._copy_selected_ticker)
        wl_buttons.addWidget(self.copy_btn)
        self.move_btn = QPushButton("⇄ Verschieben", root)
        self.move_btn.setToolTip("Ausgewählten Ticker in eine andere Watchlist verschieben")
        self.move_btn.clicked.connect(self._move_selected_ticker)
        wl_buttons.addWidget(self.move_btn)
        wl_buttons.addStretch(1)
        self.watchlist_info = QLabel("", root)
        self.watchlist_info.setObjectName("statusLabel")
        wl_buttons.addWidget(self.watchlist_info)
        layout.addLayout(wl_buttons)

        # Preset
        layout.addWidget(self._section_label("CHART-PRESET"))
        self.preset_combo = QComboBox(root)
        self.preset_combo.setEditable(True)
        self.preset_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        preset_completer = self.preset_combo.completer()
        if preset_completer is not None:
            preset_completer.setFilterMode(Qt.MatchFlag.MatchContains)
            preset_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.preset_combo.activated.connect(self._on_preset_activated)
        layout.addWidget(self.preset_combo)

        self.charts_label = QLabel("Charts: –", root)
        self.charts_label.setObjectName("statusLabel")
        layout.addWidget(self.charts_label)

        # Status line
        self.status_label = QLabel("", root)
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.setStyleSheet(PANEL_STYLESHEET)
        self._update_buttons()

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        return label

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
            self._set_status(f"⚠ {op}: {message}", error=True)
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
        elif op == "list_presets":
            self._on_presets_response(data)
        elif op == "apply_preset":
            self._on_apply_preset_response(data)
        self._update_buttons()

    def set_chart_windows(self, windows: List[Dict[str, Any]]) -> None:
        """Called by ViewerApp whenever chart windows open/close/change flag."""
        self._chart_windows = list(windows or [])
        flag = self.flag_btn.get_flag()
        symbols = [
            str(w.get("symbol") or "?")
            for w in self._chart_windows
            if int(w.get("color_flag") or 0) == flag
        ]
        self.charts_label.setText(f"Charts (Flag {flag}): {', '.join(symbols) if symbols else '–'}")

    def refresh_all(self) -> None:
        self.refresh_watchlists()
        self.refresh_presets()

    def refresh_watchlists(self, select: str = "") -> None:
        """Reload the list of watchlists; optionally select one afterwards."""
        if not self._connected:
            return
        if select:
            self._select_after_refresh = select
        self._request("list_watchlists", {})

    def refresh_presets(self) -> None:
        if not self._connected:
            return
        self._request("list_presets", {})

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
            self._update_buttons()
            return
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
        self._transfer_selected_ticker("copy")

    def _move_selected_ticker(self) -> None:
        self._transfer_selected_ticker("move")

    def _transfer_selected_ticker(self, action: str) -> None:
        """Copy/move the selected row into another watchlist picked by the user."""
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
        target = watchlist_dialogs.ask_watchlist(
            self,
            f"Ticker {verb}",
            f"Ziel-Watchlist für {ticker}:",
            targets,
        )
        if not target:
            return
        self._set_status(f"{verb} {ticker} nach {target}…")
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

    def _on_presets_response(self, data: Dict[str, Any]) -> None:
        presets = [p for p in (data.get("presets") or []) if isinstance(p, dict)]
        wanted = self._preset_id or str(self._settings.value("panel/preset", "") or "")
        self._populating = True
        try:
            self.preset_combo.clear()
            for preset in presets:
                preset_id = str(preset.get("id") or "")
                if not preset_id:
                    continue
                label = str(preset.get("display_name") or preset_id)
                self.preset_combo.addItem(label, preset_id)
        finally:
            self._populating = False

        # Display a preset for orientation, but only keep it as an active override
        # (`_preset_id`) when the user picked one; empty means "keep the preset the
        # chart window already uses".
        display_id = wanted or (str(presets[0].get("id") or "") if presets else "")
        if display_id:
            index = self.preset_combo.findData(display_id)
            if index >= 0:
                self._populating = True
                try:
                    self.preset_combo.setCurrentIndex(index)
                finally:
                    self._populating = False
        if wanted:
            self._preset_id = wanted

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
        if applied:
            self._set_status(f"Preset {preset_id}: {len(applied)} Chart(s) aktualisiert")
        else:
            reason = ""
            if skipped:
                reason = f" ({skipped[0].get('reason', '')})"
            self._set_status(
                f"Preset {preset_id} gemerkt – kein Chart mit Flag {self.flag_btn.get_flag()} offen{reason}"
            )

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
        self.new_watchlist_btn.setEnabled(self._connected)
        self.delete_watchlist_btn.setEnabled(self._connected and bool(self._watchlists))

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
        if obj is self.watchlist_list and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_Delete:
                self._remove_selected_ticker()
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
