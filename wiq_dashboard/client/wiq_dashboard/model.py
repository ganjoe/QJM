"""Das Listenmodell: zwei Ebenen.

Ebene 0: Wurzeln — ad-hoc-Laeufe und stehende Aufgaben.
Ebene 1: die Vorkommen einer stehenden Aufgabe (ihre Instanzen).

Das ist die "aufklappbare" Ansicht: eine stehende Aufgabe zeigt unter sich die
Reihe ihrer Laeufe — die eigentliche Information bei einer Wiederholung, weil
erst der Vergleich der Vorkommen zeigt, was sich geaendert hat.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QFont

from . import format, theme

# "Laeufe" gibt es bewusst nicht mehr: die Zahl der Vorkommen steht im Baum
# (die Kinder) und im Detailkopf. Bei 30 % groesserer Schrift ist der Platz in
# der Titelspalte besser angelegt.
COLUMNS = ("Zustand", "Titel", "Art / Regel", "Items", "Tokens", "Angelegt",
           "Termin / Fertig")
NUMERIC_COLUMNS = {3, 4}


class Node:
    """Ein Knoten des Baums. Haelt die DB-Zeile und die Summe seiner Kinder."""

    __slots__ = ("row", "children", "parent")

    def __init__(self, row: dict[str, Any], parent: "Node | None" = None) -> None:
        self.row = row
        self.children: list[Node] = []
        self.parent = parent

    @property
    def is_definition(self) -> bool:
        return bool(self.row.get("is_template"))

    def total_tokens(self) -> int:
        return int(self.row.get("tokens") or 0) + sum(c.total_tokens() for c in self.children)

    def open_items(self) -> int:
        return int(self.row.get("offen") or 0) + sum(c.open_items() for c in self.children)


class RunTreeModel(QAbstractItemModel):
    """Liest nur. Gefuellt wird ueber set_data()."""

    def __init__(self) -> None:
        super().__init__()
        self._roots: list[Node] = []
        self._bold = QFont()
        self._bold.setBold(True)

    # ── Fuellen ────────────────────────────────────────────────────────────
    def set_data(self, roots: list[dict[str, Any]],
                 occurrences: dict[str, list[dict[str, Any]]]) -> None:
        self.beginResetModel()
        self._roots = []
        for row in roots:
            node = Node(row)
            if row.get("is_template"):
                for child in occurrences.get(str(row["id"]), []):
                    node.children.append(Node(child, node))
            self._roots.append(node)
        self.endResetModel()

    def node_at(self, index: QModelIndex) -> Node | None:
        if not index.isValid():
            return None
        return index.internalPointer()

    # ── QAbstractItemModel ─────────────────────────────────────────────────
    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, self._roots[row])
        parent_node: Node = parent.internalPointer()
        return self.createIndex(row, column, parent_node.children[row])

    def parent(self, index: QModelIndex) -> QModelIndex:  # type: ignore[override]
        if not index.isValid():
            return QModelIndex()
        node: Node = index.internalPointer()
        if node.parent is None:
            return QModelIndex()
        grand = node.parent.parent
        row = self._roots.index(node.parent) if grand is None else grand.children.index(node.parent)
        return self.createIndex(row, 0, node.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if not parent.isValid():
            return len(self._roots)
        node: Node = parent.internalPointer()
        return len(node.children) if parent.column() == 0 else 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        node: Node = index.internalPointer()
        row = node.row
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                label = theme.state_label(str(row["state"]))
                if node.open_items() > 0:
                    label += "  (" + str(node.open_items()) + " offen)"
                return label
            if column == 1:
                if node.parent is not None:
                    # Unter einer stehenden Aufgabe ist der Titel redundant —
                    # die Reihe ist die Information: Nummer und Zeitpunkt.
                    siblings = node.parent.children
                    number = len(siblings) - siblings.index(node)
                    return f"Nr. {number} · {format.dt(row.get('created_at'))}"
                return str(row.get("title") or "")
            if column == 2:
                if node.is_definition:
                    # Der Satz kommt fertig vom Server (dort lebt schedules.py).
                    return "stehend · " + str(row.get("rule_short") or "-")
                if row.get("source_change_item_id"):
                    return "Vorkommen"
                return "ad-hoc"
            if column == 3:
                # Eine stehende Aufgabe hat keine Arbeitsschritte, sondern
                # Vorkommen — die stehen als Kinder darunter.
                return "-" if node.is_definition else str(row.get("items") or 0)
            if column == 4:
                return format.tokens(node.total_tokens())
            if column == 5:
                return format.dt(row.get("created_at"))
            if column == 6:
                if node.is_definition:
                    return format.dt(row.get("next_run_at")) if row.get("next_run_at") else "-"
                if row.get("finished_at"):
                    return format.dt(row.get("finished_at"))
                # Laufend: die verstrichene Zeit ist der Fortschritt. Sie tickt
                # mit der Aktualisierung — sonst sieht ein langer Lauf wie
                # Stillstand aus.
                if row.get("running_since"):
                    return "laeuft " + format.elapsed(row.get("running_since"))
                return "-"
            return None

        if role == Qt.ItemDataRole.ForegroundRole and column == 0:
            return QBrush(theme.state_color(str(row["state"])))
        if role == Qt.ItemDataRole.ForegroundRole and column in (1, 2) and not node.is_definition:
            return QBrush(theme.state_color(str(row["state"]))) if column == 2 else None
        if role == Qt.ItemDataRole.FontRole and node.is_definition:
            return self._bold
        if role == Qt.ItemDataRole.TextAlignmentRole and column in NUMERIC_COLUMNS:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            parts = [str(row.get("title") or ""), "id: " + str(row.get("id"))]
            if row.get("entry_prompt"):
                parts += ["", str(row["entry_prompt"])[:800]]
            if node.is_definition:
                parts += ["", "Regel: " + str(row.get("rule_text") or "-")]
                parts += [f"{row.get('run_count') or 0} Vorkommen"]
                if row.get("next_run_at"):
                    parts += ["Naechster Termin: " + format.dt(row.get("next_run_at"), seconds=True)
                              + "  (" + format.relative(row.get("next_run_at")) + ")"]
            return "\n".join(parts)
        return None
