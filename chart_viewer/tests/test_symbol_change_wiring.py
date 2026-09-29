"""Ende-zu-Ende: Ein Ticker-Klick blockiert den Empfangsweg nicht mehr.

Der In-Process-Transport dispatcht - wie der WebSocket-Empfangs-Thread - auf
dem aufrufenden Thread. Hier haengt statt der echten Control-API (die Daten
rendert) ein Stub, der verzoegert antwortet. Damit ist die Verdrahtung ohne
Backend pruefbar:

* der Klick kehrt sofort zurueck (der Render laeuft auf einem Worker),
* mehrere Klicks auf dasselbe Fenster rendern nur den letzten Ticker,
* die Verbindung bleibt waehrenddessen ansprechbar (Antworten kommen an).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List

from chart_viewer.agent import agent_client
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.config import ViewerConfig
from chart_viewer.transport.in_process import create_in_process_pair
from chart_viewer.ui.app import ViewerApp


class StubControlApi:
    """Minimaler /api/command-Ersatz: sammelt Symbole und antwortet verzoegert."""

    def __init__(self, delay: float = 0.0) -> None:
        self.symbols: List[str] = []
        self.delay = delay
        self.first_started = threading.Event()
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D102 - Teststub bleibt still
                pass

            def do_POST(self):  # noqa: N802 - http.server-API
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode() if length else "{}"
                try:
                    body = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    body = {}
                with outer._lock:
                    outer.symbols.append(str(body.get("symbol") or ""))
                    if len(outer.symbols) == 1:
                        outer.first_started.set()
                if outer.delay:
                    time.sleep(outer.delay)
                payload = b'{"status": "ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_click_does_not_block_and_only_the_last_symbol_is_rendered(qapp, monkeypatch):
    api = StubControlApi(delay=1.5)
    monkeypatch.setattr(agent_client, "CONTROL_API_URL", api.url)
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(
        config=ViewerConfig(control_panel_enabled=False), transport=viewer_transport
    )
    try:
        agent.start()
        app.start()
        app._handle_window_open({"window_id": "win_selftest_1d", "symbol": "AAA"}, b"")

        began = time.monotonic()
        app._on_symbol_change_requested("win_selftest_1d", "AAA")
        assert api.first_started.wait(3.0), "Der Render wurde nicht angestossen"
        # Zwei weitere Klicks, waehrend der erste Render noch laeuft.
        app._on_symbol_change_requested("win_selftest_1d", "BBB")
        app._on_symbol_change_requested("win_selftest_1d", "CCC")
        elapsed = time.monotonic() - began

        assert elapsed < 0.5, f"Die Klicks blockierten {elapsed:.2f}s auf dem Render"
        assert _wait_until(lambda: api.symbols == ["AAA", "CCC"]), api.symbols
    finally:
        app.shutdown()
        api.stop()
