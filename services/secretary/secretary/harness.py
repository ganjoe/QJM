"""DSH-Anbindung: pro Rolle ein langlebiger SDK-Prozess.

Ein Prozess je Rolle ist kein Umweg, sondern die Architektur: das SDK kann keine
Presets waehlen (kein Preset-Mount im sdk-Profil), Rollentrennung laeuft ueber
--patch-Overlays. Und der langlebige Prozess ist die "LLM-Ressource" aus dem
Originalprompt: er traegt mehrere Sessions parallel (in Phase 0 verifiziert).
"""
from __future__ import annotations

import os
from pathlib import Path

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig

from .config import Config


class RoleRunner:
    """Ein Rollen-Prozess. Wird beim ersten Auftrag gestartet und dann wiederverwendet."""

    def __init__(self, role_name: str, patch: str, model: str | None, cfg: Config) -> None:
        self.role_name = role_name
        self.patch_path = str(Path(cfg.roles_dir) / Path(patch).name)
        if not os.path.exists(self.patch_path):
            raise FileNotFoundError(f"Rollen-Patch fehlt: {self.patch_path}")
        self._harness = DeepSeekHarness(DeepSeekHarnessConfig(
            dsh_bin=cfg.dsh_bin,
            dsh_home=cfg.dsh_home,
            profile=cfg.profile,
            patches=(self.patch_path,),
            cwd=cfg.workspace,
            provider=cfg.provider,
            model=model or cfg.model,
            request_timeout_seconds=None,
        ))

    def run(self, prompt: str, session_id: str):
        """Blockierender Lauf. Gehoert in einen Worker-Thread."""
        return self._harness.run(prompt, session_id=session_id)

    def close(self) -> None:
        self._harness.close()


class RolePool:
    """Alle Rollen-Prozesse. Threadsicher genug: das dict wird nur im Tick beruehrt."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._runners: dict[str, RoleRunner] = {}

    def get(self, role_name: str, patch: str, model: str | None) -> RoleRunner:
        runner = self._runners.get(role_name)
        if runner is None:
            runner = RoleRunner(role_name, patch, model, self._cfg)
            self._runners[role_name] = runner
        return runner

    def close(self) -> None:
        for runner in self._runners.values():
            try:
                runner.close()
            except Exception:  # noqa: BLE001 — Shutdown darf nie werfen
                pass
        self._runners.clear()
