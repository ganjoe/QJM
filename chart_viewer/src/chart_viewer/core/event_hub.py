"""EventHub managing window registry, crosshair sync, and downtime clamping according to Section 4."""

from __future__ import annotations
import logging
from typing import Callable, Dict, List, Optional
from chart_viewer.models.entities import WindowState
from chart_viewer.models.envelope import Envelope, make_envelope, MessageKind

logger = logging.getLogger(__name__)


class EventHub:
    """Central event routing and window registry within the viewer process."""

    def __init__(self):
        self._windows: Dict[str, WindowState] = {}
        self._listeners: Dict[str, List[Callable[[Envelope], None]]] = {}
        self._crosshair_listeners: List[Callable[[dict], None]] = []

    def register_window(self, window_state: WindowState) -> None:
        """Register a window in the Hub."""
        self._windows[window_state.window_id] = window_state

    def unregister_window(self, window_id: str) -> None:
        """Remove a window from registry."""
        self._windows.pop(window_id, None)

    def get_window(self, window_id: str) -> Optional[WindowState]:
        return self._windows.get(window_id)

    def get_all_windows(self) -> List[WindowState]:
        return list(self._windows.values())

    def on_crosshair_broadcast(self, listener: Callable[[dict], None]) -> None:
        self._crosshair_listeners.append(listener)

    def broadcast_crosshair(
        self,
        source_window_id: str,
        timestamp: int,
        bar_index_fraction: float,
        calendar_type: str = "continuous",
    ) -> None:
        """Broadcast crosshair to all matching windows in the same sync_group_id."""
        source_win = self._windows.get(source_window_id)
        source_group = source_win.sync_group_id if source_win else None

        payload = {
            "type": "crosshair.broadcast",
            "source_window_id": source_window_id,
            "timestamp": timestamp,
            "calendar_type": calendar_type,
            "bar_index_fraction": bar_index_fraction,
            "sync_group_id": source_group,
        }

        for listener in self._crosshair_listeners:
            try:
                listener(payload)
            except Exception as e:
                logger.exception(f"Error in crosshair listener: {e}")

    @staticmethod
    def clamp_timestamp_to_available_bars(
        target_timestamp: int,
        available_bar_timestamps: List[int],
    ) -> Optional[int]:
        """Clamping rule for crosshair across exchange downtime (Section 4).

        If target_timestamp falls in a downtime gap, clamps to the nearest available bar
        so the crosshair does NOT disappear.
        If completely outside [earliest, latest], returns None (crosshair disappears).
        """
        if not available_bar_timestamps:
            return None

        earliest = available_bar_timestamps[0]
        latest = available_bar_timestamps[-1]

        # Completely outside loaded historical range -> disappears
        if target_timestamp < earliest or target_timestamp > latest:
            return None

        # Binary search for closest available timestamp
        import bisect

        idx = bisect.bisect_left(available_bar_timestamps, target_timestamp)
        if idx == 0:
            return available_bar_timestamps[0]
        if idx >= len(available_bar_timestamps):
            return available_bar_timestamps[-1]

        before = available_bar_timestamps[idx - 1]
        after = available_bar_timestamps[idx]

        if abs(target_timestamp - before) <= abs(after - target_timestamp):
            return before
        return after
