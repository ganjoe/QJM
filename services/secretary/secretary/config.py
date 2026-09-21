"""Konfiguration der Sekretaerin. Alles ueber Umgebungsvariablen, nichts im Code."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Config:
    """Laufzeitkonfiguration. Eine Instanz, keine versteckten Defaults im Code."""

    tick_seconds: float
    lease_seconds: int
    dsh_bin: str
    dsh_home: str
    profile: str
    workspace: str
    roles_dir: str
    provider: str
    model: str
    owner: str
    max_rounds: int

    @staticmethod
    def from_env() -> "Config":
        return Config(
            tick_seconds=float(_env("SECRETARY_TICK_SECONDS", "2")),
            lease_seconds=int(_env("SECRETARY_LEASE_SECONDS", "900")),
            dsh_bin=_env("SECRETARY_DSH_BIN", "/home/daniel/.local/bin/dsh"),
            dsh_home=_env("SECRETARY_DSH_HOME", "/home/daniel/.dsh"),
            profile=_env("SECRETARY_DSH_PROFILE", "sdk"),
            workspace=_env("SECRETARY_WORKSPACE", "/home/daniel/QJM"),
            roles_dir=_env("SECRETARY_ROLES_DIR", "/home/daniel/QJM/roles"),
            provider=_env("SECRETARY_PROVIDER", "deepseek-official"),
            model=_env("SECRETARY_MODEL", "deepseek-flash"),
            owner=_env("SECRETARY_OWNER", "secretary-1"),
            max_rounds=int(_env("SECRETARY_MAX_ROUNDS", "2")),
        )
