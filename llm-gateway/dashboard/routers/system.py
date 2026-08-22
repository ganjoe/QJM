from fastapi import APIRouter
from services.telemetry import telemetry_collector
from models import SystemStats

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/stats", response_model=SystemStats)
async def get_system_stats():
    """Returns host CPU, RAM, and GPU VRAM telemetry."""
    return telemetry_collector.get_system_stats()
