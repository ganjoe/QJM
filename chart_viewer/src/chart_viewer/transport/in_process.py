"""In-Process transport implementation for same-process Agent <-> Viewer communication."""

from __future__ import annotations
import logging
from typing import Callable, List
from chart_viewer.transport.base import AgentTransport
from chart_viewer.models.envelope import Envelope

logger = logging.getLogger(__name__)


class InProcessTransport(AgentTransport):
    """Direct Python object passing transport with zero serialization and lowest latency."""

    def __init__(self, name: str = "InProcess"):
        self.name = name
        self.peer: InProcessTransport | None = None
        self._handlers: List[Callable[[Envelope], None]] = []
        self._connected: bool = False

    def set_peer(self, peer: InProcessTransport) -> None:
        """Bind with the peer endpoint."""
        self.peer = peer

    def send_command(self, envelope: Envelope) -> None:
        if not self._connected:
            raise ConnectionError(f"{self.name}: Transport is not connected")
        if self.peer and self.peer._connected:
            self.peer._dispatch(envelope)
        else:
            logger.warning(f"{self.name}: Peer is not connected, dropping envelope {envelope.type}")

    def on_event(self, handler: Callable[[Envelope], None]) -> None:
        self._handlers.append(handler)

    def _dispatch(self, envelope: Envelope) -> None:
        for handler in self._handlers:
            try:
                handler(envelope)
            except Exception as e:
                logger.exception(f"{self.name} error in handler for {envelope.type}: {e}")

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


def create_in_process_pair() -> tuple[InProcessTransport, InProcessTransport]:
    """Create a linked pair of (viewer_transport, agent_transport)."""
    viewer_transport = InProcessTransport("ViewerTransport")
    agent_transport = InProcessTransport("AgentTransport")
    viewer_transport.set_peer(agent_transport)
    agent_transport.set_peer(viewer_transport)
    return viewer_transport, agent_transport
