"""
Request dispatcher — sends LLM requests to backend endpoints with streaming
and handles multi-turn MCP tool-call loops.

Flow:
  1. Send messages to LLM, stream tokens to SSE.
  2. Accumulate the full response while streaming.
  3. If the response contains tool_calls → fan-out via MCPExecutor.
  4. Append tool results to messages and re-send to LLM (next turn).
  5. Repeat until the LLM responds without tool_calls.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

import httpx

from engine.models import Job, EndpointState, ApiSchema, MCPToolConfig

logger = logging.getLogger("engine.dispatcher")

# HTTP timeout for LLM backend requests.
# 300s (5 min) to accommodate large-model inference on local hardware.
_LLM_REQUEST_TIMEOUT = httpx.Timeout(300.0, connect=10.0)

# Maximum number of MCP tool-call turns to prevent infinite loops
_MAX_MCP_TURNS = 10


class Dispatcher:
    """
    Sends LLM inference requests to a target endpoint, streams tokens back,
    and orchestrates MCP tool-call execution between turns.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._mcp_executor = None  # Set via set_mcp_executor()

    def set_mcp_executor(self, executor) -> None:
        """Inject the MCPExecutor (avoids circular import)."""
        self._mcp_executor = executor

    async def start(self) -> None:
        """Create the shared HTTP client (call at app startup)."""
        self._client = httpx.AsyncClient(timeout=_LLM_REQUEST_TIMEOUT)
        logger.info("Dispatcher HTTP client initialized (timeout=%ss)", _LLM_REQUEST_TIMEOUT.read)

    async def stop(self) -> None:
        """Close the HTTP client (call at app shutdown)."""
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("Dispatcher stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def dispatch(
        self, job: Job, endpoint: EndpointState,
    ) -> AsyncGenerator[dict, None]:
        """
        Stream a request to the endpoint, handling MCP tool-call turns.

        Yields chunk dicts for the SSE pipeline. If the LLM returns
        tool_calls, they are executed via the MCPExecutor and the results
        fed back for the next turn — all transparently streamed.
        """
        if not self._client:
            raise RuntimeError("Dispatcher not started — call start() first")

        messages = list(job.workitem.payload.messages)
        mcp_registry = job.workitem.mcp_registry
        has_mcp = bool(mcp_registry) and self._mcp_executor is not None

        for turn in range(_MAX_MCP_TURNS):
            # --- Stream one LLM turn ---
            accumulated = _ResponseAccumulator()

            async for chunk in self._stream_turn(job, endpoint, messages):
                accumulated.feed(chunk)
                yield chunk

            # --- Check for tool_calls ---
            tool_calls = accumulated.tool_calls
            if not tool_calls or not has_mcp:
                # No tool calls or no MCP registry → we're done
                break

            # --- MCP Fan-Out / Fan-In ---
            logger.info(
                "Job %s turn %d: %d tool_calls detected, executing MCP…",
                job.job_id, turn + 1, len(tool_calls),
            )

            # Yield a status event so the client knows MCP is happening
            yield {
                "type": "mcp_status",
                "turn": turn + 1,
                "tool_calls": [
                    {"id": tc.get("id"), "name": tc.get("function", {}).get("name")}
                    for tc in tool_calls
                ],
                "status": "executing",
            }

            # Execute all tool calls in parallel
            tool_messages = await self._mcp_executor.execute_tool_calls(
                tool_calls, mcp_registry,
            )

            # Yield MCP results as events (for client visibility)
            yield {
                "type": "mcp_results",
                "turn": turn + 1,
                "results": [
                    {"tool_call_id": m["tool_call_id"], "content": m["content"][:200]}
                    for m in tool_messages
                ],
            }

            # Build the assistant message with tool_calls for the conversation
            assistant_msg = accumulated.build_assistant_message()
            messages.append(assistant_msg)

            # Append all tool results to the conversation
            messages.extend(tool_messages)

            logger.info(
                "Job %s turn %d: MCP complete, sending %d tool results to LLM",
                job.job_id, turn + 1, len(tool_messages),
            )
            # Loop → next LLM turn with tool results in context

        else:
            logger.warning(
                "Job %s hit max MCP turns (%d), stopping",
                job.job_id, _MAX_MCP_TURNS,
            )

    # ------------------------------------------------------------------
    # Single-turn streaming
    # ------------------------------------------------------------------

    async def _stream_turn(
        self, job: Job, endpoint: EndpointState, messages: list[dict],
    ) -> AsyncGenerator[dict, None]:
        """Stream one LLM turn (no MCP handling)."""
        schema = endpoint.config.api_schema

        if schema in (ApiSchema.OPENAI, ApiSchema.LITELLM):
            gen = self._dispatch_openai(job, endpoint, messages)
        elif schema == ApiSchema.OLLAMA:
            gen = self._dispatch_ollama(job, endpoint, messages)
        else:
            raise ValueError(f"Unsupported API schema: {schema}")

        async for chunk in gen:
            yield chunk

    # ------------------------------------------------------------------
    # OpenAI-compatible adapter (/v1/chat/completions, stream=true)
    # ------------------------------------------------------------------

    async def _dispatch_openai(
        self, job: Job, endpoint: EndpointState, messages: list[dict],
    ) -> AsyncGenerator[dict, None]:
        """Stream from an OpenAI-compatible backend (vLLM, LM Studio, LiteLLM)."""
        base = endpoint.config.base_url.rstrip("/")
        url = f"{base}/chat/completions"

        body: dict = {
            "model": (
                endpoint.config.model_name
                or job.workitem.payload.model
                or "default"
            ),
            "messages": messages,
            "temperature": job.workitem.payload.temperature,
            "max_tokens": job.workitem.payload.max_tokens,
            "stream": True,
        }
        if job.workitem.payload.tools:
            body["tools"] = job.workitem.payload.tools

        logger.info("Dispatch [openai] job=%s → %s  model=%s", job.job_id, url, body["model"])

        try:
            async with self._client.stream(
                "POST", url, json=body,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status_code != 200:
                    raw = await resp.aread()
                    raise RuntimeError(
                        f"Backend {resp.status_code}: {raw.decode(errors='replace')[:500]}"
                    )

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            choices = chunk.get("choices", [{}])
                            delta = choices[0].get("delta", {}) if choices else {}
                            yield {
                                "type": "chunk",
                                "delta": delta,
                                "model": chunk.get("model", ""),
                                "id": chunk.get("id", ""),
                                "finish_reason": (
                                    choices[0].get("finish_reason")
                                    if choices else None
                                ),
                            }
                        except json.JSONDecodeError:
                            logger.warning("Unparseable chunk: %s", data_str[:120])

        except httpx.ConnectError as exc:
            raise RuntimeError(f"Connection failed to {url}: {exc}") from exc
        except httpx.ReadTimeout as exc:
            raise RuntimeError(f"Read timeout from {url}: {exc}") from exc

    # ------------------------------------------------------------------
    # Ollama adapter (/api/chat, NDJSON streaming)
    # ------------------------------------------------------------------

    async def _dispatch_ollama(
        self, job: Job, endpoint: EndpointState, messages: list[dict],
    ) -> AsyncGenerator[dict, None]:
        """Stream from an Ollama backend (/api/chat NDJSON)."""
        base = endpoint.config.base_url.rstrip("/")
        url = f"{base}/api/chat"

        body = {
            "model": endpoint.config.model_name or "llama3:8b",
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": job.workitem.payload.temperature,
                "num_predict": job.workitem.payload.max_tokens,
            },
        }

        logger.info("Dispatch [ollama] job=%s → %s  model=%s", job.job_id, url, body["model"])

        try:
            async with self._client.stream("POST", url, json=body) as resp:
                if resp.status_code != 200:
                    raw = await resp.aread()
                    raise RuntimeError(
                        f"Ollama {resp.status_code}: {raw.decode(errors='replace')[:500]}"
                    )

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                        msg = chunk.get("message", {})
                        yield {
                            "type": "chunk",
                            "delta": {
                                "role": msg.get("role", "assistant"),
                                "content": msg.get("content", ""),
                            },
                            "model": chunk.get("model", ""),
                            "done": chunk.get("done", False),
                        }
                        if chunk.get("done"):
                            break
                    except json.JSONDecodeError:
                        logger.warning("Unparseable Ollama chunk: %s", line[:120])

        except httpx.ConnectError as exc:
            raise RuntimeError(f"Connection failed to Ollama at {url}: {exc}") from exc
        except httpx.ReadTimeout as exc:
            raise RuntimeError(f"Read timeout from Ollama at {url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Response accumulator — collects streamed chunks to detect tool_calls
# ---------------------------------------------------------------------------

class _ResponseAccumulator:
    """
    Reassembles a full assistant message from streaming delta chunks.

    After streaming completes, provides:
      - ``content``: the concatenated text content
      - ``tool_calls``: list of tool_call dicts (if any)
      - ``build_assistant_message()``: a complete assistant message dict
    """

    def __init__(self) -> None:
        self._content_parts: list[str] = []
        self._tool_calls: dict[int, dict] = {}  # index → partial tool_call
        self._model: str = ""

    def feed(self, chunk: dict) -> None:
        """Process one streaming chunk."""
        if chunk.get("type") != "chunk":
            return

        delta = chunk.get("delta", {})
        self._model = chunk.get("model", self._model)

        # Accumulate text content
        content = delta.get("content")
        if content:
            self._content_parts.append(content)

        # Accumulate tool_calls (streamed incrementally)
        tc_deltas = delta.get("tool_calls", [])
        for tc_delta in tc_deltas:
            idx = tc_delta.get("index", 0)

            if idx not in self._tool_calls:
                self._tool_calls[idx] = {
                    "id": tc_delta.get("id", ""),
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }

            tc = self._tool_calls[idx]
            if tc_delta.get("id"):
                tc["id"] = tc_delta["id"]

            func = tc_delta.get("function", {})
            if func.get("name"):
                tc["function"]["name"] = func["name"]
            if func.get("arguments"):
                tc["function"]["arguments"] += func["arguments"]

    @property
    def content(self) -> str:
        return "".join(self._content_parts)

    @property
    def tool_calls(self) -> list[dict]:
        """Return accumulated tool_calls, or empty list if none."""
        if not self._tool_calls:
            return []
        return [self._tool_calls[i] for i in sorted(self._tool_calls)]

    def build_assistant_message(self) -> dict:
        """Build a complete assistant message dict for the conversation."""
        msg: dict = {"role": "assistant"}
        if self._content_parts:
            msg["content"] = self.content
        if self._tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg
