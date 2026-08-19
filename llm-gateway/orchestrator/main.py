import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from routers import models, routing, backends, workitems, endpoints, system
from engine import start_engine, stop_engine, registry
from engine.models import EndpointConfig, HealthCheckConfig, EndpointType, ApiSchema

logger = logging.getLogger("orchestrator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# Default endpoint registration from environment variables
# ---------------------------------------------------------------------------

async def _register_default_endpoints() -> None:
    """
    Register the homelab endpoints that were previously hardcoded in telemetry.py.

    Each endpoint is configured from environment variables so the system
    works out of the box with existing docker-compose.yml settings.
    """
    vllm_url = os.getenv("VLLM_URL", "http://localhost:8100")
    lmstudio_url = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
    ollama_local_url = os.getenv("OLLAMA_ROUTER_URL", "http://ollama:11434")
    ollama_rx6700_url = os.getenv("OLLAMA_RX6700_URL", "")
    ollama_macbook_url = os.getenv("OLLAMA_MACBOOK_URL", "")

    default_endpoints = [
        EndpointConfig(
            endpoint_id="vllm-rocm",
            name="vLLM (R9700 Pro 32GB ROCm)",
            type=EndpointType.MANAGED,
            base_url=f"{vllm_url}/v1",
            api_schema=ApiSchema.OPENAI,
            model_name=os.getenv("VLLM_MODEL", "bartowski/Qwen2.5-Coder-32B-Instruct-GGUF:Q4_K_M"),
            max_concurrency=2,
            capabilities=["fast", "reasoning", "coding"],
            health_check=HealthCheckConfig(
                url=f"{vllm_url}/health",
                interval_seconds=10,
                timeout_seconds=3.0,
            ),
            priority=1,
            docker_container=os.getenv("DOCKER_VLLM_CONTAINER", "llm-gw-vllm"),
        ),
        EndpointConfig(
            endpoint_id="lmstudio-docker",
            name="LM Studio (Docker Vulkan / GUI)",
            type=EndpointType.MANAGED,
            base_url=f"{lmstudio_url}/v1",
            api_schema=ApiSchema.OPENAI,
            max_concurrency=1,
            capabilities=["fast", "reasoning"],
            health_check=HealthCheckConfig(
                url=f"{lmstudio_url}/v1/models",
                interval_seconds=10,
                timeout_seconds=5.0,
            ),
            priority=2,
            docker_container=os.getenv("DOCKER_LMSTUDIO_CONTAINER", "llm-gw-lmstudio"),
        ),
        EndpointConfig(
            endpoint_id="ollama-local",
            name="Local Embeddings (Ollama Router)",
            type=EndpointType.MANAGED,
            base_url=ollama_local_url,
            api_schema=ApiSchema.OLLAMA,
            model_name="nomic-embed-text",
            max_concurrency=2,
            capabilities=["embeddings"],
            health_check=HealthCheckConfig(
                url=f"{ollama_local_url}/api/tags",
                interval_seconds=15,
                timeout_seconds=2.0,
            ),
            priority=1,
        ),
    ]

    # Optional network endpoints (only register if URL is configured)
    if ollama_rx6700_url:
        default_endpoints.append(EndpointConfig(
            endpoint_id="ollama-rx6700",
            name="Ollama RX 6700 XT",
            type=EndpointType.UNMANAGED,
            base_url=ollama_rx6700_url,
            api_schema=ApiSchema.OLLAMA,
            max_concurrency=2,
            capabilities=["fast"],
            health_check=HealthCheckConfig(
                url=f"{ollama_rx6700_url}/api/tags",
                interval_seconds=10,
                timeout_seconds=2.0,
            ),
            priority=3,
        ))

    if ollama_macbook_url:
        default_endpoints.append(EndpointConfig(
            endpoint_id="ollama-macbook",
            name="Ollama MacBook M1",
            type=EndpointType.UNMANAGED,
            base_url=ollama_macbook_url,
            api_schema=ApiSchema.OLLAMA,
            max_concurrency=1,
            capabilities=["fast"],
            health_check=HealthCheckConfig(
                url=f"{ollama_macbook_url}/api/tags",
                interval_seconds=10,
                timeout_seconds=2.0,
            ),
            priority=4,
        ))

    for ep in default_endpoints:
        await registry.register(ep)

    logger.info("Registered %d default endpoints", len(default_endpoints))


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for the LLM Router engine."""
    logger.info("Starting LLM Gateway Orchestrator & Router Engine…")
    await _register_default_endpoints()
    await start_engine()
    logger.info("Engine started — ready to accept WorkItems")
    yield
    logger.info("Shutting down engine…")
    await stop_engine()
    logger.info("Engine stopped")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="LLM Gateway Orchestrator & Router",
    description=(
        "Control plane, dashboard, and async LLM router with capability-based "
        "routing, SSE streaming, and health-monitored endpoint registry."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

# --- Existing admin routers ---
app.include_router(models.router)
app.include_router(routing.router)
app.include_router(backends.router)

# --- New engine routers ---
app.include_router(workitems.router)
app.include_router(endpoints.router)
app.include_router(system.router)

# --- Static dashboard ---
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def serve_dashboard():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "LLM Gateway Orchestrator API is running. Dashboard index.html not found."}


@app.get("/api/status")
async def get_system_status():
    from engine import pool_manager, sse_manager
    return {
        "status": "online",
        "service": "LLM Gateway Orchestrator & Router",
        "version": "2.0.0",
        "engine": {
            "pools": pool_manager.get_pool_stats(),
            "total_jobs": pool_manager.total_jobs,
            "active_streams": sse_manager.active_streams,
        },
    }
