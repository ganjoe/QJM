from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel
from services.switchyard_config import switchyard_config_manager

router = APIRouter(prefix="/api/routing", tags=["routing"])


class UpdateRouteRequest(BaseModel):
    tier: str
    target_model: str
    api_base: Optional[str] = None


@router.get("/")
async def get_routing_table():
    routes = switchyard_config_manager.get_routes()
    targets = switchyard_config_manager.get_targets()
    clients = switchyard_config_manager.get_clients()

    # Format routes for frontend compatibility
    formatted_routes = []
    for r in routes:
        formatted_routes.append({
            "tier": r.get("id", r.get("key")),
            "target": r.get("target") or f"{r.get('type')}: {r.get('classifier_target')} -> {r.get('weak_target')}/{r.get('strong_target')}",
            "api_base": r.get("type", "passthrough"),
            "supports_reasoning": "reasoning" in r.get("id", "").lower() or r.get("type") == "llm_classifier",
        })

    return {
        "routes": formatted_routes,
        "raw_routes": routes,
        "targets": targets,
        "clients": clients,
    }


@router.put("/")
async def update_route(req: UpdateRouteRequest):
    success = switchyard_config_manager.update_route_target(req.tier, req.target_model)
    return {"success": success, "tier": req.tier, "new_target": req.target_model}


@router.post("/")
async def add_route(req: UpdateRouteRequest):
    success = switchyard_config_manager.update_route_target(req.tier, req.target_model)
    return {"success": success, "tier": req.tier, "target_model": req.target_model}


@router.delete("/{tier}")
async def delete_route(tier: str):
    success = switchyard_config_manager.delete_route(tier)
    return {"success": success, "tier": tier}
