"""Wo steht der Dashboard-Server?

Der Client laeuft auf dem Arbeitsrechner (Windows/macOS), der Server auf dem
QJM-Host. Deshalb ist die Serveradresse das einzige, was konfiguriert wird —
Datenbank-Zugangsdaten gibt es hier bewusst nicht.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_URL = "http://10.20.0.23:8799"


@dataclass(frozen=True)
class ClientConfig:
    base_url: str

    @staticmethod
    def from_env(explicit: str | None = None) -> "ClientConfig":
        return ClientConfig(
            base_url=(explicit or os.environ.get("WIQ_DASHBOARD_URL") or DEFAULT_URL).rstrip("/")
        )
