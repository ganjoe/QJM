"""WebSocket server transport for Agent-side communication according to Section 1 & 3."""

from __future__ import annotations
import logging
import threading
from typing import Callable, List, Set
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


class WebSocketServerTransport(AgentTransport):
    """Server-side transport used by the Python Agent to talk to remote Desktop Viewers."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host = host
        self.port = port
        self._handlers: List[Callable[[Envelope], None]] = []
        self._clients: Set[ServerConnection] = set()
        self._server = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._connected = False
        self._seq = 0

    def send_command(self, envelope: Envelope) -> None:
        """Broadcast command to all connected viewer clients."""
        with self._lock:
            if not self._clients:
                return
            if envelope.sequence == 0:
                self._seq += 1
                envelope.sequence = self._seq

            binary_data = encode_envelope(envelope)
            dead_clients = []
            for client in list(self._clients):
                try:
                    client.send(binary_data)
                except Exception as e:
                    logger.warning(f"Failed to send to client: {e}")
                    dead_clients.append(client)

            for dead in dead_clients:
                self._clients.discard(dead)

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

    def is_connected(self) -> bool:
        return self._connected

    def _run_server(self) -> None:
        with serve(self._handle_client, self.host, self.port) as server:
            self._server = server
            server.serve_forever()

    def _handle_client(self, client: ServerConnection) -> None:
        with self._lock:
            self._clients.add(client)
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
            with self._lock:
                self._clients.discard(client)
