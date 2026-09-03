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


def resolve_embedding_targets(requested_model: str, config: Dict[str, Any]) -> list[tuple[str, str]]:
    """
    Resolves candidate target endpoint URLs and backend model IDs from routes.toml in fallback order.
    Returns list of (target_base_url, actual_model_id).
    """
    routes = config.get("routes", {})
    targets = config.get("targets", {})
    llm_clients = config.get("llm_clients", {})
    endpoints = config.get("endpoints", {})

    target_names: list[str] = []
    route_info = routes.get(requested_model)
    if not route_info and "embeddings" in routes:
        route_info = routes["embeddings"]

    if route_info:
        if "targets" in route_info and isinstance(route_info["targets"], list):
            target_names.extend(route_info["targets"])
        elif "target" in route_info:
            target_names.append(route_info["target"])
    elif requested_model in targets:
        target_names.append(requested_model)

    # Always ensure both GPU and CPU embedding targets are in fallback chain
    for fallback in ["ollama_embed_gpu", "ollama_embed_cpu", "ollama_embed"]:
        if fallback in targets and fallback not in target_names:
            target_names.append(fallback)

    resolved: list[tuple[str, str]] = []
    for t_name in target_names:
        if t_name not in targets:
            continue
        t_info = targets[t_name]
        m_id = t_info.get("id", requested_model or "qwen3-embedding:8b")
        c_name = t_info.get("llm_client") or t_info.get("endpoint")
        base_url = None
        if c_name and c_name in llm_clients:
            base_url = llm_clients[c_name].get("base_url")
        elif c_name and c_name in endpoints:
            base_url = endpoints[c_name].get("base_url")
        if base_url and (base_url, m_id) not in resolved:
            resolved.append((base_url, m_id))

    if not resolved:
        resolved.append(("http://host.docker.internal:11435/v1", "qwen3-embedding:8b"))
        resolved.append(("http://host.docker.internal:11434/v1", "qwen3-embedding:8b"))

    return resolved


def resolve_embedding_target(requested_model: str, config: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Backward compatibility wrapper returning first resolved target."""
    targets = resolve_embedding_targets(requested_model, config)
    return targets[0] if targets else (None, None)


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
    Handles /v1/embeddings by evaluating routes.toml and dispatching to whichever
    embedding engine (GPU or CPU) is currently started.
    """
    try:
        body = await request.json()
    except Exception:
        return Response(content=json.dumps({"error": "Invalid JSON body"}), status_code=400, media_type="application/json")

    requested_model = body.get("model", "embeddings")
    config = load_routes_config()
    target_candidates = resolve_embedding_targets(requested_model, config)

    # Active-Active Load Balancing:
    # We shuffle the fallback order so that if both GPU and CPU are running,
    # incoming concurrent requests will be statistically distributed across both.
    import random
    random.shuffle(target_candidates)

    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    headers["content-type"] = "application/json"

    last_res = None
    attempted_endpoints: list[str] = []

    # Configurable connect timeout: stopped containers fail in <5ms on Linux, small timeout protects against network stalls
    connect_timeout = float(os.getenv("SWITCHYARD_CONNECT_TIMEOUT", "1.0"))
    read_timeout = float(os.getenv("SWITCHYARD_TIMEOUT", "300.0"))

    for base_url, target_model in target_candidates:
        clean_base = base_url.rstrip("/")
        target_endpoint = f"{clean_base}/embeddings" if clean_base.endswith("/v1") else f"{clean_base}/v1/embeddings"

        req_body = dict(body)
        req_body["model"] = target_model
        attempted_endpoints.append(target_endpoint)

        try:
            resp = await http_client.post(
                target_endpoint,
                json=req_body,
                headers=headers,
                timeout=httpx.Timeout(read_timeout, connect=connect_timeout)
            )
            if resp.status_code == 200:
                logger.info(f"Embeddings served by {target_endpoint} using '{target_model}'")
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json")
                )
            last_res = resp
        except Exception as e:
            logger.debug(f"Target unreachable at {target_endpoint}: {e}")
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
                "message": f"Kein aktiver Embedding-Container erreichbar für '{requested_model}'. Bitte starte 'llm-gw-ollama-gpu' oder 'llm-gw-ollama-cpu' im QJM Control Plane.",
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
