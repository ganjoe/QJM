"""WebSocket server transport for Agent-side communication according to Section 1 & 3."""

from __future__ import annotations
import logging
import os
import queue
import threading
from typing import Any, Callable, Dict, List, Optional, Set
from websockets.sync.server import serve, ServerConnection
from websockets.exceptions import ConnectionClosed

from chart_viewer.transport.base import AgentTransport
from chart_viewer.models.envelope import (
    Envelope,
    encode_envelope,
    decode_envelope,
    make_envelope,
    MessageKind,
)

logger = logging.getLogger(__name__)

# Eingehende Nachrichten, die der Empfangs-Thread noch nicht abgenommen hat.
# Der Puffer entkoppelt kurze Lastspitzen; ist er voll, liest der interne
# Empfangs-Thread des Websockets nicht mehr weiter und Keepalive-Pings bleiben
# unbeantwortet. Deshalb ist er gross genug fuer Spitzen - und der Empfangs-
# Thread darf ohnehin nie länger blockieren (siehe agent_client/control_service).
CV_WS_MAX_QUEUE = max(1, int(os.environ.get("CV_WS_MAX_QUEUE", "256")))
# Ausgehende Nachrichten je Client, die auf ihren Writer-Thread warten. Ist der
# Rueckstand groesser, gilt der Client als tot (er bekommt beim Reconnect einen
# vollstaendigen Resync) - besser als unbegrenzt Speicher oder ein blockierter
# Empfangs-Thread.
CV_WS_SEND_QUEUE = max(1, int(os.environ.get("CV_WS_SEND_QUEUE", "512")))
# Keepalive grosszuegig: der Viewer-Client kann kurz haengen (Ausgabe/Netz). Ein
# 20-s-Ping-Timeout trennt dann die Verbindung und reisst laufende Antworten mit;
# mit 30 s Ping und 120 s Timeout ueberlebt die Sitzung die Spitze.
CV_WS_PING_INTERVAL = float(os.environ.get("CV_WS_PING_INTERVAL", "30"))
CV_WS_PING_TIMEOUT = float(os.environ.get("CV_WS_PING_TIMEOUT", "120"))


class ClientChannel:
    """Ausgehende Warteschlange eines Viewer-Clients mit eigenem Writer-Thread.

    `send_command` darf nie auf einen langsamen Client warten: der Aufrufer ist
    oft der Empfangs-Thread (window.open, layout.restore, Snapshot) oder ein
    Control-Worker. Blockiert der Send, stehen alle Sender hinter derselben
    Sperre - inklusive der Keepalive-Antworten. Deshalb wird hier nur eingereiht;
    ein Thread je Client schreibt die Nachrichten in Reihenfolge.
    """

    def __init__(self, client: Any, maxsize: int = CV_WS_SEND_QUEUE) -> None:
        self.client = client
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=maxsize)
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cv-send", daemon=True)
        self._thread.start()

    def send(self, data: bytes) -> bool:
        """Einreihen. False heisst: Kanal geschlossen oder Rueckstand zu gross."""
        if self._closed.is_set():
            return False
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            logger.warning("Send queue of a viewer client is full - dropping the client")
            self._closed.set()
            return False
        return True

    def close(self) -> None:
        self._closed.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._closed.is_set():
                    return
                continue
            if item is None:
                return
            try:
                self.client.send(item)
            except Exception as e:
                logger.warning(f"Failed to send to client: {e}")
                self._closed.set()
                return


class WebSocketServerTransport(AgentTransport):
    """Server-side transport used by the Python Agent to talk to remote Desktop Viewers."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host = host
        self.port = port
        self._handlers: List[Callable[[Envelope], None]] = []
        self._clients: Set[ServerConnection] = set()
        self._channels: Dict[Any, ClientChannel] = {}
        self._server = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._connected = False
        self._seq = 0

    # ── client registry ────────────────────────────────────────────────────

    def _register_client(self, client: Any) -> ClientChannel:
        channel = ClientChannel(client)
        with self._lock:
            self._clients.add(client)
            self._channels[client] = channel
        return channel

    def _unregister_client(self, client: Any) -> None:
        with self._lock:
            self._clients.discard(client)
            channel = self._channels.pop(client, None)
        if channel is not None:
            channel.close()

    def _drop_client(self, client: Any, reason: str) -> None:
        logger.warning(f"Dropping viewer client: {reason}")
        self._unregister_client(client)
        try:
            client.close()
        except Exception:
            pass

    def send_command(self, envelope: Envelope) -> None:
        """Queue a command/event for every connected viewer client.

        Non-blocking by design: a slow client must never stall the caller (the
        websocket receive thread) or the other clients.
        """
        with self._lock:
            if not self._channels:
                return
            if envelope.sequence == 0:
                self._seq += 1
                envelope.sequence = self._seq
            binary_data = encode_envelope(envelope)
            targets = list(self._channels.items())

        for client, channel in targets:
            if not channel.send(binary_data):
                self._drop_client(client, "send failed or backlog too large")

    def on_event(self, handler: Callable[[Envelope], None]) -> None:
        self._handlers.append(handler)

    def _dispatch(self, envelope: Envelope) -> None:
        for handler in self._handlers:
            try:
                handler(envelope)
            except Exception as e:
                logger.exception(f"Error in server envelope handler: {e}")

    def connect(self) -> None:
        """Start the WebSocket server in a background thread."""
        if self._connected:
            return
        self._connected = True
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()
        logger.info(f"Agent WebSocket server started on {self.host}:{self.port}")

    def disconnect(self) -> None:
        self._connected = False
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None
        with self._lock:
            clients = list(self._channels.keys())
        for client in clients:
            self._unregister_client(client)

    def is_connected(self) -> bool:
        return self._connected

    def _run_server(self) -> None:
        with serve(
            self._handle_client,
            self.host,
            self.port,
            max_size=None,
            max_queue=CV_WS_MAX_QUEUE,
            ping_interval=CV_WS_PING_INTERVAL,
            ping_timeout=CV_WS_PING_TIMEOUT,
        ) as server:
            self._server = server
            server.serve_forever()

    def _handle_client(self, client: ServerConnection) -> None:
        self._register_client(client)
        logger.info(f"Viewer client connected from {client.remote_address}")

        try:
            for message in client:
                if isinstance(message, (bytes, bytearray)):
                    try:
                        envelope = decode_envelope(bytes(message))
                        self._dispatch(envelope)
                    except Exception as e:
                        logger.error(f"Error decoding client message: {e}")
        except ConnectionClosed:
            logger.info("Viewer client disconnected")
        finally:
            self._unregister_client(client)
