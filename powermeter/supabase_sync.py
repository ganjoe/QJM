"""Tägliche Aggregation der Shelly-Messwerte nach Supabase (PostgREST)."""
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from config import (
    SUPABASE_URL, SUPABASE_STATS_TABLE, SUPABASE_SERVICE_KEY, OPENBRAIN_ENV_FILE,
)

logger = logging.getLogger("supabase_sync")

_SERVICE_KEY_NAMES = ("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SERVICE_ROLE_KEY")


def resolve_service_key() -> str:
    """Service-Role-Key aus Umgebung oder aus der openBrain-.env."""
    for name in _SERVICE_KEY_NAMES:
        val = os.getenv(name)
        if val:
            return val.strip()
    path = Path(OPENBRAIN_ENV_FILE)
    if path.exists():
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in _SERVICE_KEY_NAMES:
                    return value.strip().strip('"').strip("'")
        except Exception as e:
            logger.error(f"Konnte {path} nicht lesen: {e}")
    return ""


class SupabaseDailySync:
    def __init__(self, url: str = SUPABASE_URL, table: str = SUPABASE_STATS_TABLE,
                 service_key: Optional[str] = None):
        self.url = (url or "").rstrip("/")
        self.table = table
        self.service_key = service_key or resolve_service_key()

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.service_key)

    async def upsert_daily(self, row: Dict[str, Any]) -> bool:
        """Upsert eines Tagesdatensatzes (Konflikt auf device_handle+day)."""
        if not self.enabled:
            logger.warning("Supabase-Sync deaktiviert (URL oder Service-Key fehlt).")
            return False
        endpoint = f"{self.url}/rest/v1/{self.table}?on_conflict=device_handle,day"
        headers = {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(endpoint, headers=headers, json=[row])
            if resp.status_code in (200, 201, 204):
                logger.info(f"Supabase-Tageswerte gespeichert: {row.get('device_handle')} {row.get('day')}")
                return True
            logger.error(f"Supabase-Upsert fehlgeschlagen (HTTP {resp.status_code}): {resp.text[:300]}")
            return False
        except Exception as e:
            logger.error(f"Supabase-Upsert Fehler: {e}")
            return False


supabase_sync = SupabaseDailySync()
