"""Modal dialogs for watchlist administration in the control panel.

The panel must not talk to the database directly; these dialogs only collect a
user decision (a name, a source list, a target list) and the panel turns it into
a control.request envelope for the agent-side ControlService.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from chart_viewer.ui import theme

# Client-side mirror of the agent-side sanitizer: names are stored verbatim in
# Supabase, so the dialog must not let anything through that the API rejects.
_LIST_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.\- ]")
_MAX_LIST_NAME_LEN = 64
_MASTER_NAMES = {"all", "master", "universe", "all.txt"}

EMPTY_SOURCE_LABEL = "— leer (Ticker später hinzufügen) —"

# Belt and braces: these dialogs must never end up grey-on-white again, not even
# if they are opened before/without the application theme (e.g. from a script
# that builds a QApplication without calling apply_dark_theme). Strictly the
# same palette and the same popup/input rules as the application theme.
DIALOG_STYLESHEET = theme.APP_STYLESHEET


def clean_list_name(raw: str) -> str:
    """Normalize a typed watchlist name (mirrors ControlService.clean_list_name)."""
    return _LIST_NAME_SANITIZE_RE.sub("", str(raw or "").strip())[:_MAX_LIST_NAME_LEN]


def is_reserved_name(name: str) -> bool:
    """True for the protected master-universe aliases ('all', 'master', ...)."""
    return str(name or "").strip().lower() in _MASTER_NAMES


class NewWatchlistDialog(QDialog):
    """Ask for a new watchlist name and an optional list to copy the tickers from."""

    def __init__(
        self,
        existing: Sequence[str],
        sources: Sequence[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Neue Watchlist")
        self.setModal(True)
        self._existing = {str(n).strip().lower() for n in existing if str(n).strip()}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("z. B. 20_watchlist")
        form.addRow("Name:", self.name_edit)

        self.source_combo = QComboBox(self)
        self.source_combo.addItem(EMPTY_SOURCE_LABEL, "")
        for source in sources:
            name = str(source or "").strip()
            if name:
                self.source_combo.addItem(name, name)
        form.addRow("Ticker übernehmen von:", self.source_combo)
        layout.addLayout(form)

        self.hint = QLabel(
            "Ohne Vorlage entsteht eine leere Liste; sie wird mit dem ersten Ticker gespeichert.",
            self,
        )
        self.hint.setObjectName("dialogHint")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    def values(self) -> Tuple[str, str]:
        """Return (name, source_list); an empty source means 'start empty'."""
        name = clean_list_name(self.name_edit.text())
        source = str(self.source_combo.currentData() or "")
        return name, source

    def _on_accept(self) -> None:
        name, _source = self.values()
        if not name:
            self._warn("Bitte einen Namen eingeben.")
            return
        if is_reserved_name(name):
            self._warn(f"'{name}' ist reserviert (Master-Universe).")
            return
        if name.lower() in self._existing:
            self._warn(f"Watchlist '{name}' existiert bereits.")
            return
        self.accept()

    def _warn(self, message: str) -> None:
        self.hint.setText(f"⚠ {message}")
        self.hint.setProperty("error", "true")
        self.hint.style().unpolish(self.hint)
        self.hint.style().polish(self.hint)


def ask_new_watchlist(
    parent: Optional[QWidget],
    existing: Sequence[str],
    sources: Sequence[str],
) -> Optional[Tuple[str, str]]:
    """Run the 'new watchlist' dialog; returns (name, source) or None."""
    dialog = NewWatchlistDialog(existing, sources, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.values()


def ask_watchlist(
    parent: Optional[QWidget],
    title: str,
    label: str,
    names: Sequence[str],
    preselect: str = "",
) -> Optional[str]:
    """Pick one watchlist from a modal list of names (native QInputDialog)."""
    options: List[str] = [str(n) for n in names if str(n).strip()]
    if not options:
        return None
    current = preselect if preselect in options else options[0]
    choice, ok = QInputDialog.getItem(
        parent, title, label, options, options.index(current), False
    )
    if not ok:
        return None
    choice = str(choice or "").strip()
    return choice or None


def confirm_delete(parent: Optional[QWidget], name: str) -> bool:
    """Ask before deleting a whole watchlist."""
    answer = QMessageBox.question(
        parent,
        "Watchlist löschen",
        f"Watchlist '{name}' mit allen Tickern löschen?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes
