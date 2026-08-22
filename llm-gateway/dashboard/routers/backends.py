from fastapi import APIRouter
from services.telemetry import telemetry_collector

router = APIRouter(prefix="/api/backends", tags=["backends"])

@router.get("/")
async def get_backends_status():
    backends = await telemetry_collector.get_all_backends()
    return {"backends": backends}
