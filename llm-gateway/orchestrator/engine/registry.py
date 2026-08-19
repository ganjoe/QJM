"""
Endpoint Registry and Health Monitor.

The registry is the single source of truth for all registered LLM endpoints.
The health monitor runs as a background asyncio task, periodically probing
each endpoint and updating its state (ONLINE / DEGRADED / OFFLINE).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from engine.models import (
    EndpointConfig,
    EndpointState,
    EndpointStatus,
)

logger = logging.getLogger("engine.registry")

# ---------------------------------------------------------------------------
# State-transition thresholds (module-level constants)
# ---------------------------------------------------------------------------
# Consecutive health-check failures before transitioning states.
# These are defaults — per-endpoint tuning can be added later.
DEGRADED_AFTER_FAILURES = 2      # ONLINE → DEGRADED
OFFLINE_AFTER_FAILURES = 10      # DEGRADED → OFFLINE
LATENCY_DEGRADED_MS = 5000       # Response time above this → DEGRADED


class EndpointRegistry:
    """
    Thread-safe (asyncio-safe) registry of all LLM endpoints.

    Maintains both static configuration and live runtime state
    (health, active slots, latency) for each endpoint.
    """

    def __init__(self) -> None:
        self._endpoints: dict[str, EndpointState] = {}
        self._lock = asyncio.Lock()
        # TODO: Persist to Supabase

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def register(self, config: EndpointConfig) -> EndpointState:
        """Register a new endpoint or replace an existing one."""
        async with self._lock:
            state = EndpointState(config=config, status=EndpointStatus.OFFLINE)
            self._endpoints[config.endpoint_id] = state
            logger.info(
                "Registered endpoint: %s (%s)",
                config.endpoint_id,
                config.name or config.base_url,
            )
            return state

    async def unregister(self, endpoint_id: str) -> bool:
        """Remove an endpoint from the registry."""
        async with self._lock:
            removed = self._endpoints.pop(endpoint_id, None)
            if removed:
                logger.info("Unregistered endpoint: %s", endpoint_id)
                return True
            return False

    # ------------------------------------------------------------------
    # Capability lookups
    # ------------------------------------------------------------------

    async def get_by_capability(self, capability: str) -> list[EndpointState]:
        """
        Return all *active* endpoints (ONLINE or DEGRADED) that advertise
        the given capability, sorted by health then priority.
        """
        async with self._lock:
            results = [
                state
                for state in self._endpoints.values()
                if capability in state.config.capabilities
                and state.status in (EndpointStatus.ONLINE, EndpointStatus.DEGRADED)
            ]
        # ONLINE before DEGRADED, then lower priority number = higher priority
        results.sort(key=lambda s: (
            0 if s.status == EndpointStatus.ONLINE else 1,
            s.config.priority,
        ))
        return results

    async def get_all_capabilities(self) -> set[str]:
        """Return all distinct capabilities advertised by any registered endpoint."""
        async with self._lock:
            caps: set[str] = set()
            for s in self._endpoints.values():
                caps.update(s.config.capabilities)
            return caps

    async def get_fallback_endpoints(self, capability: str) -> list[EndpointState]:
        """Return cloud/fallback endpoints that cover a capability."""
        async with self._lock:
            results = [
                state
                for state in self._endpoints.values()
                if capability in state.config.fallback_for
                and state.status in (EndpointStatus.ONLINE, EndpointStatus.DEGRADED)
            ]
        results.sort(key=lambda s: s.config.priority)
        return results

    # ------------------------------------------------------------------
    # Slot management
    # ------------------------------------------------------------------

    async def acquire_slot(self, endpoint_id: str) -> bool:
        """Try to acquire a concurrency slot. Returns False if at max."""
        async with self._lock:
            state = self._endpoints.get(endpoint_id)
            if state and state.active_slots < state.config.max_concurrency:
                state.active_slots += 1
                logger.debug(
                    "Slot acquired on %s (%d/%d)",
                    endpoint_id, state.active_slots, state.config.max_concurrency,
                )
                return True
            return False

    async def release_slot(self, endpoint_id: str) -> None:
        """Release a concurrency slot after a request completes."""
        async with self._lock:
            state = self._endpoints.get(endpoint_id)
            if state and state.active_slots > 0:
                state.active_slots -= 1
                logger.debug(
                    "Slot released on %s (%d/%d)",
                    endpoint_id, state.active_slots, state.config.max_concurrency,
                )

    # ------------------------------------------------------------------
    # Health updates
    # ------------------------------------------------------------------

    async def update_health(
        self,
        endpoint_id: str,
        *,
        is_healthy: bool,
        latency_ms: Optional[float] = None,
    ) -> None:
        """
        Update an endpoint's health state based on a probe result.

        State machine:
          ONLINE  → DEGRADED  after DEGRADED_AFTER_FAILURES consecutive failures
          DEGRADED → OFFLINE  after OFFLINE_AFTER_FAILURES consecutive failures
          OFFLINE → ONLINE    on first successful probe (auto-recovery)
          ONLINE  → DEGRADED  if latency > LATENCY_DEGRADED_MS
        """
        async with self._lock:
            state = self._endpoints.get(endpoint_id)
            if not state:
                return

            state.last_health_check = datetime.now(timezone.utc)
            state.last_latency_ms = latency_ms

            if is_healthy:
                state.consecutive_failures = 0
                if state.status != EndpointStatus.ONLINE:
                    logger.info(
                        "Endpoint %s recovered → ONLINE (was %s)",
                        endpoint_id, state.status.value,
                    )
                state.status = EndpointStatus.ONLINE

                # Latency-based degradation (even when reachable)
                if latency_ms is not None and latency_ms > LATENCY_DEGRADED_MS:
                    state.status = EndpointStatus.DEGRADED
                    logger.warning(
                        "Endpoint %s → DEGRADED (latency: %.0fms > %dms threshold)",
                        endpoint_id, latency_ms, LATENCY_DEGRADED_MS,
                    )
            else:
                state.consecutive_failures += 1
                if state.consecutive_failures >= OFFLINE_AFTER_FAILURES:
                    if state.status != EndpointStatus.OFFLINE:
                        logger.warning(
                            "Endpoint %s → OFFLINE (%d consecutive failures)",
                            endpoint_id, state.consecutive_failures,
                        )
                    state.status = EndpointStatus.OFFLINE
                elif state.consecutive_failures >= DEGRADED_AFTER_FAILURES:
                    if state.status != EndpointStatus.DEGRADED:
                        logger.warning(
                            "Endpoint %s → DEGRADED (%d consecutive failures)",
                            endpoint_id, state.consecutive_failures,
                        )
                    state.status = EndpointStatus.DEGRADED

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def get_all(self) -> list[dict]:
        """Serialize all endpoints + state for the dashboard / admin API."""
        async with self._lock:
            return [
                {
                    "endpoint_id": s.config.endpoint_id,
                    "name": s.config.name,
                    "type": s.config.type.value,
                    "base_url": s.config.base_url,
                    "api_schema": s.config.api_schema.value,
                    "model_name": s.config.model_name,
                    "capabilities": s.config.capabilities,
                    "status": s.status.value,
                    "active_slots": s.active_slots,
                    "max_concurrency": s.config.max_concurrency,
                    "priority": s.config.priority,
                    "last_health_check": (
                        s.last_health_check.isoformat() if s.last_health_check else None
                    ),
                    "last_latency_ms": s.last_latency_ms,
                    "consecutive_failures": s.consecutive_failures,
                }
                for s in self._endpoints.values()
            ]

    async def get_endpoint(self, endpoint_id: str) -> Optional[EndpointState]:
        """Get a single endpoint's full state."""
        async with self._lock:
            return self._endpoints.get(endpoint_id)


