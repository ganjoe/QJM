import os
import glob
import logging
from typing import Any, Optional
import httpx
from services.docker_client import docker_manager
from services.switchyard_config import switchyard_config_manager
from models import SystemStats, GPUStats, SwitchyardStatus

try:
    import psutil
except ImportError:
    psutil = None

logger = logging.getLogger("dashboard.telemetry")


class TelemetryCollector:
    def __init__(self):
        self.switchyard_url = os.getenv("SWITCHYARD_URL", "http://switchyard:4000")
        self.vllm_url = os.getenv("VLLM_URL", "http://host.docker.internal:8100")
        self.lmstudio_url = os.getenv("LMSTUDIO_URL", "http://host.docker.internal:1234")
        self.lmstudio_web_port = os.getenv("LMSTUDIO_WEB_PORT", "3002")
        self.ollama_url = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
        self.dsh_url = os.getenv("DSH_URL", "http://dsh:3080")
        self.vllm_container = os.getenv("DOCKER_VLLM_CONTAINER", "llm-gw-vllm")
        self.lmstudio_container = os.getenv("DOCKER_LMSTUDIO_CONTAINER", "llm-gw-lmstudio")
        self.switchyard_container = os.getenv("DOCKER_SWITCHYARD_CONTAINER", "llm-gw-switchyard")
        self.dsh_container = os.getenv("DOCKER_DSH_CONTAINER", "llm-gw-dsh")
        self.ollama_container = os.getenv("DOCKER_OLLAMA_CONTAINER", "llm-gw-ollama")
        self.timeout = httpx.Timeout(1.0, connect=0.3)

    def get_gpu_telemetry(self) -> Optional[GPUStats]:
        """Reads AMD GPU VRAM and utilization from sysfs. Picks the GPU with the most VRAM (discrete card)."""
        best_vram_total = 0
        vram_used_mb = None
        vram_total_mb = None
        vram_percent = None
        gpu_util = None
        temp_c = None
        gpu_name = "AMD Radeon GPU"

        try:
            for card_path in sorted(glob.glob("/sys/class/drm/card[0-9]/device")):
                vram_used_file = os.path.join(card_path, "mem_info_vram_used")
                vram_total_file = os.path.join(card_path, "mem_info_vram_total")
                gpu_busy_file = os.path.join(card_path, "gpu_busy_percent")

                if os.path.exists(vram_total_file):
                    try:
                        with open(vram_total_file, "r") as f:
                            total_bytes = int(f.read().strip())

                        # Always prefer the GPU with the most VRAM (discrete > iGPU)
                        if total_bytes > best_vram_total:
                            best_vram_total = total_bytes
                            vram_total_mb = round(total_bytes / (1024 * 1024), 1)

                            if os.path.exists(vram_used_file):
                                with open(vram_used_file, "r") as f:
                                    used_bytes = int(f.read().strip())
                                    vram_used_mb = round(used_bytes / (1024 * 1024), 1)

                            if vram_total_mb and vram_total_mb > 0:
                                vram_percent = round(((vram_used_mb or 0) / vram_total_mb) * 100, 1)

                            if os.path.exists(gpu_busy_file):
                                with open(gpu_busy_file, "r") as f:
                                    gpu_util = float(f.read().strip())
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Could not read GPU sysfs: {e}")

        return GPUStats(
            name=gpu_name,
            vram_used_mb=vram_used_mb,
            vram_total_mb=vram_total_mb,
            vram_percent=vram_percent,
            gpu_utilization_percent=gpu_util,
            temperature_c=temp_c,
        )

    def get_system_stats(self) -> SystemStats:
        """Collects Host CPU and Memory telemetry."""
        if psutil:
            cpu_percent = psutil.cpu_percent(interval=None)
            cpu_cores = psutil.cpu_count(logical=True) or 1
            mem = psutil.virtual_memory()
            total_gb = round(mem.total / (1024**3), 2)
            used_gb = round(mem.used / (1024**3), 2)
            mem_percent = round(mem.percent, 1)
        else:
            cpu_percent = 0.0
            cpu_cores = os.cpu_count() or 1
            total_gb = 32.0
            used_gb = 8.0
            mem_percent = 25.0

        gpu_stats = self.get_gpu_telemetry()

        return SystemStats(
            cpu_percent=cpu_percent,
            cpu_cores=cpu_cores,
            memory_total_gb=total_gb,
            memory_used_gb=used_gb,
            memory_percent=mem_percent,
            gpu=gpu_stats,
        )

    async def get_switchyard_status(self) -> SwitchyardStatus:
        """Fetches active models and routes from Switchyard."""
        is_healthy = False
        models = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(f"{self.switchyard_url}/v1/models")
                if resp.status_code == 200:
                    is_healthy = True
                    data = resp.json().get("data", [])
                    models = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
            except Exception:
                is_healthy = False

        routes = switchyard_config_manager.get_routes()
        targets = switchyard_config_manager.get_targets()

        return SwitchyardStatus(
            healthy=is_healthy,
            url=self.switchyard_url,
            active_models=models,
            routes=routes,
            targets=targets,
        )

    async def get_all_backends(self) -> list[dict[str, Any]]:
        """Returns health summaries for all registered backend providers."""
        backends = []

        # 1. Switchyard Gateway
        sw_status = await self.get_switchyard_status()
        backends.append({
            "id": "switchyard",
            "name": "NVIDIA NeMo Switchyard (Router)",
            "healthy": sw_status.healthy,
            "url": self.switchyard_url,
            "type": "router",
            "active_models": sw_status.active_models,
        })

        # 2. DeepSeek Harness
        dsh_state = await docker_manager.get_container_status(self.dsh_container)
        backends.append({
            "id": "dsh",
            "name": "DeepSeek Harness (Agent UI)",
            "healthy": dsh_state.get("status") == "running",
            "url": "http://10.20.0.23:3080",
            "type": "agent",
            "container_status": dsh_state.get("status", "stopped"),
        })

        # 3. LM Studio
        lms_state = await docker_manager.get_container_status(self.lmstudio_container)
        lms_healthy = False
        if lms_state.get("status") == "running":
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                try:
                    resp = await client.get(f"{self.lmstudio_url}/v1/models")
                    lms_healthy = resp.status_code == 200
                except Exception:
                    pass

        backends.append({
            "id": "lmstudio",
            "name": "LM Studio (GPU Inferenz)",
            "healthy": lms_healthy,
            "url": self.lmstudio_url,
            "web_ui_port": self.lmstudio_web_port,
            "type": "llm_backend",
            "container_status": lms_state.get("status", "stopped"),
        })

        # 4. Ollama (CPU Embeddings)
        ollama_state = await docker_manager.get_container_status(self.ollama_container)
        backends.append({
            "id": "ollama",
            "name": "Ollama (CPU Embeddings)",
            "healthy": ollama_state.get("status") == "running",
            "url": self.ollama_url,
            "type": "embeddings",
            "container_status": ollama_state.get("status", "stopped"),
        })

        return backends


telemetry_collector = TelemetryCollector()
