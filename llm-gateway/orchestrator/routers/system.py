from fastapi import APIRouter

from services.docker_client import docker_manager

router = APIRouter(prefix="/api/system", tags=["system"])

@router.get("/containers")
async def list_containers():
    """Returns a list of all running Docker containers on the host."""
    containers = await docker_manager.list_all_containers()
    return {"containers": containers}
