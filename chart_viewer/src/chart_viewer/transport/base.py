"""Transport abstraction interface according to Section 1 & Section 3."""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Callable
from chart_viewer.models.envelope import Envelope


class AgentTransport(ABC):
    """Abstract base transport interface for Agent <-> Viewer communication."""

    @abstractmethod
    def send_command(self, envelope: Envelope) -> None:
        """Send an envelope (command or event) over the transport."""
        pass

    @abstractmethod
    def on_event(self, handler: Callable[[Envelope], None]) -> None:
        """Register a callback for incoming envelopes."""
        pass

    @abstractmethod
    def connect(self) -> None:
        """Establish the connection."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection."""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the transport is active and connected."""
        pass
