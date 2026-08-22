import os
import glob
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from services.docker_client import docker_manager
from services.switchyard_config import switchyard_config_manager

router = APIRouter(prefix="/api/models", tags=["models"])

DEFAULT_MODELS = [
    {
        "id": "bartowski/Qwen2.5-Coder-32B-Instruct-GGUF:Q4_K_M",
        "name": "Qwen2.5 Coder 32B (GGUF Q4)",
        "quantization": "GGUF Q4_K_M",
        "vram": "19.0 GiB",
        "backend": "vLLM / LM Studio",
        "tokenizer": "Qwen/Qwen2.5-Coder-32B-Instruct",
        "type": "Code / General",
    },
    {
        "id": "Qwen/Qwen3-30B-A3B-Instruct-AWQ",
        "name": "Qwen3 30B MoE (AWQ)",
        "quantization": "AWQ 4-bit",
        "vram": "16.5 GiB",
        "backend": "vLLM",
        "tokenizer": "Qwen/Qwen3-30B-A3B-Instruct",
        "type": "Reasoning / MoE",
    },
    {
        "id": "gemini-2.5-flash",
        "name": "Gemini 2.5 Flash (Cloud)",
        "quantization": "Cloud",
        "vram": "0 GiB",
        "backend": "Google Gemini",
        "type": "Fast / Cloud",
    },
    {
        "id": "gemini-2.5-pro",
        "name": "Gemini 2.5 Pro (Cloud)",
        "quantization": "Cloud",
        "vram": "0 GiB",
        "backend": "Google Gemini",
        "type": "Reasoning / Cloud",
    },
]


def scan_lmstudio_models() -> list:
    """Scans the local LM Studio models volume for all downloaded GGUF models."""
    models_path = os.getenv("LMSTUDIO_MODELS_PATH", "/lmstudio-models")
    if not os.path.exists(models_path):
        models_path = os.getenv("LM_STUDIO_MODELS_DIR", "/home/daniel/.lmstudio/models")

    if not os.path.exists(models_path):
        return []

    discovered = []
    seen_ids = set()

    for root, dirs, files in os.walk(models_path):
        for file in files:
            if file.lower().endswith(".gguf"):
                rel_path = os.path.relpath(os.path.join(root, file), models_path)
                parts = rel_path.split(os.sep)
                if len(parts) >= 2:
                    repo_id = f"{parts[0]}/{parts[1]}"
                else:
                    repo_id = parts[0]

                if repo_id not in seen_ids:
                    seen_ids.add(repo_id)
                    file_size_gb = round(os.path.getsize(os.path.join(root, file)) / (1024 ** 3), 1)
                    discovered.append({
                        "id": repo_id,
                        "name": f"{parts[1] if len(parts) >= 2 else parts[0]}",
                        "quantization": "GGUF",
                        "vram": f"~{file_size_gb} GiB",
                        "backend": "LM Studio (Docker)",
                        "filename": file,
                        "type": "Local GGUF",
                    })

    return discovered


class SwitchModelRequest(BaseModel):
    model_id: str
    target_backend: str = "lmstudio"  # "lmstudio" or "vllm"
    tokenizer: str = None
    hf_config_path: str = None


@router.get("/available")
async def get_available_models():
    local_lm_models = scan_lmstudio_models()
    all_models = list(DEFAULT_MODELS)

    existing_ids = {m["id"] for m in all_models}
    for lm in local_lm_models:
        if lm["id"] not in existing_ids:
            all_models.append(lm)

    return {
        "models": all_models,
        "lmstudio_models": local_lm_models,
    }


@router.get("/active")
async def get_active_model():
    import httpx
    vllm_container = os.getenv("DOCKER_VLLM_CONTAINER", "llm-gw-vllm")
    lmstudio_container = os.getenv("DOCKER_LMSTUDIO_CONTAINER", "llm-gw-lmstudio")
    switchyard_container = os.getenv("DOCKER_SWITCHYARD_CONTAINER", "llm-gw-switchyard")

    vllm_status = await docker_manager.get_container_status(vllm_container)
    lmstudio_status = await docker_manager.get_container_status(lmstudio_container)
    switchyard_status = await docker_manager.get_container_status(switchyard_container)
    current_model = os.getenv("VLLM_MODEL", "bartowski/Qwen2.5-Coder-32B-Instruct-GGUF:Q4_K_M")

    lmstudio_active_models = []
    if lmstudio_status.get("status") == "running":
        try:
            lmstudio_url = os.getenv("LMSTUDIO_URL", "http://host.docker.internal:1234")
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{lmstudio_url}/v1/models")
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    lmstudio_active_models = [
                        m.get("id") for m in data if isinstance(m, dict) and m.get("id")
                    ]
        except Exception:
            pass

    return {
        "vllm_container": vllm_status,
        "lmstudio_container": lmstudio_status,
        "switchyard_container": switchyard_status,
        "active_model": current_model,
        "lmstudio_active_models": lmstudio_active_models,
    }


@router.post("/switch")
async def switch_model(req: SwitchModelRequest):
    vllm_container = os.getenv("DOCKER_VLLM_CONTAINER", "llm-gw-vllm")
    lmstudio_container = os.getenv("DOCKER_LMSTUDIO_CONTAINER", "llm-gw-lmstudio")

    if req.target_backend == "lmstudio":
        # 1. Stop vLLM if running to free VRAM
        await docker_manager.stop_container(vllm_container)

        # 2. Start LM Studio if stopped
        await docker_manager.start_container(lmstudio_container)

        # 3. Update Switchyard route mapping
        switchyard_config_manager.update_route_target("local", "local_model")
        return {
            "message": f"LM Studio aktiviert für {req.model_id}.",
            "status": "active",
        }

    # If Target is vLLM
    await docker_manager.stop_container(lmstudio_container)
    os.environ["VLLM_MODEL"] = req.model_id
    success = await docker_manager.restart_container(vllm_container)
    if not success:
        success = await docker_manager.start_container(vllm_container)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to restart/start vLLM container")

    return {
        "message": f"vLLM lade {req.model_id}...",
        "status": "restarting",
    }


class EmbeddingsModeRequest(BaseModel):
    mode: str  # "cpu" or "gpu"


@router.get("/embeddings/mode")
async def get_embeddings_mode():
    import httpx
    url = os.getenv("OLLAMA_ROUTER_URL", "http://ollama:11434")
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{url}/_router/mode")
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return {"mode": "unknown"}


@router.post("/embeddings/mode")
async def set_embeddings_mode(req: EmbeddingsModeRequest):
    import httpx
    if req.mode not in ["cpu", "gpu"]:
        raise HTTPException(status_code=400, detail="Mode must be 'cpu' or 'gpu'")

    url = os.getenv("OLLAMA_ROUTER_URL", "http://ollama:11434")
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(f"{url}/_router/mode", json={"mode": req.mode})
            if resp.status_code == 200:
                return resp.json()
            else:
                raise HTTPException(status_code=resp.status_code, detail="Failed to switch mode")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Router offline or error: {str(e)}")
