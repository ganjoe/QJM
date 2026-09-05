import os
import json
import logging
import time
from collections import OrderedDict
from typing import Optional, Dict, Any, List

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

logger = logging.getLogger("dashboard.gemini_proxy")
router = APIRouter(prefix="/api/gemini/v1", tags=["Gemini Proxy"])

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

# Unsupported fields by Google's direct OpenAI REST parser
STRIP_FIELDS = {
    "store",
    "metadata",
    "service_tier",
    "modalities",
    "stream_options",
    "parallel_tool_calls",
}

# Exact active Google AI Studio model IDs
MODEL_MAP = {
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview": "gemini-3.1-pro-preview",
    "gemini-3-pro": "gemini-3.1-pro-preview",
    "pro": "gemini-3.1-pro-preview",
    "reasoning": "gemini-3.1-pro-preview",
    "gemini-3.8-flash": "gemini-3.8-flash",
    "gemini-3.6-flash": "gemini-3.6-flash",
    "gemini-3-flash": "gemini-3.8-flash",
    "fast": "gemini-3.8-flash",
    "flash": "gemini-3.8-flash",
    "gemini-3.5-flash": "gemini-3.5-flash",
    "gemini-3.5-flash-lite": "gemini-3.5-flash-lite",
    "lite": "gemini-3.5-flash-lite",
    "free": "gemini-3.5-flash-lite",
    "auto": "gemini-3.8-flash",
}

# ---------------------------------------------------------------------------
# Thought Signature Cache & Validator
# ---------------------------------------------------------------------------
# Gemini 2.5/3.x models include a `thought_signature` with tool_call responses.
# This is an encrypted snapshot of the model's reasoning state, required by
# Google's API on subsequent turns that reference the tool_call.
#
# Standard OpenAI-compatible clients (like dsh/Switchyard) do not include this
# non-standard field in conversation history. We cache the full assistant message
# on response and re-inject it on the next request. If no signature was saved,
# Google provides the sentinel "skip_thought_signature_validator" to bypass 400 errors.
# ---------------------------------------------------------------------------

_THOUGHT_CACHE_MAX = int(os.getenv("THOUGHT_CACHE_MAX_ENTRIES", "500"))
_THOUGHT_CACHE_TTL = int(os.getenv("THOUGHT_CACHE_TTL_SECONDS", "1800"))
FALLBACK_THOUGHT_SIGNATURE = "skip_thought_signature_validator"


class _ThoughtSignatureCache:
    """LRU + TTL cache: maps tool_call_id → full assistant message dict and tool_call dict."""

    def __init__(self, max_entries: int, ttl_seconds: int):
        self._store: OrderedDict[str, dict] = OrderedDict()
        self._tc_store: OrderedDict[str, dict] = OrderedDict()
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
            self._tc_store[tc_id] = tc
            self._tc_store.move_to_end(tc_id)
            self._ts[tc_id] = now

    def get_message(self, tool_call_id: str) -> Optional[dict]:
        """Retrieve a cached assistant message by tool_call_id."""
        msg = self._store.get(tool_call_id)
        if msg is None:
            return None
        if time.time() - self._ts.get(tool_call_id, 0) >= self._ttl:
            self._store.pop(tool_call_id, None)
            self._tc_store.pop(tool_call_id, None)
            self._ts.pop(tool_call_id, None)
            return None
        return msg

    def get_tool_call(self, tool_call_id: str) -> Optional[dict]:
        """Retrieve a cached tool_call dict by tool_call_id."""
        tc = self._tc_store.get(tool_call_id)
        if tc is None:
            return None
        if time.time() - self._ts.get(tool_call_id, 0) >= self._ttl:
            self._store.pop(tool_call_id, None)
            self._tc_store.pop(tool_call_id, None)
            self._ts.pop(tool_call_id, None)
            return None
        return tc

    def _evict(self, now: float):
        # Remove expired entries
        expired = [k for k, t in self._ts.items() if now - t >= self._ttl]
        for k in expired:
            self._store.pop(k, None)
            self._tc_store.pop(k, None)
            self._ts.pop(k, None)
        # Trim to max size
        while len(self._store) > self._max:
            k, _ = self._store.popitem(last=False)
            self._tc_store.pop(k, None)
            self._ts.pop(k, None)


_thought_cache = _ThoughtSignatureCache(_THOUGHT_CACHE_MAX, _THOUGHT_CACHE_TTL)


def _extract_signature(item: dict) -> Optional[str]:
    """Extract thought_signature from various possible locations in a tool_call or message dict."""
    if not isinstance(item, dict):
        return None
    # Check extra_content.google.thought_signature (Standard Google OpenAI format)
    extra = item.get("extra_content")
    if isinstance(extra, dict):
        google_extra = extra.get("google")
        if isinstance(google_extra, dict) and google_extra.get("thought_signature"):
            return str(google_extra["thought_signature"])
    # Check top-level thought_signature
    if item.get("thought_signature"):
        return str(item["thought_signature"])
    # Check function.thought_signature
    func = item.get("function")
    if isinstance(func, dict):
        if func.get("thought_signature"):
            return str(func["thought_signature"])
        extra_f = func.get("extra_content")
        if isinstance(extra_f, dict):
            google_f = extra_f.get("google")
            if isinstance(google_f, dict) and google_f.get("thought_signature"):
                return str(google_f["thought_signature"])
    return None


