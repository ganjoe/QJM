"""Regression: der WebSocket-Empfangsweg des Agenten darf nie blockieren.

Symptom (Chartviewer): Ein Klick auf einen Ticker aktualisiert den Chart
manchmal nicht, nach einer Weile erscheinen alle angeklickten Charts
hintereinander, und das Control Panel meldet "keine Antwort vom Agent".

Ursache: Der Agent erledigte Symbolwechsel und Symbolsuche direkt im
Empfangs-Thread und wartete dort blockierend auf HTTP (teils ohne Timeout).
Solange nahm der Thread keine Nachrichten mehr ab: der Nachrichtenpuffer des
Websockets lief voll, Keepalive-Pings blieben unbeantwortet, die Verbindung
wurde mit 1011 getrennt. Die Antworten kamen nie - oder als Schwall, sobald der
Thread wieder frei war.

Diese Tests halten fest:
* der Empfangsweg kehrt sofort zurueck (Datenabruf laeuft auf Workern),
* mehrere Klicks auf dasselbe Fenster werden auf den letzten zusammengefasst,
* ein geschlossenes Fenster wird nicht nachtraeglich wieder geoeffnet,
* die Symbolsuche blockiert den Empfangsweg nicht,
* ein langsamer Viewer-Client blockiert keine anderen Sends.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.agent.control_service import ControlConfig, ControlService
from chart_viewer.models.envelope import MessageKind, make_envelope
from chart_viewer.transport.websocket_server import WebSocketServerTransport


class FakeTransport:
    """Minimaler Transport-Ersatz (der Agent ruft nur on_event/send_command)."""

    def __init__(self) -> None:
        self.sent: List[Any] = []
        self.handlers: List[Any] = []
        self._connected = True

    def on_event(self, handler) -> None:
        self.handlers.append(handler)

    def send_command(self, envelope) -> None:
        self.sent.append(envelope)


class FakeResponse:
    def read(self) -> bytes:
        return b"{}"

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class UrlOpenRecorder:
    """urlopen-Ersatz: protokolliert Symbole und kann den ersten Aufruf anhalten."""

    def __init__(self, hold_first: bool = False, hold_seconds: float = 2.0) -> None:
        self.symbols: List[str] = []
        self.first_started = threading.Event()
        self.release = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self._hold_first = hold_first
        self._hold_seconds = hold_seconds
        self._calls = 0
        self._running = 0
        self._lock = threading.Lock()

    def __call__(self, req, *args, **kwargs) -> FakeResponse:
        with self._lock:
            self._calls += 1
            call = self._calls
            symbol = ""
            if getattr(req, "data", None):
                try:
                    symbol = str(json.loads(req.data.decode()).get("symbol") or "")
                except Exception:
                    symbol = ""
            self.symbols.append(symbol)
            self._running += 1
            self.idle.clear()
        try:
            if call == 1:
                self.first_started.set()
                if self._hold_first:
                    self.release.wait(self._hold_seconds)
            return FakeResponse()
        finally:
            with self._lock:
                self._running -= 1
                if self._running <= 0:
                    self.idle.set()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        return self.idle.wait(timeout)


def make_agent() -> ChartAgent:
    agent = ChartAgent(transport=FakeTransport())
    agent.layout_ledger["win_a"] = {
        "window_id": "win_a",
        "symbol": "OLD",
        "timeframe": {"unit": "D", "multiplier": 1},
        "sync_group_id": "stocks",
    }
    return agent


def send_change_symbol(agent: ChartAgent, window_id: str, symbol: str, preset: Optional[str] = None) -> None:
    payload: Dict[str, Any] = {"window_id": window_id, "symbol": symbol}
    if preset:
        payload["preset"] = preset
    agent._on_envelope(
        make_envelope(
            "window.change_symbol",
            payload=payload,
            kind=MessageKind.EVENT,
            window_id=window_id,
        )
    )


def test_symbol_change_does_not_block_the_receive_path(monkeypatch):
    recorder = UrlOpenRecorder(hold_first=True)
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    agent = make_agent()

    started = time.monotonic()
    send_change_symbol(agent, "win_a", "NEW")
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, f"Empfangsweg blockierte {elapsed:.2f}s auf dem Datenabruf"
    assert recorder.first_started.wait(2.0), "Symbolwechsel wurde nicht angestossen"
    recorder.release.set()
    assert recorder.wait_idle(5.0)


def test_rapid_clicks_coalesce_to_the_last_symbol(monkeypatch):
    recorder = UrlOpenRecorder(hold_first=True)
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    agent = make_agent()

    send_change_symbol(agent, "win_a", "AAA")
    assert recorder.first_started.wait(2.0)
    send_change_symbol(agent, "win_a", "BBB")
    send_change_symbol(agent, "win_a", "CCC")
    time.sleep(0.2)  # BBB/CCC muessen in der Warteschlange liegen

    recorder.release.set()
    assert recorder.wait_idle(5.0)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and recorder.symbols != ["AAA", "CCC"]:
        time.sleep(0.02)
    assert recorder.symbols == ["AAA", "CCC"], recorder.symbols


def test_closed_window_is_not_reopened_by_a_late_render(monkeypatch):
    recorder = UrlOpenRecorder(hold_first=True)
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    agent = make_agent()

    send_change_symbol(agent, "win_a", "AAA")
    assert recorder.first_started.wait(2.0)
    send_change_symbol(agent, "win_a", "BBB")

    # Nutzer schliesst das Fenster, bevor der nachlaufende Wechsel greift
    agent._on_envelope(
        make_envelope(
            "window.closed",
            payload={"window_id": "win_a"},
            kind=MessageKind.EVENT,
            window_id="win_a",
        )
    )
    recorder.release.set()
    assert recorder.wait_idle(5.0)
    time.sleep(0.3)  # einem nachlaufenden Render Gelegenheit geben

    assert recorder.symbols == ["AAA"], recorder.symbols
    assert agent._pending_symbol_changes == {}


class FakeControlAgent:
    def __init__(self) -> None:
        self.transport = FakeTransport()
        self.layout_ledger: Dict[str, dict] = {}


def test_search_symbols_is_answered_off_the_receive_thread():
    agent = FakeControlAgent()
    service = ControlService(
        agent,
        config=ControlConfig(pca_url="http://pca.test", http_timeout_s=1.0),
    )
    started = threading.Event()
    release = threading.Event()

    def slow_search(params):
        started.set()
        release.wait(3.0)
        return {"results": [], "match_count": 0}

    service._ops["search_symbols"] = slow_search

    began = time.monotonic()
    service.handle("cp_1", "search_symbols", {"query": "AA"})
    elapsed = time.monotonic() - began

    assert elapsed < 0.2, f"Suche blockierte den Empfangsweg {elapsed:.2f}s"
    assert started.wait(2.0), "Suche wurde nicht ausgefuehrt"
    release.set()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not agent.transport.sent:
        time.sleep(0.01)
    assert agent.transport.sent, "Keine Antwort auf die Suche"
    service.shutdown()


class BlockingClient:
    """Client, dessen send() haengt (langsamer/verstopfter Viewer)."""

    def __init__(self, delay: float = 1.0) -> None:
        self.delay = delay
        self.received = threading.Event()
        self.remote_address = ("127.0.0.1", 1)

    def send(self, data) -> None:
        time.sleep(self.delay)
        self.received.set()


class RecordingClient:
    def __init__(self) -> None:
        self.received = threading.Event()
        self.remote_address = ("127.0.0.1", 2)

    def send(self, data) -> None:
        self.received.set()


def test_slow_client_does_not_stall_the_send_path():
    transport = WebSocketServerTransport(host="127.0.0.1", port=0)
    slow = BlockingClient(delay=1.0)
    fast = RecordingClient()
    transport._register_client(slow)
    transport._register_client(fast)

    envelope = make_envelope(
        "window.open",
        payload={"window_id": "win_a"},
        kind=MessageKind.COMMAND,
        window_id="win_a",
    )

    began = time.monotonic()
    transport.send_command(envelope)
    elapsed = time.monotonic() - began

    assert elapsed < 0.2, f"send_command blockierte {elapsed:.2f}s auf einem langsamen Client"
    assert fast.received.wait(2.0), "schneller Client bekam die Nachricht nicht"
    assert slow.received.wait(3.0), "langsamer Client bekam die Nachricht nicht"
    transport.disconnect()
