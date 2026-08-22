import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from routers import models, routing, backends, containers, system, gemini_proxy
from services.switchyard_config import switchyard_config_manager
from services.telemetry import telemetry_collector

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for the Dashboard & Control Plane."""
    logger.info("Starting QJM Dashboard & Control Plane…")
    # Ensure default Switchyard config exists
    switchyard_config_manager.read_config()
    logger.info("Dashboard online")
    yield
    logger.info("Shutting down Dashboard…")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="QJM LLM Gateway Dashboard",
    description="Control plane and management dashboard for NVIDIA NeMo Switchyard router and Docker LLM backends.",
    version="3.0.0",
    lifespan=lifespan,
)

# --- Admin & Management Routers ---
app.include_router(containers.router)
app.include_router(system.router)
app.include_router(routing.router)
app.include_router(backends.router)
app.include_router(models.router)
app.include_router(gemini_proxy.router)

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
    return {"message": "QJM Dashboard API is running. Dashboard index.html not found."}


@app.get("/api/status")
async def get_system_status():
    switchyard_status = await telemetry_collector.get_switchyard_status()
    sys_stats = telemetry_collector.get_system_stats()
    return {
        "status": "online",
        "service": "QJM Dashboard (Control Plane)",
        "version": "3.0.0",
        "router": {
            "type": "NVIDIA NeMo Switchyard",
            "healthy": switchyard_status.healthy,
            "endpoint": "http://10.20.0.23:4000/v1",
            "active_models": switchyard_status.active_models,
        },
        "system": {
            "cpu_percent": sys_stats.cpu_percent,
            "memory_percent": sys_stats.memory_percent,
            "memory_used_gb": sys_stats.memory_used_gb,
            "memory_total_gb": sys_stats.memory_total_gb,
            "gpu": sys_stats.gpu.model_dump() if sys_stats.gpu else None,
        },
    }
