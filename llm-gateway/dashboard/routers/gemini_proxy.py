import os
import json
import logging
import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

logger = logging.getLogger("dashboard.gemini_proxy")
router = APIRouter(prefix="/api/gemini/v1", tags=["Gemini Proxy"])

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

# Unsupported fields by Google's direct OpenAI REST parser
STRIP_FIELDS = {"store", "metadata", "service_tier", "modalities"}

# Exact active Google AI Studio model IDs (as of Aug 2026)
MODEL_MAP = {
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview": "gemini-3.1-pro-preview",
    "gemini-3-pro": "gemini-3.1-pro-preview",
    "pro": "gemini-3.1-pro-preview",
    "reasoning": "gemini-3.1-pro-preview",
    "gemini-3.6-flash": "gemini-3.6-flash",
    "gemini-3-flash": "gemini-3.6-flash",
    "fast": "gemini-3.6-flash",
    "flash": "gemini-3.6-flash",
    "gemini-3.5-flash": "gemini-3.5-flash",
    "gemini-3.5-flash-lite": "gemini-3.5-flash-lite",
    "lite": "gemini-3.5-flash-lite",
}


@router.get("/models")
async def get_gemini_models():
    """Returns available Gemini models in OpenAI format."""
    return {
        "object": "list",
        "data": [
            {"id": "gemini-3.1-pro-preview", "object": "model", "owned_by": "google"},
            {"id": "gemini-3.1-pro", "object": "model", "owned_by": "google"},
            {"id": "gemini-3.6-flash", "object": "model", "owned_by": "google"},
            {"id": "gemini-3.5-flash", "object": "model", "owned_by": "google"},
            {"id": "gemini-3.5-flash-lite", "object": "model", "owned_by": "google"},
        ],
    }


@router.post("/chat/completions")
async def gemini_chat_proxy(request: Request):
    """
    Transparent proxy to Google AI Studio OpenAI endpoint.
    1. Sanitizes OpenAI client fields ('store', 'metadata', etc.).
    2. Maps forward-looking model IDs to verified Google AI Studio models.
    3. Normalizes streaming chunks so that finish_reason is always sent before [DONE].
    """
    api_key = GEMINI_API_KEY or os.getenv("GEMINI_API_KEY", "")
    
    # Try extracting API key from client Authorization header if present
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:].strip() not in ("switchyard", "none", ""):
        api_key = auth_header[7:].strip()

    try:
        body = await request.json()
    except Exception:
        return Response(content='{"error": "Invalid JSON body"}', status_code=400, media_type="application/json")

    # 1. Remove unsupported fields that cause 400 'Unknown name' errors on Google's API
    for field in STRIP_FIELDS:
        body.pop(field, None)

    # 2. Map model aliases to exact Google AI Studio model names
    model_req = body.get("model", "")
    if model_req in MODEL_MAP:
        body["model"] = MODEL_MAP[model_req]

    is_streaming = body.get("stream", False)
    api_key = api_key.strip()
    
    if not api_key:
        return Response(
            content='{"error": "GEMINI_API_KEY ist leer oder nicht im Container gesetzt. Bitte überprüfe die .env Datei und erstelle den Container mit docker-compose up -d neu."}',
            status_code=401,
            media_type="application/json"
        )

    target_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    client = httpx.AsyncClient(timeout=120.0)

    if is_streaming:
        async def sse_stream_generator():
            saw_finish_reason = False
            last_id = "chatcmpl-gemini"
            try:
                async with client.stream("POST", GEMINI_BASE_URL, headers=target_headers, json=body) as resp:
                    if resp.status_code != 200:
                        err_bytes = await resp.aread()
                        logger.error(f"Gemini API error {resp.status_code}: {err_bytes.decode('utf-8', 'ignore')}")
                        err_msg = f"Google API Error ({resp.status_code}): {err_bytes.decode('utf-8', 'ignore')}"
                        err_chunk = {
                            "id": last_id,
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {"content": err_msg}, "finish_reason": "stop"}],
                        }
                        yield f"data: {json.dumps(err_chunk)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if not line:
                            yield b"\n"
                            continue

                        if line.startswith("data: "):
                            raw_data = line[6:].strip()
                            if raw_data == "[DONE]":
                                if not saw_finish_reason:
                                    fix_chunk = {
                                        "id": last_id,
                                        "object": "chat.completion.chunk",
                                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                    }
                                    yield f"data: {json.dumps(fix_chunk)}\n\n".encode("utf-8")
                                    saw_finish_reason = True

                                yield b"data: [DONE]\n\n"
                                continue

                            try:
                                chunk_json = json.loads(raw_data)
                                if "id" in chunk_json:
                                    last_id = chunk_json["id"]
                                choices = chunk_json.get("choices", [])
                                if choices and choices[0].get("finish_reason"):
                                    saw_finish_reason = True
                            except Exception:
                                pass

                        yield (line + "\n").encode("utf-8")

                    if not saw_finish_reason:
                        fix_chunk = {
                            "id": last_id,
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        }
                        yield f"data: {json.dumps(fix_chunk)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"

            except Exception as e:
                logger.error(f"Streaming error to Gemini API: {e}")
                err_chunk = {
                    "id": last_id,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": f"Proxy Error: {str(e)}"}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(err_chunk)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
            finally:
                await client.aclose()

        return StreamingResponse(sse_stream_generator(), media_type="text/event-stream")
    else:
        try:
            resp = await client.post(GEMINI_BASE_URL, headers=target_headers, json=body)
            await client.aclose()
            return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except Exception as e:
            await client.aclose()
            logger.error(f"Error forwarding to Gemini API: {e}")
            return Response(content=f'{{"error": "{str(e)}"}}', status_code=502, media_type="application/json")
