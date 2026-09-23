"""Startet das WIQ-Dashboard (Client).

  python run_dashboard.py [--url http://10.20.0.23:8799]

Der Client laeuft auf dem Arbeitsrechner und spricht nur mit dem Dashboard-
Server auf dem QJM-Host. Er braucht deshalb weder Datenbank-Treiber noch
Zugangsdaten — nur PySide6.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from wiq_dashboard.api import ApiError, DashboardApi  # noqa: E402
from wiq_dashboard.config import ClientConfig  # noqa: E402
from wiq_dashboard.theme import STYLESHEET  # noqa: E402
from wiq_dashboard.window import DashboardWindow  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="WIQ-Dashboard")
    parser.add_argument("--url", default=None, help="Adresse des Dashboard-Servers")
    args = parser.parse_args()

    config = ClientConfig.from_env(args.url)
    api = DashboardApi(config.base_url)

    app = QApplication(sys.argv)
    app.setApplicationName("WIQ Dashboard")
    app.setOrganizationName("qjm")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)

    try:
        api.health()
    except ApiError as exc:
        print(f"Dashboard-Server nicht erreichbar: {exc}", file=sys.stderr)
        print(f"Erwartet unter {config.base_url} — laeuft der Dienst auf dem QJM-Host?",
              file=sys.stderr)
        return 1

    window = DashboardWindow(api, config.base_url)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
