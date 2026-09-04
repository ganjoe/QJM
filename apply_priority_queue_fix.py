#!/usr/bin/env python3
"""
Applies deduplication and priority upgrade logic to stock-data-node's priority_queue.py.
"""
import sys

TARGET = "/home/daniel/stock-data-node/src/priority_queue.py"

NEW_CONTENT = '''"""
priority_queue.py — T-004
Thread-safe priority queue with preemption support and deduplication. (F-FNC-010, F-FNC-020)
"""
from __future__ import annotations

import heapq
import logging
import threading
import time
from typing import Optional

from models import DownloadPriority, DownloadRequest, IPriorityQueue

logger = logging.getLogger(__name__)


class DownloadQueue(IPriorityQueue):
    """
    Thread-safe min-heap priority queue with deduplication.
    DownloadRequest.__lt__ ensures API (prio 1) < WATCHER (prio 2) < STALENESS (prio 3),
    and among equal priorities items are ordered by creation time (FIFO).

    Deduplication:
      - Tickers with the same timeframe are deduplicated.
      - If a duplicate request arrives with equal or lower priority, it is discarded.
      - If a request arrives with strictly higher priority (e.g. API over STALENESS),
        its priority is upgraded and it is inserted into the heap.
    """

    _SUMMARY_INTERVAL_S = 10.0
    _SUMMARY_STEP = 100

    def __init__(self) -> None:
        self._heap: list[DownloadRequest] = []
        self._pending: dict[tuple[str, str], DownloadPriority] = {}
        self._prio_counts: dict[int, int] = {1: 0, 2: 0, 3: 0}
        self._lock = threading.Lock()
        self._enqueued = 0
        self._last_summary = time.monotonic()

    def enqueue(self, request: DownloadRequest) -> None:
        """Pushes a request onto the priority heap if not already queued with equal/higher priority."""
        key = (request.ticker.upper(), request.timeframe)
        with self._lock:
            existing_prio = self._pending.get(key)
            if existing_prio is not None:
                # If already queued with equal or higher priority (lower numeric value), ignore duplicate
                if int(existing_prio) <= int(request.priority):
                    return
                # Upgrading priority: decrement old count
                old_p = int(existing_prio)
                if old_p in self._prio_counts and self._prio_counts[old_p] > 0:
                    self._prio_counts[old_p] -= 1

            prio_int = int(request.priority)
            self._pending[key] = request.priority
            self._prio_counts[prio_int] = self._prio_counts.get(prio_int, 0) + 1
            heapq.heappush(self._heap, request)
            self._enqueued += 1
            queue_size = len(self._pending)

            now = time.monotonic()
            if (
                queue_size % self._SUMMARY_STEP == 0
                or (now - self._last_summary) >= self._SUMMARY_INTERVAL_S
            ):
                self._last_summary = now
                logger.info(
                    "Queue size: %d (last enqueued: %s/%s; total enqueued: %d)",
                    queue_size,
                    request.ticker,
                    request.timeframe,
                    self._enqueued,
                )

    def dequeue(self) -> Optional[DownloadRequest]:
        """Pops the highest-priority request. Discards superseded duplicates. Returns None if queue is empty."""
        with self._lock:
            while self._heap:
                item = heapq.heappop(self._heap)
                key = (item.ticker.upper(), item.timeframe)
                current_prio = self._pending.get(key)

                # If key is not in pending, it was already handled by an upgraded entry
                if current_prio is None:
                    continue
                # If this item has lower priority than what is pending, a higher-priority entry exists
                if int(item.priority) > int(current_prio):
                    continue

                self._pending.pop(key, None)
                prio_int = int(current_prio)
                if prio_int in self._prio_counts and self._prio_counts[prio_int] > 0:
                    self._prio_counts[prio_int] -= 1

                logger.debug(
                    "Dequeued %s/%s (prio=%s) — queue size: %d",
                    item.ticker, item.timeframe,
                    item.priority.name, len(self._pending)
                )
                return item
            return None

    def has_higher_priority_waiting(self, current_priority: DownloadPriority) -> bool:
        """
        Returns True if there is a queued request with strictly higher priority
        (i.e., lower numeric value) than current_priority.
        Used during chunk downloads to decide whether to preempt.
        """
        with self._lock:
            curr_val = int(current_priority)
            return any(
                count > 0
                for prio, count in self._prio_counts.items()
                if prio < curr_val
            )

    def size(self) -> int:
        """Returns current unique queue depth."""
        with self._lock:
            return len(self._pending)
'''

with open(TARGET, "w", encoding="utf-8") as f:
    f.write(NEW_CONTENT)

print(f"Successfully updated {TARGET}")
