"""Desktop Chart Viewer client entrypoint to run on your local machine (e.g. Windows PC)."""

from __future__ import annotations
import sys
import argparse
from PySide6.QtWidgets import QApplication

from chart_viewer.transport.websocket import WebSocketTransport
from chart_viewer.ui.app import ViewerApp
from chart_viewer.config import ViewerConfig


def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Desktop Chart Viewer Client (Qt/PySide6)")
    parser.add_argument(
        "--ws",
        type=str,
        default="ws://10.20.0.23:8765",
        help="WebSocket URL of the Agent Server (e.g. ws://10.20.0.23:8765)",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("ChartViewer")
    app.setQuitOnLastWindowClosed(False)

    config = ViewerConfig.from_env()
    transport = WebSocketTransport(url=args.ws, config=config)
    viewer = ViewerApp(transport=transport, config=config)

    print(f"Connecting to Agent Server at {args.ws}...")
    viewer.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
