"""
Admin API for endpoint registration and management.

Allows dynamic CRUD of LLM endpoints and querying of pool/health state
at runtime without restarting the router.
"""
from fastapi import APIRouter, HTTPException

from engine import registry, pool_manager
from engine.models import EndpointConfig

router = APIRouter(prefix="/api/endpoints", tags=["endpoints"])


@router.post("/")
async def register_endpoint(config: EndpointConfig):
    """
    Register a new LLM endpoint (managed or unmanaged).

    If an endpoint with the same ``endpoint_id`` already exists it will
    be replaced.
    """
    state = await registry.register(config)
    await pool_manager.sync_pools()
    return {
        "message": f"Endpoint '{config.endpoint_id}' registered",
        "endpoint_id": config.endpoint_id,
        "status": state.status.value,
    }


@router.get("/")
async def list_endpoints():
    """List all registered endpoints with their runtime state."""
    endpoints = await registry.get_all()
    return {"endpoints": endpoints}


@router.get("/{endpoint_id}")
async def get_endpoint(endpoint_id: str):
    """Get detailed state for a specific endpoint."""
    state = await registry.get_endpoint(endpoint_id)
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"Endpoint '{endpoint_id}' not found",
        )
    return {
        "endpoint_id": state.config.endpoint_id,
        "name": state.config.name,
        "type": state.config.type.value,
        "base_url": state.config.base_url,
        "api_schema": state.config.api_schema.value,
        "model_name": state.config.model_name,
        "status": state.status.value,
        "active_slots": state.active_slots,
        "max_concurrency": state.config.max_concurrency,
        "capabilities": state.config.capabilities,
        "priority": state.config.priority,
        "last_health_check": (
            state.last_health_check.isoformat() if state.last_health_check else None
        ),
        "last_latency_ms": state.last_latency_ms,
        "consecutive_failures": state.consecutive_failures,
    }


@router.delete("/{endpoint_id}")
async def unregister_endpoint(endpoint_id: str):
    """Remove an endpoint from the registry."""
    success = await registry.unregister(endpoint_id)
    if not success:
        raise HTTPException(
            status_code=404, detail=f"Endpoint '{endpoint_id}' not found",
        )
    return {"message": f"Endpoint '{endpoint_id}' removed", "success": True}


@router.post("/{endpoint_id}/restart")
async def restart_endpoint(endpoint_id: str):
    """Restart the docker container associated with a managed endpoint."""
    from services.docker_client import docker_manager
    state = await registry.get_endpoint(endpoint_id)
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"Endpoint '{endpoint_id}' not found",
        )
    
    if not state.config.docker_container:
        raise HTTPException(
            status_code=400, detail=f"Endpoint '{endpoint_id}' has no associated docker container",
        )
    
    success = await docker_manager.restart_container(state.config.docker_container)
    if not success:
        raise HTTPException(
            status_code=500, detail=f"Failed to restart docker container '{state.config.docker_container}'",
        )
    
    return {"message": f"Container '{state.config.docker_container}' restarted successfully", "success": True}


@router.get("/{endpoint_id}/health")
async def get_endpoint_health(endpoint_id: str):
    """Get detailed health/circuit-breaker state for a specific endpoint."""
    state = await registry.get_endpoint(endpoint_id)
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"Endpoint '{endpoint_id}' not found",
        )
    return {
        "endpoint_id": endpoint_id,
        "status": state.status.value,
        "last_health_check": (
            state.last_health_check.isoformat() if state.last_health_check else None
        ),
        "consecutive_failures": state.consecutive_failures,
        "last_latency_ms": state.last_latency_ms,
        "health_check_url": (
            state.config.health_check.url if state.config.health_check else None
        ),
    }
