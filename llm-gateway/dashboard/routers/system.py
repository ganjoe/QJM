from fastapi import APIRouter
from services.telemetry import telemetry_collector

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/stats")
async def get_system_stats():
    """Returns host CPU, RAM, and GPU VRAM telemetry."""
    return telemetry_collector.get_system_stats()


@router.get("/metrics")
async def get_system_metrics():
    """Returns a flat dict of metrics for the frontend header pills and hardware tab."""
    stats = telemetry_collector.get_system_stats()
    gpu = stats.gpu

    return {
        "cpu_percent": stats.cpu_percent,
        "cpu_count": stats.cpu_cores,
        "ram_percent": stats.memory_percent,
        "ram_used_gb": stats.memory_used_gb,
        "ram_total_gb": stats.memory_total_gb,
        "vram_used_mb": gpu.vram_used_mb if gpu else None,
        "vram_total_mb": gpu.vram_total_mb if gpu else None,
        "vram_percent": gpu.vram_percent if gpu else None,
        "gpu_util": gpu.gpu_utilization_percent if gpu else None,
        "gpu_name": gpu.name if gpu else "N/A",
    }
