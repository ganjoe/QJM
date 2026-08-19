"""
Pydantic data models for the LLM Router engine.

Central schema definitions for WorkItems, Jobs, Endpoints, and Routing.
All models are defined here to avoid circular imports between engine modules.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EndpointType(str, Enum):
    """Whether the router manages the endpoint's lifecycle (Docker) or not."""
    MANAGED = "managed"
    UNMANAGED = "unmanaged"


class EndpointStatus(str, Enum):
    """Runtime health state of an endpoint."""
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class ApiSchema(str, Enum):
    """Wire protocol / API format of the backend."""
    OPENAI = "openai"
    LITELLM = "litellm"
    OLLAMA = "ollama"


class JobStatus(str, Enum):
    """Lifecycle state of a submitted job."""
    QUEUED = "queued"
    RUNNING = "running"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Endpoint Configuration & State
# ---------------------------------------------------------------------------

class HealthCheckConfig(BaseModel):
    """Per-endpoint health check parameters."""
    url: str
    interval_seconds: int = 10
    timeout_seconds: float = 2.0


class EndpointConfig(BaseModel):
    """
    Registration schema for an LLM endpoint (managed or unmanaged).

    Matches the Endpoint Registration Schema from the requirements spec.
    """
    endpoint_id: str
    name: str = ""
    type: EndpointType = EndpointType.UNMANAGED
    base_url: str
    api_schema: ApiSchema = ApiSchema.OPENAI
    model_name: str = ""
    max_concurrency: int = 2
    capabilities: list[str] = Field(default_factory=list)
    health_check: Optional[HealthCheckConfig] = None
    priority: int = 1                                       # Lower = higher priority
    fallback_for: list[str] = Field(default_factory=list)   # Capabilities this EP is a cloud fallback for
    docker_container: Optional[str] = None                  # Container name (managed endpoints only)


class EndpointState(BaseModel):
    """
    Runtime state for a registered endpoint.

    Combines the static config with live health/slot information.
    """
    config: EndpointConfig
    status: EndpointStatus = EndpointStatus.OFFLINE
    active_slots: int = 0
    last_health_check: Optional[datetime] = None
    consecutive_failures: int = 0
    last_latency_ms: Optional[float] = None


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

class RoutingConfig(BaseModel):
    """Per-WorkItem routing instructions."""
    capability_class: str = "fast"
    priority: int = 1
    allow_cloud_fallback: bool = False


class RoutingDecision(BaseModel):
    """Internal result of the routing algorithm."""
    endpoint: EndpointState
    is_fallback: bool = False
    reason: str = ""


# ---------------------------------------------------------------------------
# MCP (Model Context Protocol) — tool call configuration
# ---------------------------------------------------------------------------

class MCPToolConfig(BaseModel):
    """
    Configuration for a single MCP tool and its server.

    Included in the WorkItem's ``mcp_registry`` to tell the router how
    to reach the MCP server that implements a given tool.
    """
    server_id: str
    url: str
    timeout: float = 5.0
    max_concurrency: int = 10   # Per-server semaphore limit (FR-5.2)
    transport: str = "jsonrpc"  # "jsonrpc" or "rest" (NFR-3)



# ---------------------------------------------------------------------------
# WorkItem & Job
# ---------------------------------------------------------------------------

class WorkItemPayload(BaseModel):
    """The actual LLM request payload carried by a WorkItem."""
    messages: list[dict[str, Any]]
    temperature: float = 0.7
    max_tokens: int = 2048
    tools: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = True
    model: Optional[str] = None   # Optional model override


class WorkItem(BaseModel):
    """
    Top-level input schema for POST /v1/submit.

    Matches the WorkItem Schema from the requirements spec.
    """
    workitem_id: Optional[str] = None
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    payload: WorkItemPayload
    mcp_registry: dict[str, MCPToolConfig] = Field(default_factory=dict)


class Job(BaseModel):
    """
    Internal job representation tracking a WorkItem through its lifecycle.

    Created when a WorkItem is submitted; updated as it moves through
    QUEUED → RUNNING → STREAMING → COMPLETED/FAILED.
    """
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workitem: WorkItem
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    assigned_endpoint: Optional[str] = None
    error: Optional[str] = None
    # NOTE: The SSE event queue for this job lives in SSEManager, not here.
    # TODO: Persist to Supabase


# ---------------------------------------------------------------------------
# API Response Models
# ---------------------------------------------------------------------------

class SubmitResponse(BaseModel):
    """Response from POST /v1/submit."""
    job_id: str
    stream_url: str
    status: str = "queued"


class JobStatusResponse(BaseModel):
    """Response from GET /v1/jobs/{job_id}/status."""
    job_id: str
    status: JobStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    assigned_endpoint: Optional[str] = None
    error: Optional[str] = None
