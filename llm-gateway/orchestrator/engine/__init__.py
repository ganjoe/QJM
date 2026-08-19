"""
LLM Router Engine — core singleton instances.

All engine components are instantiated here as module-level singletons.
Cross-references (pool_manager ↔ dispatcher ↔ mcp_executor) are wired
immediately.

Call ``start_engine()`` / ``stop_engine()`` from FastAPI's lifespan to
bring up and tear down background tasks (health monitor, pool workers,
HTTP client pool, MCP transport).
"""
from engine.registry import EndpointRegistry, HealthMonitor
from engine.pool_manager import PoolManager
from engine.dispatcher import Dispatcher
from engine.sse_streamer import SSEManager
from engine.mcp_executor import MCPExecutor

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------
registry = EndpointRegistry()
health_monitor = HealthMonitor(registry)
sse_manager = SSEManager()
pool_manager = PoolManager(registry, sse_manager)
dispatcher = Dispatcher()
mcp_executor = MCPExecutor()

# Wire cross-references
pool_manager.set_dispatcher(dispatcher)
dispatcher.set_mcp_executor(mcp_executor)


# ---------------------------------------------------------------------------
# Lifecycle helpers (called from FastAPI lifespan)
# ---------------------------------------------------------------------------

async def start_engine() -> None:
    """Start all engine background tasks and transports."""
    await dispatcher.start()
    await mcp_executor.start()
    await health_monitor.start()
    await pool_manager.start()


async def stop_engine() -> None:
    """Gracefully stop all engine background tasks."""
    await pool_manager.stop()
    await health_monitor.stop()
    await mcp_executor.stop()
    await dispatcher.stop()
