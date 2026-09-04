"""WebSocket binary MessagePack transport according to Section 1, 3, & 10."""

from __future__ import annotations
import asyncio
import logging
import threading
import time
from typing import Callable, List, Optional
import websockets
from websockets.sync.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from chart_viewer.transport.base import AgentTransport
from chart_viewer.models.envelope import (
    Envelope,
    MessageKind,
    encode_envelope,
    decode_envelope,
    make_envelope,
)
from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig

logger = logging.getLogger(__name__)


class WebSocketTransport(AgentTransport):
    """WebSocket client transport using binary msgspec MessagePack frames."""

    def __init__(
        self,
        url: str = "ws://127.0.0.1:8765",
        config: ViewerConfig | None = None,
    ):
        self.url = url
        self.config = config or GLOBAL_CONFIG
        self._handlers: List[Callable[[Envelope], None]] = []
        self._ws = None
        self._connected = False
        self._should_run = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Sequence tracking
        self._last_received_sequence = -1
        self._send_sequence = 0
        self._last_resync_time = 0.0

    def send_command(self, envelope: Envelope) -> None:
        """Send envelope as binary MessagePack."""
        with self._lock:
            if not self._connected or not self._ws:
                raise ConnectionError("WebSocket is not connected")
            # Stamp outbound sequence if not set
            if envelope.sequence == 0:
                self._send_sequence += 1
                envelope.sequence = self._send_sequence

            binary_data = encode_envelope(envelope)
            self._ws.send(binary_data)

    def on_event(self, handler: Callable[[Envelope], None]) -> None:
        self._handlers.append(handler)

    def _dispatch(self, envelope: Envelope) -> None:
        for handler in self._handlers:
            try:
                handler(envelope)
            except Exception as e:
                logger.exception(f"Error in envelope handler: {e}")

    def connect(self) -> None:
        if self._connected:
            return
        self._should_run = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self._should_run = False
        with self._lock:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
            self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def _run_loop(self) -> None:
        """Background thread with reconnect loop and exponential backoff."""
        backoff_ms = self.config.reconnect_backoff_initial_ms
        max_backoff_ms = self.config.reconnect_backoff_max_ms
        factor = self.config.reconnect_backoff_factor

        while self._should_run:
            try:
                logger.info(f"Connecting to {self.url}...")
                with ws_connect(self.url) as ws:
                    with self._lock:
                        self._ws = ws
                        self._connected = True
                        # Reset backoff on successful connection
                        backoff_ms = self.config.reconnect_backoff_initial_ms
                        self._last_received_sequence = -1

                    print(f"[OK] Erfolgreich mit Agent Server verbunden!", flush=True)
                    print(f"[INFO] Desktop Viewer ist betriebsbereit. Warte auf Chart-Befehle vom Agenten...\n", flush=True)
                    logger.info(f"Connected to {self.url}")
                    # Notify handshake readiness
                    self._on_connection_established()

                    while self._should_run:
                        try:
                            msg = ws.recv()
                            if isinstance(msg, (bytes, bytearray)):
                                self._handle_binary_frame(bytes(msg))
                        except ConnectionClosed:
                            logger.warning("WebSocket connection closed by peer")
                            break
            except Exception as e:
                if self._should_run:
                    logger.debug(f"WebSocket connection error: {e}")

            with self._lock:
                self._connected = False
                self._ws = None

            if not self._should_run:
                break

            # Sleep backoff before reconnecting
            sleep_sec = backoff_ms / 1000.0
            logger.info(f"Reconnecting in {sleep_sec:.2f}s...")
            time.sleep(sleep_sec)
            backoff_ms = min(max_backoff_ms, int(backoff_ms * factor))

    def _on_connection_established(self) -> None:
        """Trigger viewer.ready and request layout resync."""
        logger.info("Connection established, sending viewer.ready")
        ready_env = make_envelope(
            msg_type="viewer.ready",
            payload={"protocol_version": self.config.protocol_version},
            kind=MessageKind.EVENT,
        )
        try:
            self.send_command(ready_env)
        except Exception as e:
            logger.error(f"Failed to send viewer.ready: {e}")

    def _handle_binary_frame(self, data: bytes) -> None:
        """Decode binary msgpack frame and check sequence monotonicity."""
        try:
            envelope = decode_envelope(data)
        except Exception as e:
            logger.error(f"Failed to decode binary messagepack: {e}")
            # Section 3.2: Decode error -> rejection with error-reply
            err_env = make_envelope(
                msg_type="error",
                payload={"error": f"Decode failure: {str(e)}"},
                kind=MessageKind.ERROR,
            )
            try:
                self.send_command(err_env)
            except Exception:
                pass
            return

        # Section 3.1 & 10: Sequence gap detection
        # Note: fire-and-forget events like tick.update or initial messages might start sequence
        if envelope.sequence > 0:
            if self._last_received_sequence != -1 and envelope.sequence != self._last_received_sequence + 1:
                # Sequence gap detected!
                logger.warning(
                    f"Sequence gap detected! Expected {self._last_received_sequence + 1}, got {envelope.sequence}"
                )
                self._trigger_resync(envelope.window_id)
            self._last_received_sequence = envelope.sequence

        self._dispatch(envelope)

    def _trigger_resync(self, window_id: str | None) -> None:
        """Send resync.request respecting rate limiting."""
        now = time.time()
        if now - self._last_resync_time < self.config.resync_rate_limit_sec:
            logger.info("Resync request suppressed by rate-limiting (max 1 per 5s)")
            return

        self._last_resync_time = now
        logger.warning(f"Triggering full state reset / resync.request for window {window_id}")
        resync_env = make_envelope(
            msg_type="resync.request",
            payload={"window_id": window_id, "reason": "sequence_gap"},
            kind=MessageKind.COMMAND,
            window_id=window_id,
        )
        try:
            self.send_command(resync_env)
        except Exception as e:
            logger.error(f"Failed to send resync.request: {e}")
