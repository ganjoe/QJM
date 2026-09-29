"""WebSocket binary MessagePack transport according to Section 1, 3, & 10."""

from __future__ import annotations
import asyncio
import faulthandler
import logging
import os
import tempfile
import threading
import time
from datetime import datetime
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
from chart_viewer.config import ViewerConfig

logger = logging.getLogger(__name__)

# Diagnose des Clients. Der Viewer laeuft auf einem fremden Rechner: haengt er
# (keine Nachrichten, keine Keepalive-Antworten), ist das ohne Log von aussen
# nicht zu sehen. Standard: <temp>/chart_viewer_client.log. Ueber CV_CLIENT_LOG
# umstellbar, ueber CV_CLIENT_LOG=off abschaltbar.
CV_CLIENT_LOG = os.environ.get("CV_CLIENT_LOG") or os.path.join(
    tempfile.gettempdir(), "chart_viewer_client.log"
)
# Nach so vielen Sekunden ohne Lebenszeichen des Empfangs-Threads (bzw. in
# "connecting"/"reconnecting") wird ein Thread-Dump in die Diagnose-Datei
# geschrieben - er zeigt, wo der Client wirklich steht.
CV_CLIENT_STALL_DUMP_SEC = float(os.environ.get("CV_CLIENT_STALL_DUMP_SEC", "45"))
# Empfangs-Tick: beweist, dass die Empfangsschleife auch im Leerlauf laeuft.
CV_CLIENT_RECV_TICK_SEC = float(os.environ.get("CV_CLIENT_RECV_TICK_SEC", "5"))

# Wie der Server: grosszuegiges Keepalive. Der Client haengt gelegentlich kurz
# (Ausgabe/Netz) - mit 20 s Ping-Timeout bricht die Sitzung dann ab, obwohl sie
# sich Sekunden spaeter wieder faengt.
CV_CLIENT_PING_INTERVAL = float(os.environ.get("CV_CLIENT_PING_INTERVAL", "30"))
CV_CLIENT_PING_TIMEOUT = float(os.environ.get("CV_CLIENT_PING_TIMEOUT", "120"))
# Eingangspuffer: kurze Haenger des GUI-Threads duerfen den Empfang nicht
# blockieren (voller Puffer stoppt die Ping-Antworten).
CV_CLIENT_MAX_QUEUE = int(os.environ.get("CV_CLIENT_MAX_QUEUE", "256"))

_log_handlers_attached = False


