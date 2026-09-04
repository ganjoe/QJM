"""Reference Python Agent implementation according to Section 1, 8, & 11."""

from __future__ import annotations
import logging
from typing import Dict, List, Optional
from chart_viewer.transport.base import AgentTransport
from chart_viewer.models.envelope import Envelope, MessageKind, make_envelope, create_message_id
from chart_viewer.models.entities import WindowState

logger = logging.getLogger(__name__)


class ChartAgent:
    """Reference Python Agent: sole authoritative source of truth for layout and data."""

    def __init__(self, transport: AgentTransport):
        self.transport = transport
        self.transport.on_event(self._on_envelope)

        # Authoritative Layout Ledger (Section 11)
        self.layout_ledger: Dict[str, dict] = {}
        self.series_data: Dict[str, dict] = {}
        self.received_events: List[Envelope] = []
        self._seq = 0

    def start(self) -> None:
        self.transport.connect()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _on_envelope(self, envelope: Envelope) -> None:
        self.received_events.append(envelope)
        logger.info(f"Agent received: {envelope.type} (kind={envelope.kind})")

        if envelope.type == "viewer.ready":
            self._handle_viewer_ready(envelope)

        elif envelope.type == "window.closed":
            win_id = envelope.window_id or (envelope.payload.get("window_id") if isinstance(envelope.payload, dict) else None)
            if win_id:
                logger.info(f"Agent recorded window {win_id} closed by user")
                self.layout_ledger.pop(win_id, None)

        elif envelope.type == "annotation.moved":
            logger.info(f"Agent recorded annotation move: {envelope.payload}")

        elif envelope.type == "resync.request":
            # Viewer requested full resync -> push layout.restore
            self.restore_layout()

    def _handle_viewer_ready(self, envelope: Envelope) -> None:
        """Viewer connected -> push stored layout if any (Section 11)."""
        logger.info("Viewer ready, restoring layout...")
        self.restore_layout()

    def open_window(
        self,
        window_id: str,
        symbol: str,
        timeframe_unit: str = "D",
        multiplier: int = 1,
        sync_group_id: Optional[str] = None,
        position: Optional[dict] = None,
        size: Optional[dict] = None,
    ) -> None:
        """Agent opens a new window in the Viewer (Section 8)."""
        win_info = {
            "window_id": window_id,
            "symbol": symbol,
            "timeframe": {"unit": timeframe_unit, "multiplier": multiplier},
            "sync_group_id": sync_group_id,
            "position": position or {"x": 100, "y": 100},
            "size": size or {"width": 900, "height": 600},
        }
        self.layout_ledger[window_id] = win_info

        env = make_envelope(
            msg_type="window.open",
            payload=win_info,
            kind=MessageKind.COMMAND,
            window_id=window_id,
            sequence=self._next_seq(),
        )
        self.transport.send_command(env)

    def send_snapshot(self, window_id: str, snapshot_data: dict) -> None:
        """Push initial/updated full snapshot to window."""
        self.series_data[window_id] = snapshot_data
        env = make_envelope(
            msg_type="snapshot.full",
            payload=snapshot_data,
            kind=MessageKind.COMMAND,
            window_id=window_id,
            sequence=self._next_seq(),
        )
        self.transport.send_command(env)

    def append_bar(self, window_id: str, bar_data: dict) -> None:
        """Push completed candle."""
        env = make_envelope(
            msg_type="bar.append",
            payload={"bar": bar_data},
            kind=MessageKind.COMMAND,
            window_id=window_id,
            sequence=self._next_seq(),
        )
        self.transport.send_command(env)

    def send_tick(self, window_id: str, price: float, volume: float = 0.0) -> None:
        """Push real-time price tick (fire-and-forget, coalesced)."""
        env = make_envelope(
            msg_type="tick.update",
            payload={"price": price, "volume": volume},
            kind=MessageKind.EVENT,
            window_id=window_id,
            sequence=self._next_seq(),
        )
        self.transport.send_command(env)

    def restore_layout(self) -> None:
        """Section 11: Push layout.restore containing all open windows."""
        windows_list = list(self.layout_ledger.values())
        if not windows_list:
            return

        env = make_envelope(
            msg_type="layout.restore",
            payload={"windows": windows_list},
            kind=MessageKind.COMMAND,
            sequence=self._next_seq(),
        )
        self.transport.send_command(env)

        # Push snapshots for each window
        for win_id, snap in self.series_data.items():
            self.send_snapshot(win_id, snap)
