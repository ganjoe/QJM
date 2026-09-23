"""Dialoge: neuer Auftrag (sofort) und neue stehende Aufgabe.

Beide schreiben nur eine neue Zeile — sie setzen keinen Status. Die stehende
Aufgabe wird hier NICHT zeitlich interpretiert: das Fenster schreibt die Regel,
die Sekretaerin rechnet den Termin (services/secretary/secretary/schedules.py).
Deshalb ist der Dialog bewusst klein: er erfindet keine Terminlogik.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox,
                               QFormLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget)

WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
ZONES = ("Europe/Berlin", "Europe/London", "America/New_York", "UTC")


class NewOrderDialog(QDialog):
    """Ein Fenster, zwei Wege: sofort einreichen oder stehend anlegen.

    Kein Chat: das Feld stellt eine Frage an die Engine, keine an ein Modell.
    Die Antwort liest man im Lauf selbst (Review-Item).
    """

    def __init__(self, roles: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Neuer Auftrag")
        self.resize(920, 700)
        self.roles = roles

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_now(), "Sofort")
        self.tabs.addTab(self._build_standing(), "Stehend (verzoegert / wiederkehrend)")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Einreichen")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)

    # ── Tab 1: sofort ──────────────────────────────────────────────────────
    def _build_now(self) -> QWidget:
        page = QWidget()
        layout = QFormLayout(page)
        self.now_prompt = QPlainTextEdit()
        self.now_prompt.setPlaceholderText(
            "Was soll die Engine tun? Der Lead Engineer zerlegt es in Workitems.")
        self.now_prompt.setMinimumHeight(200)
        self.now_title = QLineEdit()
        self.now_title.setPlaceholderText("Leer lassen = erste Zeile des Auftrags")
        self.now_max_rounds = self._rounds_spin()
        layout.addRow("Auftrag:", self.now_prompt)
        layout.addRow("Titel:", self.now_title)
        layout.addRow("Planungsrunden:", self.now_max_rounds)
        return page

    def _rounds_spin(self) -> QSpinBox:
        """Wie oft der Lead nachplanen darf, wenn das Review unzufrieden ist."""
        spin = QSpinBox()
        spin.setRange(1, 10)
        spin.setValue(2)
        spin.setToolTip("Wie oft der Lead nachplanen darf, wenn das Review das Ergebnis fuer "
                        "ungenuegend haelt. Beim letzten erlaubten Durchgang endet der Lauf "
                        "als failed. Default 2.")
        return spin

    # ── Tab 2: stehend ─────────────────────────────────────────────────────
    def _build_standing(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        form = QFormLayout()

        self.s_prompt = QPlainTextEdit()
        self.s_prompt.setPlaceholderText(
            "Der Auftrag, der bei jedem Vorkommen ausgefuehrt wird — z.B.\n"
            "'Durchsuche die gespeicherten X-Posts der letzten 7 Tage nach ... "
            "und bewerte semantisch.'")
        self.s_prompt.setMinimumHeight(150)
        form.addRow("Auftrag:", self.s_prompt)

        self.s_title = QLineEdit()
        self.s_title.setPlaceholderText("Leer lassen = erste Zeile des Auftrags")
        form.addRow("Titel:", self.s_title)

        self.s_role = QComboBox()
        for role in self.roles:
            name = str(role.get("name")) if isinstance(role, dict) else str(role)
            effort = (str(role.get("reasoning_effort") or "default")
                      if isinstance(role, dict) else "default")
            self.s_role.addItem(f"{name}  ·  Reasoning {effort}", name)
            if isinstance(role, dict) and role.get("description"):
                self.s_role.setItemData(self.s_role.count() - 1, str(role["description"]),
                                        Qt.ItemDataRole.ToolTipRole)
        self.s_role.setToolTip("Das Reasoning-Level kommt aus der Rolle (Tabelle roles) — "
                               "der Lead Engineer kann es pro Aufgabe überschreiben.")
        form.addRow("Rolle:", self.s_role)

        self.s_kind = QComboBox()
        self.s_kind.addItems(["Einmalig zu einem Zeitpunkt", "Taeglich", "Woechentlich",
                              "Feste Rate (Minuten)"])
        self.s_kind.currentIndexChanged.connect(self._sync_visibility)
        form.addRow("Zeitregel:", self.s_kind)

        self.s_at = QDateTimeEdit()
        self.s_at.setCalendarPopup(True)
        self.s_at.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.s_at.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        form.addRow("Zeitpunkt:", self.s_at)

        self.s_clock = QLineEdit("18:00")
        self.s_clock.setFixedWidth(90)
        form.addRow("Uhrzeit:", self.s_clock)

        self.s_weekday = QComboBox()
        self.s_weekday.addItems(WEEKDAYS)
        self.s_weekday.setCurrentIndex(6)
        form.addRow("Wochentag:", self.s_weekday)

        self.s_minutes = QSpinBox()
        self.s_minutes.setRange(5, 1440)          # Minimum 300 s — wie schedules.py
        self.s_minutes.setValue(60)
        form.addRow("Alle Minuten:", self.s_minutes)

        self.s_zone = QComboBox()
        self.s_zone.addItems(ZONES)
        form.addRow("Zeitzone:", self.s_zone)

        self.s_max_runs = QSpinBox()
        self.s_max_runs.setRange(0, 100000)
        self.s_max_runs.setSpecialValueText("ohne Ende")
        form.addRow("Nach N Laeufen beenden:", self.s_max_runs)

        self.s_max_rounds = self._rounds_spin()
        form.addRow("Planungsrunden:", self.s_max_rounds)

        self._form = form
        outer.addLayout(form)
        hint = QLabel("Der erste Termin wird von der Sekretaerin berechnet "
                      "(Zeitzone, Sommerzeit). Diese Fenster schreibt nur die Regel.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        self._sync_visibility()
        return page

    def _sync_visibility(self) -> None:
        """Nur die Felder zeigen, die zur gewaehlten Regel gehoeren — samt Label."""
        kind = self.s_kind.currentIndex()
        wanted = {id(self.s_at): kind == 0, id(self.s_clock): kind in (1, 2),
                  id(self.s_weekday): kind == 2, id(self.s_minutes): kind == 3}
        for row in range(self._form.rowCount()):
            field = self._form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            label = self._form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if field is None or field.widget() is None:
                continue
            visible = wanted.get(id(field.widget()), True)
            field.widget().setVisible(visible)
            if label is not None and label.widget() is not None:
                label.widget().setVisible(visible)

    # ── Ergebnis ───────────────────────────────────────────────────────────
    def now_values(self) -> tuple[str, str, int]:
        return (self.now_prompt.toPlainText(), self.now_title.text().strip(),
                int(self.now_max_rounds.value()))

    def standing_values(self) -> tuple[str, str, dict[str, Any], str, int]:
        """(prompt, role, regel, titel) — die Regel in der Form, die
        schedules.py versteht."""
        kind = self.s_kind.currentIndex()
        rule: dict[str, Any] = {}
        if kind == 0:
            rule = {"kind": "once", "at": self.s_at.dateTime().toPython().astimezone().isoformat()}
        elif kind in (1, 2):
            rule = {"kind": "repeat", "every": "weekly" if kind == 2 else "daily",
                    "time": self.s_clock.text().strip(), "time_zone": self.s_zone.currentText()}
            if kind == 2:
                rule["weekday"] = self.s_weekday.currentIndex()
        else:
            rule = {"kind": "repeat", "every_seconds": int(self.s_minutes.value()) * 60}
        if self.s_max_runs.value() > 0:
            rule["max_runs"] = int(self.s_max_runs.value())
        # currentData(), nicht currentText(): der Text traegt das Reasoning-Level.
        return (self.s_prompt.toPlainText(), str(self.s_role.currentData() or ""), rule,
                self.s_title.text().strip(), int(self.s_max_rounds.value()))
