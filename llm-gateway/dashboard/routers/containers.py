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
@router.get("/{container_name}/logs")
async def get_container_logs(container_name: str, tail: int = 50):
    """Returns the tail of the container logs."""
    try:
        import httpx
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")) as client:
            resp = await client.get(f"http://localhost/containers/{container_name}/logs?stdout=1&stderr=1&tail={tail}")
            if resp.status_code == 200:
                import re
                clean_logs = re.sub(r'[\x00-\x07]', '', resp.text)
                return {"success": True, "logs": clean_logs}
            return {"success": False, "error": f"Status {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
@router.post("/fix-perms")
async def fix_permissions():
    try:
        import httpx
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")) as client:
            # Create a busybox container to run chmod on the host's directory
            payload = {
                "Image": "llm-gw-dashboard:latest",
                "Cmd": ["sh", "-c", "chmod 600 /host/home/daniel/QJM/llm-gateway/dsh-config/.credentials.yaml"],
                "HostConfig": {
                    "Binds": ["/:/host"]
                }
            }
            create_resp = await client.post("http://localhost/containers/create", json=payload)
            if create_resp.status_code != 201:
                return {"success": False, "error": f"Create failed: {create_resp.text}"}
            container_id = create_resp.json()["Id"]
            start_resp = await client.post(f"http://localhost/containers/{container_id}/start")
            await client.post(f"http://localhost/containers/{container_id}/wait")
            await client.delete(f"http://localhost/containers/{container_id}")
            return {"success": True, "message": "Permissions fixed!"}
    except Exception as e:
        return {"success": False, "error": str(e)}
