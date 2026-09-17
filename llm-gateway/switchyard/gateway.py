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
    Resolves the embedding endpoint URL and backend model ID from routes.toml.

    Es gibt genau EIN Embedding-Backend (CPU-Ollama). Ein GPU-Target existiert nicht mehr:
    die iGPU kann das 6,6-GB-Modell nicht halten und rechnet ohnehin auf der CPU, ein
    Wechsel des Backends würde also nur den warmgehaltenen Zustand zerstören.

    Kein Modell-Fallback: das Modell bleibt immer 'qwen3-embedding:8b', sonst würden
    Vektoren eines fremden Raums in denselben vector(4096)-Index geschrieben.
    """
    routes = config.get("routes", {})
    targets = config.get("targets", {})
    llm_clients = config.get("llm_clients", {})
    endpoints = config.get("endpoints", {})

    route_info = routes.get(requested_model)
    if not route_info and "embeddings" in routes:
        route_info = routes["embeddings"]

    target_names: list[str] = []
    if route_info:
        if "targets" in route_info and isinstance(route_info["targets"], list):
            target_names.extend(route_info["targets"])
        elif "target" in route_info:
            target_names.append(route_info["target"])
    elif requested_model in targets:
        target_names.append(requested_model)

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
        if base_url:
            return (base_url, m_id)

    return (EMBED_FALLBACK_BASE_URL, EMBED_MODEL_ID)


# -----------------------------------------------------------------------------
# Embedding-Scheduler: Priorisierung + keep_alive
# -----------------------------------------------------------------------------
# Ollama serialisiert Requests pro Modell-Instanz und kennt keine Prioritäten. Damit eine
# interaktive Suche nicht hinter großen Batch-Jobs wartet, laufen alle Embedding-Requests
# durch genau einen Worker; die Reihenfolge bestimmt die Prioritätsklasse des Aufrufers.
EMBED_FALLBACK_BASE_URL = os.getenv("EMBED_BASE_URL", "http://host.docker.internal:11434/v1")
EMBED_MODEL_ID = os.getenv("EMBED_MODEL_ID", "qwen3-embedding:8b")

# keep_alive=-1 => Modell wird niemals entladen. Der OpenAI-kompatible Endpoint ignoriert
# dieses Feld, deshalb geht der Gateway auf die native /api/embed-API.
EMBED_KEEP_ALIVE = os.getenv("EMBED_KEEP_ALIVE", "-1")
EMBED_BATCH_SIZE = max(1, int(os.getenv("EMBED_BATCH_SIZE", "8")))
EMBED_QUEUE_TIMEOUT = float(os.getenv("EMBED_QUEUE_TIMEOUT", "600"))
EMBED_MAX_RETRIES = max(0, int(os.getenv("EMBED_MAX_RETRIES", "2")))

# Höhere Zahl = höhere Priorität. Unbekannte/fehlende Klasse wird wie eine interaktive
# Anfrage behandelt, damit ein nicht angepasster Aufrufer nie in der YT-Klasse landet.
EMBED_PRIORITIES: Dict[str, int] = {
    "x_search": 30,   # interaktive Suche (X-Suche, Open-Brain-Suche, YT-Suche)
    "x_post": 20,     # X-Posts vektorisieren, Profil-/Dokument-Embeddings
    "yt": 10,         # YouTube-Chunks (Massenlast, geringster Vorrang)
}
EMBED_DEFAULT_PRIORITY_CLASS = "x_search"

embed_queues: Dict[int, asyncio.Queue] = {p: asyncio.Queue() for p in EMBED_PRIORITIES.values()}
embed_lock = asyncio.Lock()
embed_event = asyncio.Event()
embed_scheduler_task: Optional[asyncio.Task] = None
embed_metrics: Dict[str, Any] = {
    "items_enqueued": 0,
    "items_served": 0,
    "batches": 0,
    "last_batch_size": 0,
    "last_batch_duration_s": 0.0,
    "last_error": None,
    "by_class": {cls: 0 for cls in EMBED_PRIORITIES},
}


def embedding_priority(priority_class: Optional[str]) -> int:
    """Mappt die angeforderte Klasse auf einen Rang. Unbekanntes Verhalten ist absichtlich laut."""
    if not priority_class:
        return EMBED_PRIORITIES[EMBED_DEFAULT_PRIORITY_CLASS]
    if priority_class in EMBED_PRIORITIES:
        return EMBED_PRIORITIES[priority_class]
    logger.warning(
        f"Unbekannte Embedding-Prioritätsklasse '{priority_class}', "
        f"behandle wie '{EMBED_DEFAULT_PRIORITY_CLASS}'. Erlaubt: {sorted(EMBED_PRIORITIES)}"
    )
    return EMBED_PRIORITIES[EMBED_DEFAULT_PRIORITY_CLASS]


def enqueue_embedding(
    inputs: list[str],
    base_url: str,
    backend_model: str,
    priority: int,
    priority_class: str,
) -> asyncio.Future:
    """
    Zerlegt eine Anfrage in Chunks von höchstens EMBED_BATCH_SIZE Inputs und stellt sie in
    die Prioritäts-Lane. Der Aufrufer bekommt genau ein Future, das erst erfüllt ist, wenn
    ALLE Chunks zurück sind -- und zwar in der Summe genau len(inputs) Vektoren in Reihenfolge.

    Wichtig: jeder Chunk braucht ein eigenes Future. Ein gemeinsames Future wäre nach dem
    ersten Chunk bereits erfüllt und die restlichen Vektoren gingen verloren.
    """
    loop = asyncio.get_running_loop()
    total = len(inputs)
    owner: asyncio.Future = loop.create_future()
    collected: list[Optional[list[list[float]]]] = [None] * total
    remaining = [total]
    deadline = [loop.time() + EMBED_QUEUE_TIMEOUT]

    def on_chunk_done(start: int, fut: "asyncio.Future") -> None:
        if owner.done():
            return
        if fut.cancelled():
            owner.cancel()
            return
        err = fut.exception()
        if err is not None:
            owner.set_exception(err)
            return
        chunk_vectors = fut.result()
        collected[start:start + len(chunk_vectors)] = chunk_vectors
        remaining[0] -= len(chunk_vectors)
        if remaining[0] <= 0:
            # Reihenfolge bleibt erhalten, weil jeder Chunk an seiner Startposition einsetzt.
            owner.set_result([v for v in collected if v is not None])
        else:
            arm_timeout()

    def arm_timeout() -> None:
        """Timeout erst nach dem letzten Chunk entfernen, damit lange Anfragen nicht
        mitten in der Verarbeitung ablaufen."""
        if owner.done():
            return

        def fire() -> None:
            if not owner.done():
                owner.set_exception(
                    RuntimeError(
                        f"Embedding-Anfrage nach {EMBED_QUEUE_TIMEOUT:.0f}s nicht vollständig bedient"
                    )
                )
        handle = loop.call_later(max(0.0, deadline[0] - loop.time()), fire)
        owner.add_done_callback(lambda _f: handle.cancel())

    for start in range(0, total, EMBED_BATCH_SIZE):
        chunk = inputs[start:start + EMBED_BATCH_SIZE]
        chunk_fut: asyncio.Future = loop.create_future()
        chunk_fut.add_done_callback(lambda f, s=start: on_chunk_done(s, f))
        embed_queues[priority].put_nowait(
            {"inputs": chunk, "base_url": base_url, "model": backend_model, "future": chunk_fut}
        )
    arm_timeout()

    embed_metrics["items_enqueued"] += total
    if priority_class in embed_metrics["by_class"]:
        embed_metrics["by_class"][priority_class] += total
    embed_event.set()
    return owner


def _collect_batch(lanes: list[asyncio.Queue]) -> list[dict]:
    """Entnimmt nach Priorität (hoch zuerst) und bündelt zu einem Backend-Call.

    Jede Lane-Anfrage ist bereits auf EMBED_BATCH_SIZE Inputs begrenzt, deshalb kann eine
    Anfrage hier nicht angeschnitten werden. Die Budget-Prüfung bleibt als Sicherheitsnetz:
    sie verhindert nur, dass ein Batch das Limit überschreitet.
    """
    batch: list[dict] = []
    budget = EMBED_BATCH_SIZE
    while budget > 0:
        took_any = False
        for lane in lanes:
            if budget <= 0:
                break
            if lane.empty():
                continue
            entry = lane.get_nowait()
            n = len(entry["inputs"])
            if n > budget:
                # Sollte durch die Chunking-Logik nicht vorkommen; nicht anschneiden.
                lane.put_nowait(entry)
                continue
            batch.append(entry)
            budget -= n
            took_any = True
        if not took_any:
            break
    return batch


def _parse_keep_alive():
    try:
        return int(EMBED_KEEP_ALIVE)
    except ValueError:
        return EMBED_KEEP_ALIVE


def _native_embed_endpoint(base_url: str) -> str:
    """OpenAI-Basis-URL -> native Ollama-Embed-Route (nur die respektiert keep_alive)."""
    clean = base_url.rstrip("/")
    if clean.endswith("/v1"):
        clean = clean[: -len("/v1")]
    return f"{clean}/api/embed"


async def _post_embeddings(endpoint: str, payload: dict) -> dict:
    last_error: Optional[Exception] = None
    for attempt in range(EMBED_MAX_RETRIES + 1):
        try:
            resp = await http_client.post(endpoint, json=payload)
            if resp.status_code == 200:
                return resp.json()
            last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        except Exception as e:
            last_error = e
        if attempt < EMBED_MAX_RETRIES:
            await asyncio.sleep(0.5 * (attempt + 1))
    raise last_error if last_error else RuntimeError("Embedding-Backend nicht erreichbar")


async def _embed_worker_loop() -> None:
    """Ein einziger Worker: entnimmt nach Priorität, bündelt zu einem Backend-Call."""
    while True:
        try:
            await embed_event.wait()
            async with embed_lock:
                lanes = [embed_queues[p] for p in sorted(embed_queues, reverse=True)]
                if all(q.empty() for q in lanes):
                    embed_event.clear()
                    batch = []
                else:
                    batch = _collect_batch(lanes)

            if not batch:
                continue

            first = batch[0]
            payload = {
                "model": first["model"],
                "input": [item for entry in batch for item in entry["inputs"]],
                "keep_alive": _parse_keep_alive(),
            }
            endpoint = _native_embed_endpoint(first["base_url"])
            started = asyncio.get_running_loop().time()
            try:
                result = await _post_embeddings(endpoint, payload)
                vectors = result.get("embeddings") or []
                if len(vectors) != len(payload["input"]):
                    raise RuntimeError(
                        f"Backend lieferte {len(vectors)} Vektoren für {len(payload['input'])} Inputs"
                    )
                offset = 0
                for entry in batch:
                    n = len(entry["inputs"])
                    if not entry["future"].done():
                        entry["future"].set_result(vectors[offset:offset + n])
                    offset += n
                duration = asyncio.get_running_loop().time() - started
                embed_metrics["batches"] += 1
                embed_metrics["items_served"] += len(payload["input"])
                embed_metrics["last_batch_size"] = len(payload["input"])
                embed_metrics["last_batch_duration_s"] = round(duration, 2)
                logger.info(
                    f"Embeddings: {len(payload['input'])} Inputs aus {len(batch)} Anfrage(n) "
                    f"in {duration:.2f}s (Backend {endpoint})"
                )
            except Exception as e:
                embed_metrics["last_error"] = str(e)
                logger.error(f"Embedding-Batch fehlgeschlagen ({endpoint}): {e}")
                for entry in batch:
                    if not entry["future"].done():
                        entry["future"].set_exception(e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Nie sterben: ein Fehler im Loop darf den Embedding-Pfad nicht dauerhaft blockieren.
            logger.error(f"Embedding-Worker-Loop Fehler: {e}")
            await asyncio.sleep(0.5)


@app.on_event("startup")
async def startup_event():
    global switchyard_process, http_client, embed_scheduler_task
    # Großzügiger Timeout: ein Batch auf der CPU kann deutlich länger als 300 s dauern.
    http_client = httpx.AsyncClient(timeout=float(os.getenv("SWITCHYARD_TIMEOUT", "900")))
    embed_scheduler_task = asyncio.create_task(_embed_worker_loop())
    logger.info(
        f"Embedding-Scheduler gestartet (Prioritäten "
        f"{sorted(EMBED_PRIORITIES, key=EMBED_PRIORITIES.get, reverse=True)}, "
        f"Batch {EMBED_BATCH_SIZE}, keep_alive {EMBED_KEEP_ALIVE})"
    )

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
    global switchyard_process, http_client, embed_scheduler_task
    if embed_scheduler_task:
        embed_scheduler_task.cancel()
        try:
            await embed_scheduler_task
        except (asyncio.CancelledError, Exception):
            pass
        embed_scheduler_task = None
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
    Nimmt Embedding-Anfragen entgegen und übergibt sie an den Prioritäts-Scheduler.

    Prioritätsklasse über das optionale Body-Feld "priority": 'x_search' > 'x_post' > 'yt'.
    Fehlt die Angabe, gilt die Anfrage als interaktiv (x_search) -- ein nicht angepasster
    Aufrufer soll nie in der langsamsten Klasse landen.
    """
    try:
        body = await request.json()
    except Exception:
        return Response(content=json.dumps({"error": "Invalid JSON body"}), status_code=400, media_type="application/json")

    requested_model = body.get("model", "embeddings")
    priority_class = body.get("priority")
    priority = embedding_priority(priority_class)

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        inputs = [raw_input]
    elif isinstance(raw_input, list):
        inputs = [str(x) for x in raw_input]
    else:
        return Response(
            content=json.dumps({"error": "Field 'input' must be a string or an array of strings"}),
            status_code=400,
            media_type="application/json",
        )
    if not inputs:
        return Response(
            content=json.dumps({"error": "Field 'input' must not be empty"}),
            status_code=400,
            media_type="application/json",
        )

    config = load_routes_config()
    base_url, backend_model = resolve_embedding_target(requested_model, config)

    fut = enqueue_embedding(inputs, base_url, backend_model, priority, priority_class or EMBED_DEFAULT_PRIORITY_CLASS)

    try:
        # Timeout wird bereits im Owner-Future des Schedulers durchgesetzt; hier nur warten.
        vectors = await fut
    except asyncio.TimeoutError:
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"Embedding-Anfrage nach {EMBED_QUEUE_TIMEOUT:.0f}s nicht bedient (Klasse '{priority_class or EMBED_DEFAULT_PRIORITY_CLASS}').",
                    "type": "server_error",
                    "code": "embedding_timeout",
                }
            }),
            status_code=504,
            media_type="application/json",
        )
    except Exception as e:
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"Embedding-Backend '{base_url}' nicht erreichbar: {e}",
                    "type": "server_error",
                    "code": "backend_unreachable",
                }
            }),
            status_code=503,
            media_type="application/json",
        )

    payload = {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": vec}
            for i, vec in enumerate(vectors)
        ],
        "model": backend_model,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }
    return Response(content=json.dumps(payload), media_type="application/json")


@app.get("/embeddings/status")
async def embeddings_status():
    """Transparenz über Queue-Tiefe, Prioritätsklassen und letzte Batch-Laufzeit."""
    return {
        "priorities": EMBED_PRIORITIES,
        "default_class": EMBED_DEFAULT_PRIORITY_CLASS,
        "keep_alive": _parse_keep_alive(),
        "batch_size": EMBED_BATCH_SIZE,
        "queue_depth": {
            cls: embed_queues[rank].qsize() for cls, rank in EMBED_PRIORITIES.items()
        },
        "metrics": embed_metrics,
    }


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
