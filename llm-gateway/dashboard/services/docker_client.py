import os
import asyncio
import logging
from typing import Any, Optional
import docker
from models import ContainerInfo

logger = logging.getLogger("orchestrator.docker")

# Managed container prefixes or known names in LLM Gateway stack
KNOWN_LLM_CONTAINERS = [
    "llm-gw-switchyard",
    "llm-gw-dsh",
    "llm-gw-lmstudio",
    "llm-gw-dashboard",
    "llm-gw-ollama",
    "llm-gw-vllm",
]


class DockerManager:
    """
    Docker container lifecycle and resource management for LLM backends.

    All Docker SDK calls are wrapped in ``asyncio.to_thread()`` to avoid
    blocking FastAPI's async event loop.
    """

    def __init__(self):
        try:
            self.client = docker.from_env()
        except Exception as e:
            logger.warning(f"Failed to connect to Docker socket: {e}")
            self.client = None

    async def list_llm_containers(self) -> list[ContainerInfo]:
        """Returns runtime status and memory/cpu usage for all LLM Gateway containers."""
        if not self.client:
            return []

        try:
            all_containers = await asyncio.to_thread(self.client.containers.list, all=True)
            result = []

            for c in all_containers:
                name = c.name
                # Include containers that match our stack prefix or known names
                if not (name.startswith("llm-gw-") or name in KNOWN_LLM_CONTAINERS):
                    continue

                mem_usage_mb = None
                mem_limit_mb = None
                mem_percent = None

                # If container is running, gather memory footprint
                if c.status == "running":
                    try:
                        stats = await asyncio.to_thread(c.stats, stream=False)
                        memory_stats = stats.get("memory_stats", {})
                        usage = memory_stats.get("usage", 0)
                        limit = memory_stats.get("limit", 0)
                        if limit > 0:
                            mem_usage_mb = round(usage / (1024 * 1024), 1)
                            mem_limit_mb = round(limit / (1024 * 1024), 1)
                            mem_percent = round((usage / limit) * 100, 1)
                    except Exception:
                        pass

                ports = []
                c_ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
                for container_port, host_bindings in (c_ports or {}).items():
                    if host_bindings:
                        for b in host_bindings:
                            ports.append(f"{b.get('HostPort', '')}->{container_port}")
                    else:
                        ports.append(container_port)

                image_tag = ",".join(c.image.tags) if c.image.tags else c.image.id[:12]

                result.append(
                    ContainerInfo(
                        id=c.short_id,
                        name=name,
                        image=image_tag,
                        status=c.status,
                        state=c.attrs.get("State", {}).get("Status", c.status),
                        memory_usage_mb=mem_usage_mb,
                        memory_limit_mb=mem_limit_mb,
                        memory_percent=mem_percent,
                        ports=ports,
                        created=c.attrs.get("Created", ""),
                    )
                )

            # Sort so switchyard, dsh, and lmstudio appear first
            result.sort(key=lambda x: (0 if "switchyard" in x.name else (1 if "dsh" in x.name else (2 if "lmstudio" in x.name else 3)), x.name))
            return result
        except Exception as e:
            logger.error(f"Error listing LLM containers: {e}")
            return []

    async def start_container(self, container_name: str) -> tuple[bool, str]:
        if not self.client:
            return False, "Docker client unavailable"
        try:
            container = await asyncio.to_thread(self.client.containers.get, container_name)
            if container.status == "running":
                return True, f"Container '{container_name}' is already running"
            logger.info(f"Starting container: {container_name}")
            await asyncio.to_thread(container.start)
            return True, f"Container '{container_name}' started successfully"
        except Exception as e:
            logger.error(f"Error starting container {container_name}: {e}")
            return False, str(e)

    async def stop_container(self, container_name: str) -> tuple[bool, str]:
        if not self.client:
            return False, "Docker client unavailable"
        try:
            container = await asyncio.to_thread(self.client.containers.get, container_name)
            if container.status != "running":
                return True, f"Container '{container_name}' is already stopped"
            logger.info(f"Stopping container: {container_name}")
            await asyncio.to_thread(container.stop, timeout=10)
            return True, f"Container '{container_name}' stopped successfully (resources released)"
        except Exception as e:
            logger.error(f"Error stopping container {container_name}: {e}")
            return False, str(e)

    async def restart_container(self, container_name: str) -> tuple[bool, str]:
        if not self.client:
            return False, "Docker client unavailable"
        try:
            container = await asyncio.to_thread(self.client.containers.get, container_name)
            logger.info(f"Restarting container: {container_name}")
            await asyncio.to_thread(container.restart, timeout=10)
            return True, f"Container '{container_name}' restarted successfully"
        except Exception as e:
            logger.error(f"Error restarting container {container_name}: {e}")
            return False, str(e)

    async def get_container_status(self, container_name: str) -> dict[str, Any]:
        if not self.client:
            return {"status": "unknown", "error": "Docker socket unavailable"}
        try:
            container = await asyncio.to_thread(self.client.containers.get, container_name)
            return {
                "id": container.short_id,
                "name": container.name,
                "status": container.status,
                "created": container.attrs.get("Created", ""),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}


docker_manager = DockerManager()
