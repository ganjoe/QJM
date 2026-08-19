import os
import httpx
import logging
import asyncio

logger = logging.getLogger("orchestrator.telemetry")

class TelemetryCollector:
    def __init__(self):
        self.vllm_url = os.getenv("VLLM_URL", "http://localhost:8100")
        self.litellm_url = os.getenv("LITELLM_URL", "http://localhost:4000")
        self.lmstudio_url = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
        self.lmstudio_web_port = os.getenv("LMSTUDIO_WEB_PORT", "3002")
        self.ollama_rx6700_url = os.getenv("OLLAMA_RX6700_URL", "http://192.168.1.100:11434")
        self.ollama_macbook_url = os.getenv("OLLAMA_MACBOOK_URL", "http://192.168.1.101:11434")
        self.ollama_local_url = os.getenv("OLLAMA_ROUTER_URL", "http://ollama:11434")
        self.timeout = float(os.getenv("BACKEND_TIMEOUT_SECONDS", "3.0"))

    async def get_vllm_status(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(f"{self.vllm_url}/health")
                is_healthy = resp.status_code == 200
            except Exception:
                is_healthy = False

            metrics = {}
            if is_healthy:
                try:
                    m_resp = await client.get(f"{self.vllm_url}/metrics")
                    if m_resp.status_code == 200:
                        metrics = self._parse_prometheus(m_resp.text)
                except Exception:
                    pass

            return {
                "id": "vllm",
                "name": "vLLM (R9700 Pro 32GB ROCm)",
                "healthy": is_healthy,
                "url": self.vllm_url,
                "type": "vllm",
                "metrics": metrics,
            }

    async def get_lmstudio_status(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(f"{self.lmstudio_url}/api/v0/models")
                healthy = resp.status_code == 200
                data = resp.json().get("data", []) if healthy else []
                active_models = [
                    m.get("id") for m in data 
                    if isinstance(m, dict) and m.get("state") == "loaded" and m.get("id")
                ]
            except Exception:
                healthy = False
                active_models = []

            return {
                "id": "lmstudio",
                "name": "LM Studio (Docker Vulkan / GUI)",
                "healthy": healthy,
                "url": self.lmstudio_url,
                "web_ui_port": self.lmstudio_web_port,
                "type": "lmstudio",
                "active_models": active_models,
            }

    async def get_ollama_status(self, id_str: str, name: str, url: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(f"{url}/api/ps")
                healthy = resp.status_code == 200
                models = resp.json().get("models", []) if healthy else []
            except Exception:
                healthy = False
                models = []

            return {
                "id": id_str,
                "name": name,
                "healthy": healthy,
                "url": url,
                "type": "ollama",
                "active_models": models,
            }

    async def get_all_backends(self) -> list:
        vllm, lmstudio, local_ollama, rx6700, macbook = await asyncio.gather(
            self.get_vllm_status(),
            self.get_lmstudio_status(),
            self.get_ollama_status("ollama-local", "Local Embeddings (Ollama)", self.ollama_local_url),
            self.get_ollama_status("rx6700", "Ollama RX 6700 XT", self.ollama_rx6700_url),
            self.get_ollama_status("macbook", "Ollama MacBook M1", self.ollama_macbook_url),
        )
        return [vllm, lmstudio, local_ollama, rx6700, macbook]

    def _parse_prometheus(self, text: str) -> dict:
        res = {}
        for line in text.splitlines():
            if line.startswith("vllm:num_requests_running"):
                res["running_requests"] = float(line.split()[-1])
            elif line.startswith("vllm:gpu_cache_usage_perc"):
                res["gpu_cache_percent"] = round(float(line.split()[-1]) * 100, 1)
            elif line.startswith("vllm:avg_generation_throughput_toks_per_s"):
                res["tokens_per_sec"] = round(float(line.split()[-1]), 1)
        return res

telemetry_collector = TelemetryCollector()