def _inject_cached_signatures(body: dict):
    """
    Ensures that every assistant message with tool_calls contains the necessary
    thought_signature (either from cache or fallback sentinel) in extra_content.google.
    Google Gemini API strictly enforces thought_signature inside extra_content.google.
    """
    messages = body.get("messages", [])
    patched = 0
    for msg in messages:
        if msg.get("role") not in ("assistant", "model"):
            continue

        # Handle tool_calls
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "")
                cached_tc = _thought_cache.get_tool_call(tc_id) if tc_id else None
                cached_msg = _thought_cache.get_message(tc_id) if tc_id else None

                sig = None
                if cached_tc:
                    sig = _extract_signature(cached_tc)
                if not sig and cached_msg:
                    sig = _extract_signature(cached_msg)
                if not sig:
                    sig = _extract_signature(tc)
                if not sig:
                    sig = _extract_signature(msg)
                if not sig:
                    sig = FALLBACK_THOUGHT_SIGNATURE

                # Inject into Google's required OpenAI structure: extra_content.google.thought_signature
                if "extra_content" not in tc or not isinstance(tc["extra_content"], dict):
                    tc["extra_content"] = {}
                if "google" not in tc["extra_content"] or not isinstance(tc["extra_content"]["google"], dict):
                    tc["extra_content"]["google"] = {}
                tc["extra_content"]["google"]["thought_signature"] = sig

                # Also set legacy top-level for backward compatibility
                tc["thought_signature"] = sig
                if "function" in tc and isinstance(tc["function"], dict):
                    tc["function"]["thought_signature"] = sig
                patched += 1

            if not msg.get("thought_signature") and msg["tool_calls"]:
                first_sig = _extract_signature(msg["tool_calls"][0]) or FALLBACK_THOUGHT_SIGNATURE
                msg["thought_signature"] = first_sig

        # Handle legacy function_call
        if msg.get("function_call"):
            fc = msg["function_call"]
            sig = _extract_signature(fc) or _extract_signature(msg) or FALLBACK_THOUGHT_SIGNATURE
            if "extra_content" not in fc or not isinstance(fc["extra_content"], dict):
                fc["extra_content"] = {}
            if "google" not in fc["extra_content"] or not isinstance(fc["extra_content"]["google"], dict):
                fc["extra_content"]["google"] = {}
            fc["extra_content"]["google"]["thought_signature"] = sig
            fc["thought_signature"] = sig
            msg["thought_signature"] = sig
            patched += 1

    if patched:
        logger.info(f"[ThoughtSig] Injected/validated thought_signature for {patched} tool_call/function_call entry(s)")


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
                    "extra_content": {"google": {}},
                }
            entry = self.tool_calls[idx]
            for k, v in tc_delta.items():
                if k not in ("index", "function", "extra_content"):
                    if k in ("id", "type"):
                        if v:
                            entry[k] = v
                    else:
                        entry[k] = v
                elif k == "extra_content" and isinstance(v, dict):
                    entry.setdefault("extra_content", {})
                    google_data = v.get("google", {})
                    if isinstance(google_data, dict):
                        entry["extra_content"].setdefault("google", {}).update(google_data)

            func = tc_delta.get("function", {})
            if func.get("name"):
                entry["function"]["name"] += func["name"]
            if "arguments" in func:
                entry["function"]["arguments"] += func["arguments"]
            for fk, fv in func.items():
                if fk not in ("name", "arguments"):
                    entry["function"][fk] = fv

        # Capture any extra fields on delta (e.g. thought_signature, thinking)
        for key, val in delta.items():
            if key not in ("role", "content", "tool_calls", "refusal", "function_call"):
                self.extra_fields[key] = val

        # Also check choice-level extra fields
        for key, val in choice.items():
            if key not in ("index", "delta", "finish_reason", "logprobs"):
                self.extra_fields[f"_choice_{key}"] = val

        # Also check chunk-level extra fields
        for key, val in chunk_json.items():
            if key not in ("id", "object", "created", "model", "choices", "usage", "system_fingerprint"):
                self.extra_fields[f"_chunk_{key}"] = val

    def build_message(self) -> Optional[dict]:
        """Build the complete assistant message. Returns None if no tool_calls were seen."""
        if not self.tool_calls:
            return None

        tool_calls_list = [self.tool_calls[i] for i in sorted(self.tool_calls.keys())]
        for tc in tool_calls_list:
            sig = _extract_signature(tc)
            if not sig:
                sig = self.extra_fields.get("thought_signature") or FALLBACK_THOUGHT_SIGNATURE

            if "extra_content" not in tc or not isinstance(tc["extra_content"], dict):
                tc["extra_content"] = {}
            if "google" not in tc["extra_content"] or not isinstance(tc["extra_content"]["google"], dict):
                tc["extra_content"]["google"] = {}
            tc["extra_content"]["google"]["thought_signature"] = sig
            tc["thought_signature"] = sig

        msg: dict = {
            "role": "assistant",
            "content": "".join(self.content_parts) if self.content_parts else None,
            "tool_calls": tool_calls_list,
        }
        # Attach any extra fields we captured (thought_signature, etc.)
        for key, val in self.extra_fields.items():
            if not key.startswith("_choice_") and not key.startswith("_chunk_"):
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
    1. Sanitizes OpenAI client fields ('store', 'metadata', 'stream_options', etc.).
    2. Maps forward-looking model IDs to verified Google AI Studio models.
    3. Normalizes streaming chunks so that finish_reason is always sent before [DONE].
    4. Caches & re-injects Gemini 3.x thought_signatures (or fallback sentinels) for tool-call round-trips.
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

    # Force large max_tokens for reasoning models to prevent thought/tool_call truncation
    if body["model"] == "gemini-3.1-pro-preview" or "reasoning" in model_req.lower():
        body.pop("max_tokens", None)
        body.pop("max_completion_tokens", None)
        body["max_tokens"] = 50000

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
