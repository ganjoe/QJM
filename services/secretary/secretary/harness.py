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
    """Ein Rollen-Prozess. Wird beim ersten Auftrag gestartet und dann wiederverwendet.

    Der Reasoning-Level ist Teil der IDENTITAET dieses Prozesses: DSH setzt ihn
    bei der Initialisierung (nicht pro Session). Ein Prozess traegt also genau
    einen Level — deshalb der Schluessel (Rolle, Level) im RolePool.
    """

    def __init__(self, role_name: str, patch: str, model: str | None,
                 reasoning: str | None, cfg: Config) -> None:
        self.role_name = role_name
        self.reasoning = reasoning or None
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
            reasoning_effort=self.reasoning,
            request_timeout_seconds=None,
        ))

    def run(self, prompt: str, session_id: str):
        """Blockierender Lauf. Gehoert in einen Worker-Thread."""
        return self._harness.run(prompt, session_id=session_id)

    def close(self) -> None:
        self._harness.close()


class RolePool:
    """Alle Rollen-Prozesse, Schluessel (Rolle, Reasoning-Level).

    Threadsicher genug: das dict wird nur im Tick beruehrt.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._runners: dict[tuple[str, str], RoleRunner] = {}

    def get(self, role_name: str, patch: str, model: str | None,
            reasoning: str | None = None) -> RoleRunner:
        key = (role_name, reasoning or "")
        runner = self._runners.get(key)
        if runner is None:
            runner = RoleRunner(role_name, patch, model, reasoning, self._cfg)
            self._runners[key] = runner
        return runner

    def close(self) -> None:
        for runner in self._runners.values():
            try:
                runner.close()
            except Exception:  # noqa: BLE001 — Shutdown darf nie werfen
                pass
        self._runners.clear()
