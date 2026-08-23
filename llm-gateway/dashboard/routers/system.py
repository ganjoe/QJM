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
        "gpu_name": gpu.name if gpu else "N/A",
        "gpu_util": gpu.gpu_utilization_percent if gpu else None,
        "vram_used_mb": gpu.vram_used_mb if gpu else None,
        "vram_total_mb": gpu.vram_total_mb if gpu else None,
        "vram_percent": gpu.vram_percent if gpu else None,
        "gpu_temp": gpu.temperature_c if gpu else None,
        "gpu_temp_hotspot": gpu.temperature_hotspot_c if gpu else None,
        "gpu_temp_mem": gpu.temperature_mem_c if gpu else None,
        "gpu_power": gpu.power_watts if gpu else None,
        "gpu_power_cap": gpu.power_cap_watts if gpu else None,
        "gpu_fan_rpm": gpu.fan_rpm if gpu else None,
        "gpu_fan_percent": gpu.fan_percent if gpu else None,
        "gpu_clock_mhz": gpu.clock_mhz if gpu else None,
    }
