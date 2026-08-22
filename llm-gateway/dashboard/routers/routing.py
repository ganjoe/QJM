import time
import httpx
from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from services.switchyard_config import switchyard_config_manager

router = APIRouter(prefix="/api/routing", tags=["routing"])

SWITCHYARD_BASE_URL = "http://switchyard:4000"


class RoutePayload(BaseModel):
    key: str
    id: Optional[str] = None
    type: str = "passthrough"
    target: Optional[str] = None
    targets: Optional[list[str]] = None
    mode: Optional[str] = "capability"
    classifier_target: Optional[str] = None
    weak_target: Optional[str] = None
    strong_target: Optional[str] = None
    base_threshold: Optional[float] = 0.5
    threshold_step: Optional[float] = 0.1
    weights: Optional[dict[str, float]] = None


class TargetPayload(BaseModel):
    key: str
    id: str
    llm_client: str


class ClientPayload(BaseModel):
    key: str
    format: str = "openai_chat"
    base_url: str
    api_key_env: Optional[str] = None


class RawTomlPayload(BaseModel):
    content: str


class TestPromptPayload(BaseModel):
    route: str
    prompt: str = "Hi, reply with a short greeting."
    max_tokens: Optional[int] = 100


# --- Complete Config ---
@router.get("/full")
async def get_full_routing_config():
    """Returns the complete structured config of Switchyard."""
    config = switchyard_config_manager.read_config()
    routes = switchyard_config_manager.get_routes()
    targets = switchyard_config_manager.get_targets()
    clients = switchyard_config_manager.get_clients()
    raw = switchyard_config_manager.read_raw_toml()

    return {
        "routes": routes,
        "targets": targets,
        "clients": clients,
        "raw_toml": raw,
        "schema_version": config.get("schema_version", 1),
    }


# --- Legacy Compatibility Route ---
@router.get("/")
async def get_routing_table():
    routes = switchyard_config_manager.get_routes()
    targets = switchyard_config_manager.get_targets()
    clients = switchyard_config_manager.get_clients()

    formatted_routes = []
    for r in routes:
        desc = r.get("target")
        if r.get("type") == "llm_classifier":
            desc = f"Judge: {r.get('classifier_target')} -> {r.get('weak_target')}/{r.get('strong_target')}"
        elif r.get("type") == "fallback":
            desc = f"Chain: {' -> '.join(r.get('targets', []))}"
        elif r.get("type") in ("weighted", "load_balance"):
            desc = f"Weights: {r.get('weights')}"

        formatted_routes.append({
            "tier": r.get("id", r.get("key")),
            "key": r.get("key"),
            "target": desc,
            "api_base": r.get("type", "passthrough"),
            "type": r.get("type", "passthrough"),
            "supports_reasoning": "reasoning" in r.get("id", "").lower() or r.get("type") == "llm_classifier",
        })

    return {
        "routes": formatted_routes,
        "raw_routes": routes,
        "targets": targets,
        "clients": clients,
    }


# --- Route CRUD ---
@router.post("/route")
async def save_route(payload: RoutePayload):
    key = payload.key or payload.id or "custom_route"
    success = switchyard_config_manager.save_route(key, payload.model_dump())
    if not success:
        raise HTTPException(status_code=500, detail="Failed to save route.")
    return {"success": True, "key": key}


@router.delete("/route/{key}")
async def delete_route(key: str):
    success = switchyard_config_manager.delete_route(key)
    if not success:
        raise HTTPException(status_code=404, detail="Route not found or deletion failed.")
    return {"success": True, "key": key}


# --- Target CRUD ---
@router.post("/target")
async def save_target(payload: TargetPayload):
    success = switchyard_config_manager.save_target(payload.key, payload.model_dump())
    if not success:
        raise HTTPException(status_code=500, detail="Failed to save target.")
    return {"success": True, "key": payload.key}


@router.delete("/target/{key}")
async def delete_target(key: str):
    success = switchyard_config_manager.delete_target(key)
    if not success:
        raise HTTPException(status_code=404, detail="Target not found.")
    return {"success": True, "key": key}


# --- Client CRUD ---
@router.post("/client")
async def save_client(payload: ClientPayload):
    success = switchyard_config_manager.save_client(payload.key, payload.model_dump())
    if not success:
        raise HTTPException(status_code=500, detail="Failed to save client.")
    return {"success": True, "key": payload.key}


@router.delete("/client/{key}")
async def delete_client(key: str):
    success = switchyard_config_manager.delete_client(key)
    if not success:
        raise HTTPException(status_code=404, detail="Client not found.")
    return {"success": True, "key": key}


# --- Raw TOML Management ---
@router.get("/raw")
async def get_raw_toml():
    return {"content": switchyard_config_manager.read_raw_toml()}


@router.post("/raw")
async def save_raw_toml(payload: RawTomlPayload):
    success, msg = switchyard_config_manager.validate_and_write_raw_toml(payload.content)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg}


# --- Switchyard Hot Reload ---
@router.post("/reload")
async def reload_switchyard():
    res = await switchyard_config_manager.reload_switchyard()
    return res


# --- Interactive Route Tester / Playground ---
@router.post("/test")
async def test_route_prompt(payload: TestPromptPayload):
    """Sends a test chat completion to Switchyard and records latency and response."""
    start_time = time.time()
    url = f"{SWITCHYARD_BASE_URL}/v1/chat/completions"
    req_body = {
        "model": payload.route,
        "messages": [{"role": "user", "content": payload.prompt}],
        "max_tokens": payload.max_tokens or 500,
        "temperature": 0.3,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=req_body)
            duration_ms = round((time.time() - start_time) * 1000, 1)

            if resp.status_code == 200:
                data = resp.json()
                choice = data.get("choices", [{}])[0]
                content = choice.get("message", {}).get("content", "")
                finish_reason = choice.get("finish_reason", "unknown")
                routed_model = data.get("model", payload.route)
                usage = data.get("usage", {})

                # Flag if the response was truncated
                truncated = finish_reason == "length"

                return {
                    "success": True,
                    "status_code": resp.status_code,
                    "latency_ms": duration_ms,
                    "routed_model": routed_model,
                    "content": content,
                    "finish_reason": finish_reason,
                    "truncated": truncated,
                    "usage": usage,
                }
            else:
                return {
                    "success": False,
                    "status_code": resp.status_code,
                    "latency_ms": duration_ms,
                    "error": resp.text,
                }
    except Exception as e:
        duration_ms = round((time.time() - start_time) * 1000, 1)
        return {
            "success": False,
            "latency_ms": duration_ms,
            "error": str(e),
        }


# --- Switchyard Live Statistics ---
@router.get("/stats")
async def get_switchyard_stats():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            stats_resp = await client.get(f"{SWITCHYARD_BASE_URL}/v1/stats")
            stats_data = stats_resp.json() if stats_resp.status_code == 200 else {}

            models_resp = await client.get(f"{SWITCHYARD_BASE_URL}/v1/models")
            models_data = models_resp.json() if models_resp.status_code == 200 else {}

            return {
                "online": stats_resp.status_code == 200,
                "stats": stats_data,
                "active_models": models_data.get("data", []),
            }
    except Exception as e:
        return {
            "online": False,
            "error": str(e),
            "stats": {},
            "active_models": [],
        }


@router.post("/stats/reset")
async def reset_switchyard_stats():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{SWITCHYARD_BASE_URL}/v1/stats/reset")
            return {"success": resp.status_code == 200}
    except Exception as e:
        return {"success": False, "error": str(e)}