def diag_log(message: str, *, dump_threads: bool = False) -> None:
    """Eine Zeile in die Client-Diagnose schreiben (blockiert die GUI nie)."""
    if str(CV_CLIENT_LOG).lower() in ("", "off", "none"):
        return
    try:
        with open(CV_CLIENT_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
            if dump_threads:
                faulthandler.dump_traceback(file=fh, all_threads=True)
    except Exception:  # noqa: BLE001 - Diagnose darf nie stoeren
        logger.debug("diagnostic log not writable: %s", CV_CLIENT_LOG)


def _attach_websocket_file_log() -> None:
    """websockets-DEBUG (Pings/Pongs, Close-Gruende) in dieselbe Datei schreiben."""
    global _log_handlers_attached
    if _log_handlers_attached or str(CV_CLIENT_LOG).lower() in ("", "off", "none"):
        return
    _log_handlers_attached = True
    try:
        handler = logging.FileHandler(CV_CLIENT_LOG, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    for name in ("websockets.client", "websockets.protocol"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        lg.addHandler(handler)


class WebSocketTransport(AgentTransport):
    """WebSocket client transport using binary msgspec MessagePack frames."""

    def __init__(
        self,
        config: ViewerConfig,
        url: str = "ws://127.0.0.1:8765",
    ):
        self.url = url
        self.config = config
        self._handlers: List[Callable[[Envelope], None]] = []
        self._connect_handlers: List[Callable[[], None]] = []
        self._disconnect_handlers: List[Callable[[], None]] = []
        self._ws = None
        self._connected = False
        self._should_run = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Sequence tracking
        self._last_received_sequence = -1
        self._send_sequence = 0
        self._last_resync_time = 0.0

        # Diagnose: Zustand der Verbindungsschleife + Lebenszeichen
        self._loop_state = "idle"  # idle | connecting | connected | reconnecting
        self._state_since = time.monotonic()
        self._beat_at = time.monotonic()  # letztes Lebenszeichen des Recv-Threads
        self._last_rx_at = time.monotonic()  # letzte empfangene Nachricht
        self._recv_tick_s = CV_CLIENT_RECV_TICK_SEC

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

    def on_connect(self, handler: Callable[[], None]) -> None:
        """Register a callback invoked after the connection is (re-)established."""
        self._connect_handlers.append(handler)

    def on_disconnect(self, handler: Callable[[], None]) -> None:
        """Register a callback invoked after the connection was lost."""
        self._disconnect_handlers.append(handler)

    def _notify(self, handlers: List[Callable[[], None]]) -> None:
        for handler in list(handlers):
            try:
                handler()
            except Exception as e:
                logger.exception(f"Error in connection state handler: {e}")

    def _dispatch(self, envelope: Envelope) -> None:
        for handler in self._handlers:
            try:
                handler(envelope)
            except Exception as e:
                logger.exception(f"Error in envelope handler: {e}")

    def connect(self) -> None:
        if self._connected or self._should_run:
            return
        self._should_run = True
        _attach_websocket_file_log()
        diag_log(f"--- transport starting, connecting to {self.url} ---")
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        threading.Thread(target=self._stall_watchdog, name="cv-watchdog", daemon=True).start()

    def disconnect(self) -> None:
        self._should_run = False
        was_connected = self._connected
        with self._lock:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
            self._connected = False
        if was_connected:
            self._notify(self._disconnect_handlers)

    def is_connected(self) -> bool:
        return self._connected

    def _set_loop_state(self, state: str) -> None:
        self._loop_state = state
        self._state_since = time.monotonic()

    def _recv_with_tick(self, ws):
        """recv() mit Tick: beweist auch im Leerlauf, dass die Schleife laeuft.

        Ohne Tick waere ein haengender Empfangs-Thread nicht von einem ruhigen
        Markt zu unterscheiden - genau das machte den Client bisher unsichtbar.
        """
        if self._recv_tick_s <= 0:
            return ws.recv()
        try:
            return ws.recv(timeout=self._recv_tick_s)
        except TimeoutError:
            return None
        except TypeError:  # aeltere websockets-Version ohne timeout-Parameter
            logger.info("websockets unterstuetzt recv(timeout=...) nicht - Tick deaktiviert")
            self._recv_tick_s = 0.0
            return ws.recv()

    def _stall_watchdog(self) -> None:
        """Schreibt einen Thread-Dump, wenn der Transport haengt.

        Der Viewer laeuft auf einem fremden Rechner: ein haengender
        Verbindungs-Thread ist von aussen unsichtbar (die GUI laeuft weiter,
        aber es kommen keine Nachrichten und keine Keepalive-Antworten mehr).
        """
        while self._should_run:
            time.sleep(2.0)
            if not self._should_run:
                return
            now = time.monotonic()
            if self._loop_state == "connected":
                silent = now - self._beat_at
                if silent > CV_CLIENT_STALL_DUMP_SEC:
                    diag_log(
                        f"receive loop silent for {silent:.0f}s "
                        f"(state=connected, latency={getattr(self._ws, 'latency', None)})",
                        dump_threads=True,
                    )
                    self._beat_at = now
            elif self._loop_state in ("connecting", "reconnecting"):
                stuck = now - self._state_since
                if stuck > CV_CLIENT_STALL_DUMP_SEC:
                    diag_log(
                        f"transport stuck in state '{self._loop_state}' for {stuck:.0f}s",
                        dump_threads=True,
                    )
                    self._state_since = now

    def _run_loop(self) -> None:
        """Background thread with reconnect loop and exponential backoff."""
        backoff_ms = self.config.reconnect_backoff_initial_ms
        max_backoff_ms = self.config.reconnect_backoff_max_ms
        factor = self.config.reconnect_backoff_factor

        while self._should_run:
            self._set_loop_state("connecting")
            try:
                logger.info(f"Connecting to {self.url}...")
                with ws_connect(
                    self.url,
                    max_size=None,
                    max_queue=CV_CLIENT_MAX_QUEUE,
                    ping_interval=CV_CLIENT_PING_INTERVAL,
                    ping_timeout=CV_CLIENT_PING_TIMEOUT,
                ) as ws:
                    with self._lock:
                        self._ws = ws
                        self._connected = True
                        # Reset backoff on successful connection
                        backoff_ms = self.config.reconnect_backoff_initial_ms
                        self._last_received_sequence = -1
                    self._set_loop_state("connected")
                    now = time.monotonic()
                    self._beat_at = now
                    self._last_rx_at = now
                    diag_log(f"connected to {self.url}")

                    print(f"[OK] Erfolgreich mit Agent Server verbunden!", flush=True)
                    print(f"[INFO] Desktop Viewer ist betriebsbereit. Warte auf Chart-Befehle vom Agenten...\n", flush=True)
                    logger.info(f"Connected to {self.url}")
                    # Notify handshake readiness
                    self._on_connection_established()

                    while self._should_run:
                        try:
                            msg = self._recv_with_tick(ws)
                            self._beat_at = time.monotonic()
                            if msg is None:
                                continue  # Leerlauf-Tick: Schleife lebt
                            if isinstance(msg, (bytes, bytearray)):
                                self._handle_binary_frame(bytes(msg))
                        except ConnectionClosed as exc:
                            logger.warning("WebSocket connection closed by peer")
                            diag_log(
                                f"connection closed ({exc.__class__.__name__}: {exc}); "
                                f"last message {time.monotonic() - self._last_rx_at:.1f}s ago",
                                dump_threads=True,
                            )
                            break
            except Exception as e:
                if self._should_run:
                    logger.debug(f"WebSocket connection error: {e}")
                    diag_log(f"connection failed: {e!r}")

            with self._lock:
                was_connected = self._connected
                self._connected = False
                self._ws = None

            if was_connected:
                self._notify(self._disconnect_handlers)

            if not self._should_run:
                break

            self._set_loop_state("reconnecting")
            # Sleep backoff before reconnecting
            sleep_sec = backoff_ms / 1000.0
            logger.info(f"Reconnecting in {sleep_sec:.2f}s...")
            time.sleep(sleep_sec)
            backoff_ms = min(max_backoff_ms, int(backoff_ms * factor))
        self._set_loop_state("idle")

    def _on_connection_established(self) -> None:
        """Notify connect handlers; ViewerApp performs the viewer.ready handshake."""
        logger.info("Connection established, notifying connect handlers")
        self._notify(self._connect_handlers)

    def _handle_binary_frame(self, data: bytes) -> None:
        """Decode binary msgpack frame and check sequence monotonicity."""
        self._last_rx_at = time.monotonic()
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
