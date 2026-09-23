"""Die Serverbruecke: ein Thread, eine Verbindung.

Alle Abfragen laufen hier, nicht im GUI-Thread. Das Fenster bleibt auch dann
bedienbar, wenn der Server langsam ist oder neu startet.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from .api import DashboardApi


class DbWorker(QObject):
    runs_loaded = Signal(object, object)      # roots, occurrences
    detail_loaded = Signal(object, object)    # change_item, items
    roles_loaded = Signal(object)             # Liste der Rollennamen
    message = Signal(str)                     # Erfolgsmeldung nach einem Schreiben
    failed = Signal(str)                      # Fehlertext

    def __init__(self, api: DashboardApi) -> None:
        super().__init__()
        self._api = api

    @Slot(str, str, str)
    def load_runs(self, search: str, state: str, kind: str) -> None:
        try:
            roots, occurrences = self._api.fetch_runs(search, state, kind)
            self.runs_loaded.emit(roots, occurrences)
        except Exception as exc:  # noqa: BLE001 - jeder Fehler geht ins Fenster
            self.failed.emit(f"Laden fehlgeschlagen: {exc}")

    @Slot(str)
    def load_detail(self, change_item_id: str) -> None:
        try:
            change_item, items = self._api.fetch_detail(change_item_id)
            self.detail_loaded.emit(change_item, items)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Detail fehlgeschlagen: {exc}")

    @Slot()
    def load_roles(self) -> None:
        try:
            self.roles_loaded.emit(self._api.roles())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Rollen fehlgeschlagen: {exc}")

    @Slot(str, str, int)
    def submit(self, prompt: str, title: str, max_rounds: int) -> None:
        try:
            change_item_id = self._api.submit(prompt, title or None, max_rounds or None)
            self.message.emit("Auftrag eingereicht: " + change_item_id)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Einreichen fehlgeschlagen: {exc}")

    @Slot(str, str, object, str, int)
    def create_schedule(self, prompt: str, role: str, rule: dict[str, Any], title: str,
                        max_rounds: int) -> None:
        try:
            definition_id = self._api.create_schedule(prompt, role, rule, title or None,
                                                      max_rounds or None)
            self.message.emit("Stehende Aufgabe angelegt: " + definition_id)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Anlegen fehlgeschlagen: {exc}")

    @Slot(str)
    def cancel_schedule(self, definition_id: str) -> None:
        try:
            self._api.cancel_schedule(definition_id)
            self.message.emit("Abgeschaltet.")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Abschalten fehlgeschlagen: {exc}")

    @Slot(str)
    def run_schedule_now(self, definition_id: str) -> None:
        try:
            self._api.run_schedule_now(definition_id)
            self.message.emit("Termin gezogen — startet beim naechsten Tick.")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Ausfuehren fehlgeschlagen: {exc}")