# ---------------------------------------------------------------------------
# Health Monitor (background task)
# ---------------------------------------------------------------------------

class HealthMonitor:
    """
    Periodically probes all registered endpoints and updates their state.

    Runs as a single asyncio background task started at app startup.
    Uses each endpoint's own health_check config for URL, interval and timeout.
    """

    def __init__(self, registry: EndpointRegistry) -> None:
        self.registry = registry
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Launch the monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(), name="health-monitor")
        logger.info("HealthMonitor started")

    async def stop(self) -> None:
        """Gracefully stop the monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("HealthMonitor stopped")

    async def _monitor_loop(self) -> None:
        """Main loop — runs health checks and sleeps between rounds."""
        while self._running:
            try:
                endpoints_data = await self.registry.get_all()

                # Fan-out health checks concurrently
                tasks = []
                for ep_dict in endpoints_data:
                    ep = await self.registry.get_endpoint(ep_dict["endpoint_id"])
                    if ep and ep.config.health_check:
                        tasks.append(self._check_endpoint(ep))

                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

                # Sleep for the shortest configured interval across all endpoints
                min_interval = await self._min_interval(endpoints_data)
                await asyncio.sleep(min_interval)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("HealthMonitor unexpected error")
                await asyncio.sleep(5)

    async def _min_interval(self_outer, endpoints_data: list[dict]) -> float:
        """Determine the shortest health-check interval across all endpoints."""
        # Default fallback interval
        minimum = 10.0
        for ep_dict in endpoints_data:
            ep = await self_outer.registry.get_endpoint(ep_dict["endpoint_id"])
            if ep and ep.config.health_check:
                minimum = min(minimum, ep.config.health_check.interval_seconds)
        return minimum

    async def _check_endpoint(self, endpoint: EndpointState) -> None:
        """Perform a single health-check probe."""
        hc = endpoint.config.health_check
        if not hc:
            return

        eid = endpoint.config.endpoint_id
        try:
            async with httpx.AsyncClient() as client:
                t0 = time.monotonic()
                resp = await asyncio.wait_for(
                    client.get(hc.url),
                    timeout=hc.timeout_seconds,
                )
                latency_ms = (time.monotonic() - t0) * 1000
                is_healthy = resp.status_code == 200

            await self.registry.update_health(
                eid, is_healthy=is_healthy, latency_ms=latency_ms,
            )

        except (asyncio.TimeoutError, httpx.RequestError, OSError) as exc:
            await self.registry.update_health(eid, is_healthy=False)
            logger.debug("Health check failed for %s: %s", eid, exc)

        except Exception as exc:
            await self.registry.update_health(eid, is_healthy=False)
            logger.warning("Unexpected health check error for %s: %s", eid, exc)
