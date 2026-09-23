"""Das Hauptfenster.

Aufbau: eine Liste (aufklappbar), daneben das Detail. Links oben die Suchmaske,
rechts der Weg zum Ergebnis — genau der Weg, den ein Lauf beim Lesen nimmt:
Change Item -> Item -> result.

Das Fenster schreibt nur zwei Dinge: einen neuen Auftrag und die Metadaten einer
stehenden Aufgabe. Statusuebergaenge macht die Sekretaerin.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QModelIndex, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHeaderView, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QSizePolicy, QSplitter, QTableWidget, QTableWidgetItem,
                               QToolBar, QTreeView, QVBoxLayout, QWidget)

from . import format, theme
from .api import DashboardApi
from .dialogs import NewOrderDialog
from .model import RunTreeModel
from .worker import DbWorker

REFRESH_MS = 5000
ITEM_COLUMNS = ("step_key", "Typ", "Rolle", "Status", "Runde", "Versuche", "Tokens", "Sekunden")


class DashboardWindow(QMainWindow):
    request_runs = Signal(str, str, str)
    request_detail = Signal(str)
    request_roles = Signal()
    request_submit = Signal(str, str, int)
    request_create_schedule = Signal(str, str, object, str, int)
    request_cancel_schedule = Signal(str)
    request_run_now = Signal(str)

    def __init__(self, api: DashboardApi, base_url: str) -> None:
        super().__init__()
        self.api = api
        self.base_url = base_url
        self.settings = QSettings("qjm", "wiq-dashboard")
        self.roles: list[dict[str, Any]] = []
        self._selected_id: str | None = None
        self._current_items: list[dict[str, Any]] = []
        self._current_change_item: dict[str, Any] | None = None

        self.setWindowTitle("WIQ — Workitem-Engine")
        self._apply_default_geometry()
        self.model = RunTreeModel()

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._start_worker()

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self._auto_refresh)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.reload)

        self._restore_state()
        self.reload()
        self.request_roles.emit()

    # ── Aufbau ─────────────────────────────────────────────────────────────
    def _build_toolbar(self) -> None:
        bar = QToolBar("Aktionen")
        bar.setMovable(False)
        self.addToolBar(bar)

        new_action = QAction("Neuer Auftrag", self)
        new_action.setShortcut(QKeySequence("Ctrl+N"))
        new_action.triggered.connect(self._new_order)
        bar.addAction(new_action)

        bar.addSeparator()
        self.run_action = QAction("Jetzt ausfuehren", self)
        self.run_action.triggered.connect(self._run_now)
        bar.addAction(self.run_action)

        self.cancel_action = QAction("Abschalten", self)
        self.cancel_action.triggered.connect(self._cancel)
        bar.addAction(self.cancel_action)

        bar.addSeparator()
        refresh = QAction("Aktualisieren", self)
        refresh.setShortcut(QKeySequence("F5"))
        refresh.triggered.connect(self.reload)
        bar.addAction(refresh)

        self.live_action = QAction("Live", self)
        self.live_action.setCheckable(True)
        self.live_action.setChecked(True)
        self.live_action.toggled.connect(self._toggle_live)
        bar.addAction(self.live_action)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        self.filter_state = QComboBox()
        self.filter_state.addItem("alle Zustaende", "")
        for state in ("running", "scheduled", "done", "failed", "cancelled", "silent"):
            self.filter_state.addItem(theme.state_label(state), state)
        self.filter_state.currentIndexChanged.connect(self.reload)
        bar.addWidget(self.filter_state)

        self.filter_kind = QComboBox()
        for label, value in (("alle Arten", ""), ("stehend", "stehend"), ("ad-hoc", "adhoc")):
            self.filter_kind.addItem(label, value)
        self.filter_kind.currentIndexChanged.connect(self.reload)
        bar.addWidget(self.filter_kind)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Suchen in Titel und Auftrag ...")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(320)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        bar.addWidget(self.search)

    def _build_tree(self) -> QTreeView:
        tree = QTreeView()
        tree.setModel(self.model)
        tree.setUniformRowHeights(True)
        tree.setRootIsDecorated(True)
        tree.setAlternatingRowColors(False)
        tree.setSortingEnabled(False)
        tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tree.selectionModel().currentChanged.connect(self._on_selection)
        header = tree.header()
        # Der Titel traegt die Information und bekommt den freien Platz; die
        # Regelspalte ist lang, aber nie wichtiger als der Titel.
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        # Breiten passend zur Schrift (16px). Die Titelspalte bekommt den Rest.
        for column, width in ((0, 172), (2, 215), (3, 60), (4, 88), (5, 158), (6, 158)):
            tree.setColumnWidth(column, width)
        self.tree = tree
        return tree

    def _build_central(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_tree())

        detail = QWidget()
        layout = QVBoxLayout(detail)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        self.title_label = QLabel("Kein Lauf ausgewaehlt")
        self.title_label.setObjectName("title")
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Preferred)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("muted")
        self.meta_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.rule_label = QLabel("")
        self.rule_label.setObjectName("muted")
        self.rule_label.setWordWrap(True)
        for label in (self.meta_label, self.rule_label):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self.prompt_view = QPlainTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setMaximumHeight(88)
        self.prompt_view.setPlaceholderText("(kein Auftragstext)")
        # Ignored horizontal: lange Texte duerfen den Splitter nicht aufdruecken.
        self.prompt_view.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Fixed)

        layout.addWidget(self.title_label)
        layout.addWidget(self.meta_label)
        layout.addWidget(self.rule_label)
        layout.addWidget(self.prompt_view)

        detail_split = QSplitter(Qt.Orientation.Vertical)

        self.items_table = QTableWidget(0, len(ITEM_COLUMNS))
        self.items_table.setHorizontalHeaderLabels(ITEM_COLUMNS)
        self.items_table.verticalHeader().setVisible(False)
        self.items_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.items_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.items_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.items_table.setShowGrid(False)
        self.items_table.setAlternatingRowColors(True)
        self.items_table.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Expanding)
        self.items_table.horizontalHeader().setStretchLastSection(False)
        self.items_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.items_table.itemSelectionChanged.connect(self._show_result)
        detail_split.addWidget(self.items_table)

        result_box = QWidget()
        result_layout = QVBoxLayout(result_box)
        result_layout.setContentsMargins(0, 6, 0, 0)
        result_layout.setSpacing(4)
        self.result_label = QLabel("Ergebnis")
        self.result_label.setObjectName("muted")
        self.result_view = QPlainTextEdit()
        self.result_view.setReadOnly(True)
        # Umbruch statt Querlauf: hier steht Prosa (Befunde des Reviews), kein Code.
        self.result_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.result_view.setFont(theme.mono_font(13))
        self.result_view.setPlaceholderText(
            "Ein Item in der Tabelle waehlen — hier steht sein result-JSON (die Antwort).")
        self.result_view.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Expanding)
        result_layout.addWidget(self.result_label)
        result_layout.addWidget(self.result_view)
        detail_split.addWidget(result_box)
        detail_split.setSizes([320, 420])

        layout.addWidget(detail_split, 1)
        splitter.addWidget(detail)
        splitter.setSizes([760, 800])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        self.splitter = splitter
        self.detail_split = detail_split
        self.setCentralWidget(splitter)

    def _build_statusbar(self) -> None:
        self.status_db = QLabel("Server: " + self.base_url)
        self.status_counts = QLabel("")
        self.status_time = QLabel("")
        bar = self.statusBar()
        bar.addWidget(self.status_db)
        bar.addPermanentWidget(self.status_counts)
        bar.addPermanentWidget(self.status_time)

    # ── Thread ─────────────────────────────────────────────────────────────
    def _start_worker(self) -> None:
        self.thread = QThread(self)
        self.worker = DbWorker(self.api)
        self.worker.moveToThread(self.thread)
        self.request_runs.connect(self.worker.load_runs)
        self.request_detail.connect(self.worker.load_detail)
        self.request_roles.connect(self.worker.load_roles)
        self.request_submit.connect(self.worker.submit)
        self.request_create_schedule.connect(self.worker.create_schedule)
        self.request_cancel_schedule.connect(self.worker.cancel_schedule)
        self.request_run_now.connect(self.worker.run_schedule_now)
        self.worker.runs_loaded.connect(self._on_runs)
        self.worker.detail_loaded.connect(self._on_detail)
        self.worker.roles_loaded.connect(self._on_roles)
        self.worker.message.connect(self._on_message)
        self.worker.failed.connect(self._on_failed)
        self.thread.start()

    # ── Laden ──────────────────────────────────────────────────────────────
    def reload(self) -> None:
        self.request_runs.emit(self.search.text(), self.filter_state.currentData() or "",
                               self.filter_kind.currentData() or "")

    def _auto_refresh(self) -> None:
        if not self.live_action.isChecked():
            return
        self.reload()
        if self._selected_id:
            self.request_detail.emit(self._selected_id)

    def _on_runs(self, roots: list[dict[str, Any]],
                 occurrences: dict[str, list[dict[str, Any]]]) -> None:
        self.model.set_data(roots, occurrences)
        self.tree.expandAll()
        self.status_counts.setText(
            f"{len(roots)} Wurzeln · {sum(len(v) for v in occurrences.values())} Vorkommen")
        # Lokale Zeit — der Betrachter sitzt vor diesem Rechner, nicht in UTC.
        self.status_time.setText("aktualisiert " + datetime.now().strftime("%H:%M:%S"))
        if self._selected_id:
            self._reselect(self._selected_id)
        elif self.model.rowCount() > 0:
            # Beim ersten Laden etwas zeigen statt einer leeren Detailflaeche.
            self.tree.setCurrentIndex(self.model.index(0, 0))

    def _reselect(self, change_item_id: str) -> None:
        for row in range(self.model.rowCount()):
            index = self.model.index(row, 0)
            node = self.model.node_at(index)
            if node is None:
                continue
            if str(node.row["id"]) == change_item_id:
                self.tree.setCurrentIndex(index)
                return
            for child_row, child in enumerate(node.children):
                if str(child.row["id"]) == change_item_id:
                    self.tree.setCurrentIndex(self.model.index(child_row, 0, index))
                    return

    def _on_detail(self, change_item: dict[str, Any] | None,
                   items: list[dict[str, Any]]) -> None:
        self._current_change_item = change_item
        self._current_items = items or []
        if not change_item:
            self.title_label.setText("Kein Lauf ausgewaehlt")
            self.meta_label.setText("")
            self.rule_label.setText("")
            self.prompt_view.setPlainText("")
            self.items_table.setRowCount(0)
            self.result_view.setPlainText("")
            return

        state = str(change_item.get("state", ""))
        self.title_label.setText(str(change_item.get("title") or ""))
        self.title_label.setStyleSheet("color: " + theme.state_color(state).name())

        parts = [
            theme.state_label(state),
            "id " + str(change_item.get("id")),
            "angelegt " + format.dt(change_item.get("created_at"), seconds=True),
        ]
        if change_item.get("finished_at"):
            parts.append("fertig " + format.dt(change_item.get("finished_at"), seconds=True))
        elif change_item.get("running_since"):
            parts.append("laeuft seit " + format.elapsed(change_item.get("running_since")))
        if change_item.get("max_rounds"):
            parts.append(f"max {change_item['max_rounds']} Runden")
        if change_item.get("is_template"):
            parts.append(f"{change_item.get('run_count')} Vorkommen")
            if change_item.get("next_run_at"):
                parts.append("naechster " + format.dt(change_item.get("next_run_at"))
                             + " (" + format.relative(change_item.get("next_run_at")) + ")")
        self.meta_label.setText("   ·   ".join(parts))

        if change_item.get("is_template"):
            self.rule_label.setText("Regel: " + str(change_item.get("rule_text") or "-"))
        elif change_item.get("source_change_item_id"):
            self.rule_label.setText("Vorkommen einer stehenden Aufgabe")
        else:
            self.rule_label.setText("")
        self.prompt_view.setPlainText(str(change_item.get("entry_prompt") or ""))

        self._fill_items(self._current_items)
        self._select_best_item()

    def _fill_items(self, items: list[dict[str, Any]]) -> None:
        self.items_table.setRowCount(len(items))
        for row, item in enumerate(items):
            usage = item.get("usage") or {}
            # Laufende Items haben noch keinen Verbrauch — zeig die Laufzeit.
            laufzeit = ""
            if str(item.get("status")) == "running" and item.get("started_at"):
                laufzeit = format.elapsed(item.get("started_at"))
            values = (
                str(item.get("step_key")),
                theme.ITEM_TYPE_LABELS.get(str(item.get("type")), str(item.get("type"))),
                # Rolle mit dem Level, das wirklich gilt.
                str(item.get("role")) + (f" · {item['reasoning_effort']}"
                                         if item.get("reasoning_effort") else ""),
                theme.state_label(str(item.get("status"))),
                str(item.get("round")),
                f"{item.get('attempts')}/{item.get('max_attempts')}",
                format.tokens_exact(usage.get("tokens")),
                laufzeit or str(usage.get("seconds", "") or ""),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, item)
                if column == 3:
                    cell.setForeground(theme.state_color(str(item.get("status"))))
                if column >= 4:
                    cell.setTextAlignment(int(Qt.AlignmentFlag.AlignRight
                                              | Qt.AlignmentFlag.AlignVCenter))
                self.items_table.setItem(row, column, cell)
        # Feste Breiten statt Inhaltsmessung: die Item-Tabelle sitzt in einem
        # Splitter und darf ihre Spalten nicht selbst aushandeln.
        header = self.items_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 85), (2, 118), (3, 132), (4, 68), (5, 88),
                              (6, 88), (7, 88)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.items_table.setColumnWidth(column, width)

    def _select_best_item(self) -> None:
        """Das Item mit der Antwort vorauswaehlen: das letzte Review mit Ergebnis,
        sonst das letzte Item mit Ergebnis."""
        if not self._current_items:
            self.items_table.clearSelection()
            self.result_view.setPlainText("")
            return
        best = None
        for index, item in enumerate(self._current_items):
            if item.get("result") is None:
                continue
            if str(item.get("type")) == "review":
                best = index
            elif best is None:
                best = index
        self.items_table.selectRow(best if best is not None else 0)

    def _show_result(self) -> None:
        rows = self.items_table.selectionModel().selectedRows() if \
            self.items_table.selectionModel() else []
        if not rows:
            return
        cell = self.items_table.item(rows[0].row(), 0)
        if cell is None:
            return
        item = cell.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item, dict):
            return
        usage = item.get("usage") or {}
        self.result_label.setText(
            f"{item.get('step_key')} · {theme.state_label(str(item.get('status')))} · "
            f"{format.tokens_exact(usage.get('tokens'))} Tokens · {usage.get('seconds', '-')} s · "
            f"Runde {item.get('round')} · "
            f"{format.dt(item.get('finished_at') or item.get('started_at'), seconds=True)}")
        result = item.get("result")
        if result is None:
            payload = item.get("payload") or {}
            self.result_view.setPlainText(
                "noch kein Ergebnis.\n\nAuftrag (payload):\n"
                + json.dumps(payload, indent=2, ensure_ascii=False))
            return
        if isinstance(result, str):
            self.result_view.setPlainText(result)
        else:
            self.result_view.setPlainText(json.dumps(result, indent=2, ensure_ascii=False))

    # ── Auswahl ────────────────────────────────────────────────────────────
    def _on_selection(self, current: QModelIndex, _previous: QModelIndex) -> None:
        node = self.model.node_at(current)
        if node is None:
            return
        self._selected_id = str(node.row["id"])
        self.request_detail.emit(self._selected_id)
        active = bool(node.is_definition and str(node.row.get("state")) == "scheduled")
        self.run_action.setEnabled(active)
        self.cancel_action.setEnabled(active)

    # ── Aktionen ───────────────────────────────────────────────────────────
    def _new_order(self) -> None:
        dialog = NewOrderDialog(self.roles, self)
        if dialog.exec() != NewOrderDialog.DialogCode.Accepted:
            return
        if dialog.tabs.currentIndex() == 0:
            prompt, title, max_rounds = dialog.now_values()
            if not prompt.strip():
                QMessageBox.warning(self, "Leer", "Der Auftrag ist leer.")
                return
            self.request_submit.emit(prompt, title, max_rounds)
        else:
            prompt, role, rule, title, max_rounds = dialog.standing_values()
            if not prompt.strip():
                QMessageBox.warning(self, "Leer", "Der Auftrag ist leer.")
                return
            if not role:
                QMessageBox.warning(self, "Rolle fehlt", "Keine Rolle gewaehlt.")
                return
            self.request_create_schedule.emit(prompt, role, rule, title, max_rounds)

    def _run_now(self) -> None:
        if self._selected_id:
            self.request_run_now.emit(self._selected_id)

    def _cancel(self) -> None:
        if not self._selected_id:
            return
        answer = QMessageBox.question(
            self, "Abschalten",
            "Diese stehende Aufgabe bekommt keinen weiteren Termin.\n"
            "Ein gerade laufendes Vorkommen laeuft zu Ende. Fortfahren?")
        if answer == QMessageBox.StandardButton.Yes:
            self.request_cancel_schedule.emit(self._selected_id)

    def _toggle_live(self, enabled: bool) -> None:
        if enabled:
            self.timer.start()
        else:
            self.timer.stop()

    def _on_roles(self, roles: list) -> None:
        # Der Server liefert Objekte: {name, reasoning_effort, max_concurrency, description}.
        self.roles = [r for r in roles if isinstance(r, dict)]

    def _on_message(self, message: str) -> None:
        self.statusBar().showMessage(message, 8000)
        QTimer.singleShot(400, self.reload)

    def _on_failed(self, message: str) -> None:
        self.statusBar().showMessage("Fehler: " + message, 15000)

    # ── Geometrie ──────────────────────────────────────────────────────────
    def _apply_default_geometry(self) -> None:
        """So gross wie der Bildschirm erlaubt, aber nie darueber hinaus. Eine
        geratene Fenstergroesse ist auf einem 4K-Schirm so falsch wie auf einem
        Laptop — gemessen wird die verfuegbare Flaeche."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1400, 880)
            return
        available = screen.availableGeometry()
        width = max(1100, min(2200, int(available.width() * 0.94)))
        height = max(700, min(1300, int(available.height() * 0.92)))
        self.resize(width, height)

    def _default_split(self) -> None:
        """Die Liste bekommt etwas mehr als das Detail: links wird gesucht,
        rechts gelesen."""
        total = max(self.splitter.width(), 1000)
        self.splitter.setSizes([int(total * 0.58), int(total * 0.42)])

    # ── Zustand merken ─────────────────────────────────────────────────────
    def _restore_state(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        splitter = self.settings.value("splitter")
        if splitter:
            self.splitter.restoreState(splitter)
        else:
            self._default_split()
        detail_split = self.settings.value("detail_split")
        if detail_split:
            self.detail_split.restoreState(detail_split)
        if self.settings.value("live", True, bool):
            self.live_action.setChecked(True)
            self.timer.start()
        else:
            self.live_action.setChecked(False)
        # Fenster nie ausserhalb des Bildschirms oeffnen
        screen = QGuiApplication.primaryScreen().availableGeometry()
        if not screen.intersects(self.frameGeometry()):
            self.move(screen.center() - self.rect().center())

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt-Namensschema
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.saveState())
        self.settings.setValue("detail_split", self.detail_split.saveState())
        self.settings.setValue("live", self.live_action.isChecked())
        self.timer.stop()
        self.thread.quit()
        self.thread.wait(3000)
        super().closeEvent(event)
