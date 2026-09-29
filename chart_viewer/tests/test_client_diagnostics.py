"""Diagnose und Toleranz des Viewer-Clients.

Der Client laeuft auf einem fremden Rechner und haengt dort gelegentlich: keine
Nachrichten, keine Keepalive-Antworten - und ohne Log ist von aussen nicht zu
sehen, wo er steht. Diese Tests halten fest:
* der Client verbindet mit grosszuegigem Keepalive und grossem Eingangspuffer,
* ein haengender Transport wird mit Thread-Dump in die Diagnose-Datei geschrieben.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from chart_viewer import transport as transport_pkg  # noqa: F401
from chart_viewer.config import ViewerConfig
from chart_viewer.transport import websocket as ws_module
from chart_viewer.transport.websocket import WebSocketTransport


class FakeConnection:
    latency = 0.001

    def __init__(self) -> None:
        self.sent: List[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        pass

    def recv(self, timeout=None):
        raise TimeoutError


class FakeConnect:
    """Ersatz fuer websockets.sync.client.connect - zeichnet die Parameter auf."""

    def __init__(self) -> None:
        self.kwargs: Dict[str, Any] = {}
        self.calls = 0

    def __call__(self, url, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        fake = self

        class _Ctx:
            def __enter__(self_inner):
                return FakeConnection()

            def __exit__(self_inner, *exc_info):
                fake.kwargs["_closed"] = True
                return False

        return _Ctx()


def test_client_connects_with_tolerant_keepalive_and_large_queue(monkeypatch):
    fake = FakeConnect()
    monkeypatch.setattr(ws_module, "ws_connect", fake)
    transport = WebSocketTransport(config=ViewerConfig(), url="ws://127.0.0.1:1")

    transport.connect()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not fake.calls:
        time.sleep(0.01)
    transport.disconnect()

    assert fake.calls == 1
    assert fake.kwargs["ping_interval"] == ws_module.CV_CLIENT_PING_INTERVAL == 30
    assert fake.kwargs["ping_timeout"] == ws_module.CV_CLIENT_PING_TIMEOUT == 120
    assert fake.kwargs["max_queue"] == ws_module.CV_CLIENT_MAX_QUEUE == 256


def test_diag_log_writes_into_the_client_log(tmp_path, monkeypatch):
    log_path = tmp_path / "client.log"
    monkeypatch.setattr(ws_module, "CV_CLIENT_LOG", str(log_path))

    ws_module.diag_log("hallo welt", dump_threads=True)

    text = log_path.read_text(encoding="utf-8")
    assert "hallo welt" in text
    assert "Thread 0x" in text or "Current thread" in text  # faulthandler-Dump


def test_stall_watchdog_dumps_a_stuck_transport(tmp_path, monkeypatch):
    log_path = tmp_path / "client.log"
    monkeypatch.setattr(ws_module, "CV_CLIENT_LOG", str(log_path))
    transport = WebSocketTransport(config=ViewerConfig(), url="ws://127.0.0.1:1")
    transport._should_run = True
    transport._loop_state = "reconnecting"
    transport._state_since = time.monotonic() - 999.0

    import threading

    watcher = threading.Thread(target=transport._stall_watchdog, daemon=True)
    watcher.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not log_path.exists():
        time.sleep(0.05)
    transport._should_run = False
    watcher.join(timeout=3.0)

    assert log_path.exists(), "Der haengende Transport wurde nicht protokolliert"
    assert "stuck in state 'reconnecting'" in log_path.read_text(encoding="utf-8")
