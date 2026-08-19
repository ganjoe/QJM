import os
import asyncio
import logging
import docker

logger = logging.getLogger("orchestrator.docker")


class DockerManager:
    """
    Docker container lifecycle manager.

    All Docker SDK calls are wrapped in ``asyncio.to_thread()`` to avoid
    blocking the async event loop (the ``docker`` library is synchronous).
    """

    def __init__(self):
        try:
            self.client = docker.from_env()
        except Exception as e:
            logger.warning(f"Failed to connect to Docker socket: {e}")
            self.client = None

    async def restart_container(self, container_name: str) -> bool:
        if not self.client:
            logger.error("Docker client not initialized")
            return False
        try:
            container = await asyncio.to_thread(
                self.client.containers.get, container_name,
            )
            logger.info(f"Restarting container: {container_name}")
            await asyncio.to_thread(container.restart, timeout=10)
            return True
        except Exception as e:
            logger.error(f"Error restarting container {container_name}: {e}")
            return False

    async def stop_container(self, container_name: str) -> bool:
        if not self.client:
            return False
        try:
            container = await asyncio.to_thread(
                self.client.containers.get, container_name,
            )
            if container.status == "running":
                logger.info(f"Stopping container: {container_name}")
                await asyncio.to_thread(container.stop, timeout=10)
            return True
        except Exception as e:
            logger.error(f"Error stopping container {container_name}: {e}")
            return False

    async def start_container(self, container_name: str) -> bool:
        if not self.client:
            return False
        try:
            container = await asyncio.to_thread(
                self.client.containers.get, container_name,
            )
            if container.status != "running":
                logger.info(f"Starting container: {container_name}")
                await asyncio.to_thread(container.start)
            return True
        except Exception as e:
            logger.error(f"Error starting container {container_name}: {e}")
            return False

    async def get_container_status(self, container_name: str) -> dict:
        if not self.client:
            return {"status": "unknown", "error": "Docker socket unavailable"}
        try:
            container = await asyncio.to_thread(
                self.client.containers.get, container_name,
            )
            return {
                "id": container.short_id,
                "name": container.name,
                "status": container.status,
                "created": container.attrs.get("Created", ""),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def list_all_containers(self) -> list[dict]:
        """Returns a list of all running containers on the host."""
        if not self.client:
            return []
        try:
            containers = await asyncio.to_thread(self.client.containers.list)
            return [
                {
                    "id": c.short_id,
                    "name": c.name,
                    "image": ",".join(c.image.tags) if c.image.tags else c.image.id,
                    "status": c.status,
                }
                for c in containers
            ]
        except Exception as e:
            logger.error(f"Error listing containers: {e}")
            return []


docker_manager = DockerManager()
