"""
Pydantic data models for the LLM Gateway Control Plane.

Schemas for Container Lifecycle, Hardware Telemetry, and Switchyard Routing.
"""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


class ContainerInfo(BaseModel):
    """Runtime information and resource stats for a managed container."""
    id: str
    name: str
    image: str
    status: str
    state: str = "unknown"
    cpu_percent: Optional[float] = None
    memory_usage_mb: Optional[float] = None
    memory_limit_mb: Optional[float] = None
    memory_percent: Optional[float] = None
    ports: list[str] = Field(default_factory=list)
    created: str = ""


class ContainerActionResponse(BaseModel):
    """Response returned when starting, stopping, or restarting a container."""
    success: bool
    container_name: str
    message: str


class GPUStats(BaseModel):
    """GPU / VRAM Telemetry (e.g. AMD ROCm / Vulkan GPU)."""
    name: str = "AMD Radeon GPU"
    vram_used_mb: Optional[float] = None
    vram_total_mb: Optional[float] = None
    vram_percent: Optional[float] = None
    gpu_utilization_percent: Optional[float] = None
    temperature_c: Optional[float] = None


class SystemStats(BaseModel):
    """Host Hardware and Resource Telemetry."""
    cpu_percent: float
    cpu_cores: int
    memory_total_gb: float
    memory_used_gb: float
    memory_percent: float
    gpu: Optional[GPUStats] = None


class SwitchyardStatus(BaseModel):
    """Switchyard Gateway Health & Model Status."""
    healthy: bool
    url: str
    active_models: list[str] = Field(default_factory=list)
    routes: list[dict[str, Any]] = Field(default_factory=list)
    targets: list[dict[str, Any]] = Field(default_factory=list)
