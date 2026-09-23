"""Konfiguration des Dashboard-Servers. Alles ueber Umgebungsvariablen."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    supabase_url: str
    service_key: str
    client_dir: Path
    schedules_py: Path
    port: int

    @staticmethod
    def from_env() -> "Config":
        return Config(
            supabase_url=os.environ.get("SUPABASE_URL", "http://host.docker.internal:8001").rstrip("/"),
            service_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
            client_dir=Path(os.environ.get("WIQ_CLIENT_DIR", "/app/client")),
            schedules_py=Path(os.environ.get("WIQ_SCHEDULES_PY", "/app/schedules_source.py")),
            port=int(os.environ.get("PORT", "8799")),
        )
