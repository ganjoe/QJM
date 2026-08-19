"""
Parallel MCP (Model Context Protocol) Execution Engine.

Handles fan-out / fan-in execution of LLM tool_calls against registered
MCP servers.  Each call is isolated with its own timeout and the entire
batch is rate-limited per MCP-server via asyncio.Semaphore.

Transport layer:
  The actual HTTP communication is handled by pluggable MCPTransport
  implementations (NFR-3).  The default ``HttpJsonRpcTransport`` sends
  standard JSON-RPC 2.0 requests over HTTP POST.
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging
from typing import Any, Optional

import httpx

from engine.models import MCPToolConfig

logger = logging.getLogger("engine.mcp")

# Default max concurrent requests per MCP server
_DEFAULT_SERVER_CONCURRENCY = 10


# ---------------------------------------------------------------------------
# Transport abstraction (NFR-3: modular MCP transport layer)
# ---------------------------------------------------------------------------

class MCPTransport(abc.ABC):
    """
    Abstract transport for communicating with MCP servers.

    Implementations handle the wire protocol (HTTP/REST, JSON-RPC, SSE, stdio).
    """

    @abc.abstractmethod
    async def call_tool(
        self,
        url: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> str:
        """
        Invoke a tool on an MCP server and return the result as a string.

        Raises on transport-level errors (connection, protocol).
        Timeout is enforced by the caller via asyncio.wait_for.
        """
        ...

    async def start(self) -> None:
        """Optional setup (e.g. open HTTP client pool)."""

    async def stop(self) -> None:
        """Optional teardown."""


class HttpJsonRpcTransport(MCPTransport):
    """
    HTTP POST transport using JSON-RPC 2.0 message format.

    Request body::

        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "<tool>", "arguments": {...}},
            "id": 1
        }

    Expected response::

        {
            "jsonrpc": "2.0",
            "result": {"content": [{"type": "text", "text": "..."}]},
            "id": 1
        }
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=5.0),
        )
        logger.info("MCP HttpJsonRpcTransport started")

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("MCP HttpJsonRpcTransport stopped")

    async def call_tool(
        self,
        url: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> str:
        if not self._client:
            raise RuntimeError("Transport not started — call start() first")

        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
            "id": 1,
        }

        resp = await self._client.post(
            url,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"MCP server returned {resp.status_code}: "
                f"{resp.text[:300]}"
            )

        data = resp.json()

        # Handle JSON-RPC error
        if "error" in data:
            err = data["error"]
            msg = err.get("message", str(err))
            raise RuntimeError(f"MCP error: {msg}")

        # Extract result — MCP returns content array
        result = data.get("result", {})
        content_list = result.get("content", [])
        if content_list:
            # Concatenate all text content blocks
            texts = [
                c.get("text", "")
                for c in content_list
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            return "\n".join(texts) if texts else json.dumps(result)

        return json.dumps(result)


class HttpRestTransport(MCPTransport):
    """
    Simple HTTP REST transport for non-JSON-RPC MCP servers.

    Sends a POST to ``{url}/{tool_name}`` with the arguments as JSON body.
    Expects a plain JSON response whose string representation is the result.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=5.0),
        )

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def call_tool(
        self,
        url: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> str:
        if not self._client:
            raise RuntimeError("Transport not started")

        target = f"{url.rstrip('/')}/{tool_name}"
        resp = await self._client.post(
            target, json=arguments, timeout=timeout,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"MCP REST {resp.status_code}: {resp.text[:300]}"
            )

        try:
            data = resp.json()
            return json.dumps(data) if not isinstance(data, str) else data
        except Exception:
            return resp.text


# ---------------------------------------------------------------------------
# MCP Executor
# ---------------------------------------------------------------------------

class MCPExecutor:
    """
    Parallel MCP tool-call execution engine.

    Responsibilities:
      - FR-5.1: Fan-out all tool_calls concurrently via asyncio.gather.
      - FR-5.2: Rate-limit per MCP server via asyncio.Semaphore.
      - FR-5.3: Isolate each call with asyncio.wait_for timeout; on failure
                 generate a structured error tool-message instead of aborting.
      - FR-5.4: Return only when ALL calls have completed or timed out
                 (turn completion).
    """

    def __init__(
        self,
        transport: MCPTransport | None = None,
        default_server_concurrency: int = _DEFAULT_SERVER_CONCURRENCY,
    ) -> None:
        self._transport = transport or HttpJsonRpcTransport()
        self._default_concurrency = default_server_concurrency
        # server_id → Semaphore (lazily created)
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    async def start(self) -> None:
        """Start the underlying transport."""
        await self._transport.start()
        logger.info(
            "MCPExecutor started (transport=%s, default_concurrency=%d)",
            type(self._transport).__name__,
            self._default_concurrency,
        )

    async def stop(self) -> None:
        """Stop the transport and release resources."""
        await self._transport.stop()
        self._semaphores.clear()
        logger.info("MCPExecutor stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_tool_calls(
        self,
        tool_calls: list[dict],
        mcp_registry: dict[str, MCPToolConfig],
    ) -> list[dict]:
        """
        Execute all tool_calls in parallel and return tool-role messages.

        Each tool_call is dispatched concurrently.  Failures and timeouts
        are isolated — they produce error messages rather than exceptions.

        Parameters
        ----------
        tool_calls : list[dict]
            The ``tool_calls`` array from the LLM assistant message.
            Each item has ``id``, ``type``, ``function.name``, ``function.arguments``.
        mcp_registry : dict[str, MCPToolConfig]
            Mapping of tool function names to their MCP server configuration,
            as provided in the original WorkItem.

        Returns
        -------
        list[dict]
            One ``{"role": "tool", "tool_call_id": "...", "content": "..."}``
            per tool_call, in the same order.  Errors/timeouts are encoded
            in the ``content`` field.
        """
        if not tool_calls:
            return []

        tasks = []
        for tc in tool_calls:
            func = tc.get("function", {})
            func_name = func.get("name", "")
            tool_call_id = tc.get("id", "")
            raw_args = func.get("arguments", "{}")

            # Parse arguments (may be a JSON string or dict)
            if isinstance(raw_args, str):
                try:
                    arguments = json.loads(raw_args)
                except json.JSONDecodeError:
                    arguments = {"raw": raw_args}
            else:
                arguments = raw_args

            config = mcp_registry.get(func_name)
            if config is None:
                # Tool not in registry → immediate error
                tasks.append(
                    self._make_error(
                        tool_call_id,
                        f"Tool '{func_name}' not found in mcp_registry",
                    )
                )
            else:
                tasks.append(
                    self._execute_single(
                        tool_call_id, func_name, arguments, config,
                    )
                )

        # FR-5.4: Wait for ALL calls to finish (gather = turn completion)
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Convert any unexpected exceptions to error messages
        tool_messages = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                tc_id = tool_calls[i].get("id", f"unknown-{i}")
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": f"Error: {result}",
                })
            else:
                tool_messages.append(result)

        logger.info(
            "MCP turn complete: %d/%d calls succeeded",
            sum(1 for m in tool_messages if not m["content"].startswith("Error:")),
            len(tool_messages),
        )
        return tool_messages

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _execute_single(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        config: MCPToolConfig,
    ) -> dict:
        """
        Execute one tool call with semaphore rate-limiting and timeout.

        Returns a tool-role message dict — never raises.
        """
        sem = self._get_semaphore(config.server_id, config.max_concurrency)

        # FR-5.2: Acquire per-server semaphore
        async with sem:
            try:
                # FR-5.3: Per-call timeout via asyncio.wait_for
                result = await asyncio.wait_for(
                    self._transport.call_tool(
                        url=config.url,
                        tool_name=tool_name,
                        arguments=arguments,
                        timeout=config.timeout,
                    ),
                    timeout=config.timeout,
                )
                logger.debug(
                    "MCP call OK: %s → %s (%.1fs)",
                    tool_name, config.server_id, config.timeout,
                )
                return {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result,
                }

            except asyncio.TimeoutError:
                # FR-5.3: Structured error for timeout
                logger.warning(
                    "MCP timeout: %s on %s after %.1fs",
                    tool_name, config.server_id, config.timeout,
                )
                return {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": f"Error: Timeout nach {config.timeout}s",
                }

            except Exception as exc:
                # FR-5.3: Structured error for any failure
                logger.warning(
                    "MCP error: %s on %s — %s",
                    tool_name, config.server_id, exc,
                )
                return {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": f"Error: {exc}",
                }

    def _get_semaphore(
        self, server_id: str, max_concurrency: Optional[int] = None,
    ) -> asyncio.Semaphore:
        """Get or create a per-server semaphore."""
        if server_id not in self._semaphores:
            limit = max_concurrency or self._default_concurrency
            self._semaphores[server_id] = asyncio.Semaphore(limit)
            logger.debug(
                "Created semaphore for MCP server '%s' (limit=%d)",
                server_id, limit,
            )
        return self._semaphores[server_id]

    @staticmethod
    async def _make_error(tool_call_id: str, message: str) -> dict:
        """Create an error tool-message (used as a coroutine in gather)."""
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": f"Error: {message}",
        }
