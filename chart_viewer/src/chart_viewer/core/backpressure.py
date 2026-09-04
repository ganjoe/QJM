"""Backpressure and tick coalescing buffer according to Section 3.4."""

from __future__ import annotations
import threading
from typing import Callable, Dict, Optional
from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig


class TickCoalescer:
    """Coalesces incoming high-frequency tick updates per window_id.

    Maintains a single 'latest state' slot per window. When the render timer fires,
    it drains all pending updates, ensuring at most 1 repaint per window per tick interval
    and zero memory backlog growth even under 1000+ ticks/second.
    """

    def __init__(
        self,
        on_flush: Callable[[str, dict], None],
        config: ViewerConfig | None = None,
    ):
        self.on_flush = on_flush
        self.config = config or GLOBAL_CONFIG
        self._slots: Dict[str, dict] = {}
        self._dirty: Dict[str, bool] = {}
        self._lock = threading.Lock()

    def push_tick(self, window_id: str, tick_data: dict) -> None:
        """Merge tick into window slot (overwriting intermediate states)."""
        with self._lock:
            # Merge into latest known state
            current = self._slots.get(window_id, {})
            current.update(tick_data)
            self._slots[window_id] = current
            self._dirty[window_id] = True

    def flush(self) -> int:
        """Flush dirty slots to on_flush callback. Returns count of flushed windows."""
        to_flush: list[tuple[str, dict]] = []
        with self._lock:
            for win_id, is_dirty in list(self._dirty.items()):
                if is_dirty:
                    to_flush.append((win_id, dict(self._slots[win_id])))
                    self._dirty[win_id] = False

        for win_id, data in to_flush:
            self.on_flush(win_id, data)

        return len(to_flush)

    def clear(self, window_id: Optional[str] = None) -> None:
        with self._lock:
            if window_id:
                self._slots.pop(window_id, None)
                self._dirty.pop(window_id, None)
            else:
                self._slots.clear()
                self._dirty.clear()
