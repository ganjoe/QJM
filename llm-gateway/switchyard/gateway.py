#!/usr/bin/env python3
import os
import sys
import json
import logging
import asyncio
import subprocess
import tomllib
from typing import Dict, Any, List, Optional
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [SwitchyardGateway] %(message)s")
logger = logging.getLogger("switchyard.gateway")

CONFIG_PATH = os.getenv("CONFIG_PATH", "/config/routes.toml")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "4000"))
INTERNAL_SWITCHYARD_PORT = 4001
INTERNAL_SWITCHYARD_URL = f"http://127.0.0.1:{INTERNAL_SWITCHYARD_PORT}"

app = FastAPI(title="Switchyard Unified Gateway", version="1.0.0")
switchyard_process: Optional[subprocess.Popen] = None
http_client: Optional[httpx.AsyncClient] = None


def load_routes_config() -> Dict[str, Any]:
    """Loads and parses the routes.toml file."""
    if not os.path.exists(CONFIG_PATH):
        logger.warning(f"Config file not found at {CONFIG_PATH}")
        return {}
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        logger.error(f"Failed to parse {CONFIG_PATH}: {e}")
        return {}


def resolve_embedding_target(requested_model: str, config: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """
    Resolves the target endpoint URL and backend model ID from routes.toml.
    Returns (target_base_url, actual_model_id).
    """
    routes = config.get("routes", {})
    targets = config.get("targets", {})
    llm_clients = config.get("llm_clients", {})
    endpoints = config.get("endpoints", {})

    target_name = None
    if requested_model in routes:
        route_info = routes[requested_model]
        target_name = route_info.get("target") or (route_info.get("targets", [None])[0])
    elif requested_model in targets:
        target_name = requested_model
    else:
        # Check if "embeddings" default route exists
        if "embeddings" in routes:
            target_name = routes["embeddings"].get("target")

    if not target_name or target_name not in targets:
        logger.warning(f"No target found for embedding model '{requested_model}', checking targets...")
        if "ollama_embed" in targets:
            target_name = "ollama_embed"
        else:
            return None, None

    target_info = targets[target_name]
    model_id = target_info.get("id", requested_model)
    client_name = target_info.get("llm_client") or target_info.get("endpoint")

    base_url = None
    if client_name and client_name in llm_clients:
        base_url = llm_clients[client_name].get("base_url")
    elif client_name and client_name in endpoints:
        base_url = endpoints[client_name].get("base_url")

    if not base_url:
        base_url = "http://host.docker.internal:11434/v1"

    return base_url, model_id


@app.on_event("startup")
async def startup_event():
    global switchyard_process, http_client
    http_client = httpx.AsyncClient(timeout=300.0)

    # Launch switchyard-server on internal port 4001
    cmd = [
        "switchyard-server",
        "--config", CONFIG_PATH,
        "--host", "127.0.0.1",
        "--port", str(INTERNAL_SWITCHYARD_PORT)
    ]
    logger.info(f"Starting internal switchyard-server on port {INTERNAL_SWITCHYARD_PORT}: {' '.join(cmd)}")
    try:
        switchyard_process = subprocess.Popen(cmd)
        logger.info(f"switchyard-server spawned with PID {switchyard_process.pid}")
    except Exception as e:
        logger.error(f"Failed to start switchyard-server: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    global switchyard_process, http_client
    if http_client:
        await http_client.aclose()
    if switchyard_process:
        logger.info("Terminating internal switchyard-server...")
        switchyard_process.terminate()
        try:
            switchyard_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            switchyard_process.kill()


@app.post("/v1/embeddings")
@app.post("/embeddings")
@app.post("/api/embeddings")
async def handle_embeddings(request: Request):
    """
    Handles /v1/embeddings by evaluating routes.toml and dispatching to the target embedding engine.
    """
    try:
        body = await request.json()
    except Exception:
        return Response(content=json.dumps({"error": "Invalid JSON body"}), status_code=400, media_type="application/json")

    requested_model = body.get("model", "embeddings")
    config = load_routes_config()
    base_url, target_model = resolve_embedding_target(requested_model, config)

    if not base_url:
        base_url = "http://host.docker.internal:11434/v1"
        target_model = "qwen3-embedding:8b"

    body["model"] = target_model

    # Normalize target URL
    clean_base = base_url.rstrip("/")
    target_endpoint = f"{clean_base}/embeddings" if clean_base.endswith("/v1") else f"{clean_base}/v1/embeddings"

    # Forward to target backend
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    headers["content-type"] = "application/json"

    logger.info(f"Routing embeddings: model='{requested_model}' -> '{target_model}' at {target_endpoint}")
    
    # Candidate endpoints for fallback (e.g. host.docker.internal vs container network ollama)
    fallback_endpoints = [target_endpoint]
    if "host.docker.internal:11434" in target_endpoint:
        fallback_endpoints.append(target_endpoint.replace("host.docker.internal:11434", "ollama:11434"))
    elif "ollama:11434" in target_endpoint:
        fallback_endpoints.append(target_endpoint.replace("ollama:11434", "host.docker.internal:11434"))

    last_res = None
    for ep in fallback_endpoints:
        try:
            resp = await http_client.post(ep, json=body, headers=headers)
            if resp.status_code == 200:
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json")
                )
            last_res = resp
        except Exception as e:
            logger.debug(f"Attempt failed at {ep}: {e}")
            continue

    if last_res:
        return Response(
            content=last_res.content,
            status_code=last_res.status_code,
            media_type=last_res.headers.get("content-type", "application/json")
        )

    return Response(
        content=json.dumps({
            "error": {
                "message": f"Could not connect to embedding target for model '{requested_model}' ({target_model}) at {target_endpoint}. Please ensure Ollama is running.",
                "type": "server_error",
                "param": None,
                "code": "backend_unreachable"
            }
        }),
        status_code=503,
        media_type="application/json"
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"])
async def proxy_to_switchyard(request: Request, path: str):
    """
    Transparently proxies all other requests (/v1/chat/completions, /v1/models, etc.)
    to the internal switchyard-server instance.
    """
    url = f"{INTERNAL_SWITCHYARD_URL}/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    body = await request.body()

    try:
        req = http_client.build_request(
            method=request.method,
            url=url,
            headers=headers,
            content=body
        )
        resp = await http_client.send(req, stream=True)

        return StreamingResponse(
            resp.aiter_raw(),
            status_code=resp.status_code,
            headers={k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")},
            media_type=resp.headers.get("content-type")
        )
    except Exception as e:
        logger.error(f"Error proxying to switchyard-server ({url}): {e}")
        return Response(
            content=json.dumps({"error": f"Internal switchyard-server error: {str(e)}"}),
            status_code=502,
            media_type="application/json"
        )


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, access_log=False)
