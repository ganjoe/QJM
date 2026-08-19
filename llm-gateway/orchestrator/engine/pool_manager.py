"""
Capability Pool Manager — per-capability queues with slot-aware dispatching.

Each capability class (e.g. "fast", "reasoning", "coding") gets a FIFO
asyncio.Queue and a background worker that routes jobs to the best available
endpoint.  The pool manager is the bridge between job submission and the
dispatcher.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from engine.models import Job, JobStatus, RoutingDecision, EndpointState
from engine.registry import EndpointRegistry
from engine.sse_streamer import SSEManager

logger = logging.getLogger("engine.pool_manager")

# How long to wait before retrying when no endpoint is available
_NO_ENDPOINT_RETRY_SECONDS = 2


class PoolManager:
    """
    Manages per-capability FIFO queues and dispatches jobs to endpoints.

    Architecture:
      submit(job) → queue[capability] → _dispatch_loop → _find_endpoint → _execute_job
    """

    def __init__(
        self,
        registry: EndpointRegistry,
        sse_manager: SSEManager,
    ) -> None:
        self.registry = registry
        self.sse_manager = sse_manager

        self._queues: dict[str, asyncio.Queue[Job]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._jobs: dict[str, Job] = {}   # job_id → Job lookup
        self._running = False
        self._dispatcher = None           # set via set_dispatcher() to avoid circular import
        # TODO: Persist to Supabase

    def set_dispatcher(self, dispatcher) -> None:
        """Inject the Dispatcher reference after construction."""
        self._dispatcher = dispatcher

    # ------------------------------------------------------------------
    # Pool lifecycle
    # ------------------------------------------------------------------

    async def sync_pools(self) -> None:
        """Discover all capabilities from registry and ensure queues & workers exist."""
        capabilities = await self.registry.get_all_capabilities()
        for cap in capabilities:
            self._ensure_pool(cap)

    async def start(self) -> None:
        """Start dispatch workers for all existing pools."""
        self._running = True
        await self.sync_pools()
        for cap in list(self._queues):
            self._ensure_worker(cap)
        logger.info("PoolManager started (active pools: %s)", list(self._queues.keys()))

    async def stop(self) -> None:
        """Cancel all background workers."""
        self._running = False
        for task in self._workers.values():
            task.cancel()
        for task in self._workers.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        logger.info("PoolManager stopped")

    # ------------------------------------------------------------------
    # Job submission
    # ------------------------------------------------------------------

    async def submit(self, job: Job) -> None:
        """Enqueue a job in the appropriate capability pool."""
        cap = job.workitem.routing.capability_class
        queue = self._ensure_pool(cap)

        self._jobs[job.job_id] = job
        # TODO: Persist to Supabase

        self.sse_manager.create_stream(job.job_id)
        self.sse_manager.push_event(job.job_id, "status", {
            "status": "queued",
            "job_id": job.job_id,
            "capability": cap,
            "queue_position": queue.qsize(),
        })

        await queue.put(job)
        logger.info(
            "Job %s submitted to pool '%s' (queue depth: %d)",
            job.job_id, cap, queue.qsize(),
        )

    # ------------------------------------------------------------------
    # Internal: pool / worker management
    # ------------------------------------------------------------------

    def _ensure_pool(self, capability: str) -> asyncio.Queue:
        """Lazily create a queue and a dispatch worker for a capability."""
        if capability not in self._queues:
            self._queues[capability] = asyncio.Queue()
            logger.info("Created pool queue: %s", capability)
        if self._running:
            self._ensure_worker(capability)
        return self._queues[capability]

    def _ensure_worker(self, capability: str) -> None:
        """Ensure a background dispatch worker is running for the pool."""
        if capability in self._workers and not self._workers[capability].done():
            return
        self._workers[capability] = asyncio.create_task(
            self._dispatch_loop(capability),
            name=f"pool-worker-{capability}",
        )
        logger.info("Started dispatch worker for pool '%s'", capability)

    # ------------------------------------------------------------------
    # Dispatch loop (one per capability)
    # ------------------------------------------------------------------

    async def _dispatch_loop(self, capability: str) -> None:
        """
        Background worker: dequeue jobs and route them to endpoints.

        Routing strategy:
          1. Query registry for ONLINE/DEGRADED endpoints supporting *capability*.
          2. Try to acquire a slot on the highest-priority endpoint.
          3. If all local slots exhausted and allow_cloud_fallback, try fallbacks.
          4. If nothing available, re-enqueue with a short delay.
        """
        queue = self._queues[capability]

        while self._running:
            # Block until a job appears (with periodic timeout so we can
            # check self._running and exit cleanly).
            try:
                job = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # Attempt routing
            decision = await self._find_endpoint(job)

            if decision is None:
                # No endpoint available right now — re-enqueue
                self.sse_manager.push_event(job.job_id, "status", {
                    "status": "waiting",
                    "message": (
                        f"No available endpoint for '{capability}', "
                        f"retrying in {_NO_ENDPOINT_RETRY_SECONDS}s…"
                    ),
                })
                await asyncio.sleep(_NO_ENDPOINT_RETRY_SECONDS)
                await queue.put(job)
                continue

            # Mark job as running
            endpoint = decision.endpoint
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)
            job.assigned_endpoint = endpoint.config.endpoint_id
            # TODO: Persist to Supabase

            self.sse_manager.push_event(job.job_id, "status", {
                "status": "running",
                "job_id": job.job_id,
                "endpoint": endpoint.config.endpoint_id,
                "is_fallback": decision.is_fallback,
            })

            # Launch execution as a separate task so the worker can
            # immediately pick up the next queued job.
            asyncio.create_task(
                self._execute_job(job, endpoint),
                name=f"job-{job.job_id[:8]}",
            )

    # ------------------------------------------------------------------
    # Routing algorithm
    # ------------------------------------------------------------------

    async def _find_endpoint(self, job: Job) -> Optional[RoutingDecision]:
        """Select the best endpoint with a free slot for a job."""
        cap = job.workitem.routing.capability_class

        # 1 — Primary endpoints (local / managed / unmanaged), by priority
        primaries = await self.registry.get_by_capability(cap)
        for ep in primaries:
            if await self.registry.acquire_slot(ep.config.endpoint_id):
                return RoutingDecision(
                    endpoint=ep,
                    is_fallback=False,
                    reason=f"primary: {ep.config.endpoint_id}",
                )

        # 2 — Cloud / fallback endpoints (only if allowed)
        if job.workitem.routing.allow_cloud_fallback:
            fallbacks = await self.registry.get_fallback_endpoints(cap)
            for ep in fallbacks:
                if await self.registry.acquire_slot(ep.config.endpoint_id):
                    return RoutingDecision(
                        endpoint=ep,
                        is_fallback=True,
                        reason=f"cloud fallback: {ep.config.endpoint_id}",
                    )

        return None

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    async def _execute_job(self, job: Job, endpoint: EndpointState) -> None:
        """Run a job on an endpoint, streaming results into the SSE queue."""
        try:
            if self._dispatcher is None:
                raise RuntimeError("Dispatcher not configured on PoolManager")

            async for chunk in self._dispatcher.dispatch(job, endpoint):
                self.sse_manager.push_event(job.job_id, "chunk", chunk)

            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.now(timezone.utc)
            # TODO: Persist to Supabase
            self.sse_manager.push_done(job.job_id)
            logger.info(
                "Job %s completed on %s", job.job_id, endpoint.config.endpoint_id,
            )

        except Exception as exc:
            job.status = JobStatus.FAILED
            job.completed_at = datetime.now(timezone.utc)
            job.error = str(exc)
            # TODO: Persist to Supabase
            self.sse_manager.push_error(job.job_id, str(exc))
            logger.error("Job %s failed: %s", job.job_id, exc)

        finally:
            await self.registry.release_slot(endpoint.config.endpoint_id)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_job(self, job_id: str) -> Optional[Job]:
        """Lookup a job by ID."""
        return self._jobs.get(job_id)

    def get_pool_stats(self) -> dict:
        """Return queue depth and worker status per capability pool."""
        stats = {}
        for cap, queue in self._queues.items():
            stats[cap] = {
                "queue_length": queue.qsize(),
                "worker_active": (
                    cap in self._workers and not self._workers[cap].done()
                ),
            }
        return stats

    @property
    def total_jobs(self) -> int:
        """Total number of jobs tracked (all states)."""
        return len(self._jobs)
