import os
import json
import logging
import time
from collections import OrderedDict

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

# ---------------------------------------------------------------------------
# Thought Signature Cache
# ---------------------------------------------------------------------------
# Gemini 3.x models include a `thought_signature` with tool_call responses.
# This is an encrypted snapshot of the model's reasoning state, required by
# Google's API on subsequent turns that reference the tool_call.
#
# OpenAI-compatible clients (like dsh/Switchyard) strip this non-standard
# field from conversation history. We cache the full assistant message on
# response and re-inject it on the next request.
# ---------------------------------------------------------------------------

_THOUGHT_CACHE_MAX = int(os.getenv("THOUGHT_CACHE_MAX_ENTRIES", "500"))
_THOUGHT_CACHE_TTL = int(os.getenv("THOUGHT_CACHE_TTL_SECONDS", "1800"))


class _ThoughtSignatureCache:
    """LRU + TTL cache: maps tool_call_id → full assistant message dict."""

    def __init__(self, max_entries: int, ttl_seconds: int):
        self._store: OrderedDict[str, dict] = OrderedDict()
        self._ts: dict[str, float] = {}
        self._max = max_entries
        self._ttl = ttl_seconds

    def put(self, message: dict):
        """Cache a full assistant message, keyed by each of its tool_call IDs."""
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return
        now = time.time()
        self._evict(now)
        for tc in tool_calls:
            tc_id = tc.get("id")
            if not tc_id:
                continue
            self._store[tc_id] = message
            self._store.move_to_end(tc_id)
            self._ts[tc_id] = now

    def get(self, tool_call_id: str) -> dict | None:
        """Retrieve a cached assistant message by tool_call_id."""
        msg = self._store.get(tool_call_id)
        if msg is None:
            return None
        if time.time() - self._ts.get(tool_call_id, 0) >= self._ttl:
            self._store.pop(tool_call_id, None)
            self._ts.pop(tool_call_id, None)
            return None
        return msg

    def _evict(self, now: float):
        # Remove expired entries
        expired = [k for k, t in self._ts.items() if now - t >= self._ttl]
        for k in expired:
            self._store.pop(k, None)
            self._ts.pop(k, None)
        # Trim to max size
        while len(self._store) > self._max:
            k, _ = self._store.popitem(last=False)
            self._ts.pop(k, None)


_thought_cache = _ThoughtSignatureCache(_THOUGHT_CACHE_MAX, _THOUGHT_CACHE_TTL)


def _inject_cached_signatures(body: dict):
    """Replace assistant tool_call messages with cached versions that include thought_signature."""
    messages = body.get("messages", [])
    patched = 0
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        first_tc_id = msg["tool_calls"][0].get("id", "")
        cached = _thought_cache.get(first_tc_id)
        if cached:
            messages[i] = cached
            patched += 1
    if patched:
        logger.info(f"[ThoughtSig] Injected {patched} cached assistant message(s)")


def _cache_from_message(msg: dict):
    """Cache an assistant message if it contains tool_calls."""
    if msg.get("tool_calls"):
        _thought_cache.put(msg)
        tc_ids = [tc.get("id", "?") for tc in msg["tool_calls"]]
        logger.info(f"[ThoughtSig] Cached assistant message for tool_call_ids={tc_ids}")


# ---------------------------------------------------------------------------
# Streaming Accumulator — reconstructs the full assistant message from chunks
# ---------------------------------------------------------------------------

class _StreamAccumulator:
    """Accumulates SSE delta chunks into a complete assistant message for caching."""

    def __init__(self):
        self.content_parts: list[str] = []
        self.tool_calls: dict[int, dict] = {}  # index → accumulated tool_call
        self.extra_fields: dict = {}  # non-standard fields (e.g. thought_signature)

    def feed_chunk(self, chunk_json: dict):
        """Feed a parsed SSE chunk into the accumulator."""
        choices = chunk_json.get("choices", [])
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta", {})

        # Accumulate text content
        if delta.get("content"):
            self.content_parts.append(delta["content"])

        # Accumulate tool_calls (they arrive in parts across multiple chunks)
        for tc_delta in delta.get("tool_calls", []):
            idx = tc_delta.get("index", 0)
            if idx not in self.tool_calls:
                self.tool_calls[idx] = {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            entry = self.tool_calls[idx]
            if tc_delta.get("id"):
                entry["id"] = tc_delta["id"]
            if tc_delta.get("type"):
                entry["type"] = tc_delta["type"]
            func = tc_delta.get("function", {})
            if func.get("name"):
                entry["function"]["name"] += func["name"]
            if "arguments" in func:
                entry["function"]["arguments"] += func["arguments"]

        # Capture any extra fields on delta (e.g. thought_signature, thinking)
        for key, val in delta.items():
            if key not in ("role", "content", "tool_calls", "refusal", "function_call"):
                self.extra_fields[key] = val

        # Also check choice-level extra fields
        for key, val in choice.items():
            if key not in ("index", "delta", "finish_reason", "logprobs"):
                self.extra_fields[f"_choice_{key}"] = val

    def build_message(self) -> dict | None:
        """Build the complete assistant message. Returns None if no tool_calls were seen."""
        if not self.tool_calls:
            return None
        msg: dict = {
            "role": "assistant",
            "content": "".join(self.content_parts) if self.content_parts else None,
            "tool_calls": [self.tool_calls[i] for i in sorted(self.tool_calls.keys())],
        }
        # Attach any extra fields we captured (thought_signature, etc.)
        for key, val in self.extra_fields.items():
            if not key.startswith("_choice_"):
                msg[key] = val
        return msg


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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
    4. Caches & re-injects Gemini 3.x thought_signatures for tool-call round-trips.
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

    # 3. Re-inject cached thought_signatures into assistant tool_call messages
    _inject_cached_signatures(body)

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
            accumulator = _StreamAccumulator()
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
                                # Cache the accumulated message before closing
                                built_msg = accumulator.build_message()
                                if built_msg:
                                    _cache_from_message(built_msg)

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

                                # Feed chunk to accumulator for thought_signature caching
                                accumulator.feed_chunk(chunk_json)
                            except Exception:
                                pass

                        yield (line + "\n").encode("utf-8")

                    # Stream ended without [DONE] — still try to cache
                    built_msg = accumulator.build_message()
                    if built_msg:
                        _cache_from_message(built_msg)

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

            # Cache assistant message with tool_calls from non-streaming response
            if resp.status_code == 200:
                try:
                    resp_data = json.loads(resp.content)
                    msg = resp_data.get("choices", [{}])[0].get("message", {})
                    _cache_from_message(msg)
                except Exception:
                    pass

            return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except Exception as e:
            await client.aclose()
            logger.error(f"Error forwarding to Gemini API: {e}")
            return Response(content=f'{{"error": "{str(e)}"}}', status_code=502, media_type="application/json")
