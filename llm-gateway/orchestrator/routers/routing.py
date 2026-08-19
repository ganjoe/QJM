from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel
from services.litellm_config import litellm_config_manager

router = APIRouter(prefix="/api/routing", tags=["routing"])

class UpdateRouteRequest(BaseModel):
    tier: str
    target_model: str
    api_base: Optional[str] = None

@router.get("/")
async def get_routing_table():
    config = litellm_config_manager.read_config()
    routes = []
    for item in config.get("model_list", []):
        routes.append({
            "tier": item.get("model_name"),
            "target": item.get("litellm_params", {}).get("model"),
            "api_base": item.get("litellm_params", {}).get("api_base", "Cloud"),
            "supports_reasoning": item.get("litellm_params", {}).get("supports_reasoning", False),
        })
    return {"routes": routes}

@router.put("/")
async def update_route(req: UpdateRouteRequest):
    success = litellm_config_manager.update_model_target(req.tier, req.target_model, req.api_base)
    return {"success": success, "tier": req.tier, "new_target": req.target_model, "api_base": req.api_base}

@router.post("/")
async def add_route(req: UpdateRouteRequest):
    success = litellm_config_manager.add_route(req.tier, req.target_model, req.api_base)
    return {"success": success, "tier": req.tier, "target_model": req.target_model, "api_base": req.api_base}

@router.delete("/{tier}")
async def delete_route(tier: str):
    success = litellm_config_manager.delete_route(tier)
    return {"success": success, "tier": tier}
