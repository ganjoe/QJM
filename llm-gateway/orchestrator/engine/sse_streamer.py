"""
SSE (Server-Sent Events) stream management for job result delivery.

Each job gets its own asyncio.Queue of SSEEvents. The API layer consumes
from the queue via an async generator, and the dispatcher/pool_manager push
events into it as tokens arrive from the LLM backend.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator

logger = logging.getLogger("engine.sse")


class SSEEvent:
    """A single Server-Sent Event ready for wire encoding."""

    __slots__ = ("event_type", "data", "event_id")

    def __init__(self, event_type: str, data: Any, event_id: str | None = None):
        self.event_type = event_type
        self.data = data
        self.event_id = event_id

    def encode(self) -> str:
        """Encode as SSE wire format (text/event-stream)."""
        lines: list[str] = []
        if self.event_id:
            lines.append(f"id: {self.event_id}")
        lines.append(f"event: {self.event_type}")
        payload = self.data if isinstance(self.data, str) else json.dumps(self.data)
        for segment in payload.split("\n"):
            lines.append(f"data: {segment}")
        # SSE spec: events are separated by a blank line
        lines.append("")
        lines.append("")
        return "\n".join(lines)


class SSEManager:
    """
    Manages per-job SSE event queues.

    Lifecycle:
      1. create_stream(job_id)   — called when a job is submitted
      2. push_event(...)         — called by dispatcher/pool as tokens arrive
      3. push_done(job_id)       — signals stream completion
      4. stream(job_id)          — async generator consumed by the SSE endpoint
      5. cleanup(job_id)         — frees the queue after the client disconnects
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[SSEEvent | None]] = {}
        # TODO: Persist events to Supabase for replay / audit

    # ------------------------------------------------------------------
    # Queue lifecycle
    # ------------------------------------------------------------------

    def create_stream(self, job_id: str) -> None:
        """Create a new SSE event queue for a job."""
        if job_id not in self._queues:
            self._queues[job_id] = asyncio.Queue()
            logger.debug("Created SSE stream for job %s", job_id)

    def cleanup(self, job_id: str) -> None:
        """Remove the queue for a completed/failed job."""
        self._queues.pop(job_id, None)
        logger.debug("Cleaned up SSE stream for job %s", job_id)

    # ------------------------------------------------------------------
    # Event producers (called by engine internals)
    # ------------------------------------------------------------------

    def push_event(self, job_id: str, event_type: str, data: Any) -> None:
        """Push an event into a job's SSE queue (non-blocking, fire-and-forget)."""
        queue = self._queues.get(job_id)
        if queue is None:
            logger.warning("No SSE stream for job %s — event dropped", job_id)
            return
        try:
            queue.put_nowait(SSEEvent(event_type=event_type, data=data))
        except asyncio.QueueFull:
            logger.error("SSE queue full for job %s — event dropped", job_id)

    def push_done(self, job_id: str) -> None:
        """Signal successful stream completion and close the generator."""
        self.push_event(job_id, "done", {"status": "completed"})
        self._push_sentinel(job_id)

    def push_error(self, job_id: str, error: str) -> None:
        """Push an error event and close the stream."""
        self.push_event(job_id, "error", {"error": error})
        self._push_sentinel(job_id)

    def _push_sentinel(self, job_id: str) -> None:
        """Push a None sentinel to terminate the async generator."""
        queue = self._queues.get(job_id)
        if queue:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------
    # Event consumer (called by the SSE endpoint)
    # ------------------------------------------------------------------

    async def stream(self, job_id: str) -> AsyncGenerator[str, None]:
        """
        Async generator that yields encoded SSE strings for a job.

        Terminates when a None sentinel is received (stream done/error)
        or when no queue exists for the job.
        """
        queue = self._queues.get(job_id)
        if queue is None:
            yield SSEEvent("error", {"error": f"No stream found for job {job_id}"}).encode()
            return

        try:
            while True:
                event = await queue.get()
                if event is None:
                    # Sentinel — stream is finished
                    break
                yield event.encode()
        finally:
            # Cleanup after client disconnect or stream completion
            self.cleanup(job_id)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    @property
    def active_streams(self) -> int:
        """Number of currently active SSE streams."""
        return len(self._queues)
