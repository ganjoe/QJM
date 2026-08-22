from fastapi import APIRouter, HTTPException
from services.docker_client import docker_manager
from models import ContainerInfo, ContainerActionResponse

router = APIRouter(prefix="/api/containers", tags=["containers"])


@router.get("/", response_model=list[ContainerInfo])
async def list_containers():
    """Returns runtime status and memory footprint for all LLM Gateway containers."""
    return await docker_manager.list_llm_containers()


@router.post("/{container_name}/start", response_model=ContainerActionResponse)
async def start_container(container_name: str):
    """Starts a container (e.g. boot LM Studio, vLLM or Ollama)."""
    success, msg = await docker_manager.start_container(container_name)
    if not success:
        raise HTTPException(status_code=500, detail=msg)
    return ContainerActionResponse(
        success=True,
        container_name=container_name,
        message=msg,
    )


@router.post("/{container_name}/stop", response_model=ContainerActionResponse)
async def stop_container(container_name: str):
    """Stops a container to release GPU VRAM and CPU resources."""
    success, msg = await docker_manager.stop_container(container_name)
    if not success:
        raise HTTPException(status_code=500, detail=msg)
    return ContainerActionResponse(
        success=True,
        container_name=container_name,
        message=msg,
    )


@router.post("/{container_name}/restart", response_model=ContainerActionResponse)
async def restart_container(container_name: str):
    """Restarts a container."""
    success, msg = await docker_manager.restart_container(container_name)
    if not success:
        raise HTTPException(status_code=500, detail=msg)
    return ContainerActionResponse(
        success=True,
        container_name=container_name,
        message=msg,
    )
