"""Der Tick. Die Sekretaerin hat kein LLM; sie waehlt nur aus und startet.

Ein Tick macht vier Dinge:
  1. fertige Laeufe einsammeln (und Protokollverletzungen behandeln)
  2. abgelaufene Leases erholen
  3. unerreichbare Tasks ueberspringen (Reviews ausgenommen)
  4. bereite Items bis zur Rollen-Kapazitaet dispatchen

Es gibt bewusst KEINEN Unblock-Code: Bereitschaft ist die View ready_workitems.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from . import framing, store, usage
from .config import Config
from .harness import RolePool

log = logging.getLogger("secretary")


class Secretary:
    """Eine Instanz, kein LLM, dauert bis zum Strg-C."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.pool = RolePool(cfg)
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="workitem")
        self.inflight: dict[Future, dict[str, Any]] = {}
        self.running_per_role: dict[str, int] = {}
        self._stop = threading.Event()
        self._roles_cache: dict[str, dict[str, Any]] = {}

    # ── Rollen ─────────────────────────────────────────────────────────────
    def _role(self, name: str) -> dict[str, Any] | None:
        if name not in self._roles_cache:
            row = store.role(name)
            if row is None:
                log.error("Rolle '%s' ist nicht in der roles-Tabelle — Item wird uebersprungen", name)
                return None
            self._roles_cache[name] = row
        return self._roles_cache[name]

    # ── Dispatch ───────────────────────────────────────────────────────────
    def _execute(self, runner: Any, prompt: str, session_id: str) -> dict[str, Any]:
        """Ein Lauf. Faengt selbst, damit Dauer und Events auch im Fehlerfall
        beim Reap ankommen — der Verbrauch ist gerade dann interessant."""
        started = time.monotonic()
        try:
            result = runner.run(prompt, session_id=session_id)
            return {"events": result.events, "seconds": time.monotonic() - started, "error": None}
        except Exception as exc:  # noqa: BLE001 — wird im Reap bewertet
            return {"events": [], "seconds": time.monotonic() - started, "error": exc}

    def _dispatch(self, item: dict[str, Any], role_row: dict[str, Any]) -> None:
        claimed = store.claim(item["id"], self.cfg.owner, self.cfg.lease_seconds)
        if claimed is None:
            return  # jemand war schneller
        predecessors = store.context_predecessors(item["id"])
        prompt = framing.build(
            item, predecessors, self._entry_prompt(item["change_item_id"]), self.cfg,
            roles=store.roles_catalog(),
        )

        # Nur Planer und Reviewer behalten eine Session: dort muss der Kontext
        # ueber Runden hinweg erhalten bleiben. Worker laufen zustandslos.
        keeps_session = claimed["type"] in ("initial", "review")
        if keeps_session:
            # INITIAL, REVIEW Runde 1 und REVIEW Runde 2 teilen sich EINE Session —
            # nur so kennt der Reviewer seine eigene Planung und seinen Fehlschlag.
            session_id = store.ensure_session(item["change_item_id"], claimed["role"], str(claimed["id"]))
        else:
            # Zustandslose Worker bekommen eine Session PRO VERSUCH. Sonst zaehlt die
            # budget-policy beim Retry den Verbrauch des Vorversuchs gegen das alte
            # Budget (sie liest es aus der ersten User-Nachricht der Session), die
            # Runde bleibt leer und das Item scheitert erneut an "kein workitem_finish".
            session_id = f"{claimed['id']}-v{claimed['attempts']}"

        runner = self.pool.get(claimed["role"], role_row["patch"], role_row["model"])
        future = self.executor.submit(self._execute, runner, prompt, session_id)
        self.inflight[future] = claimed
        self.running_per_role[claimed["role"]] = self.running_per_role.get(claimed["role"], 0) + 1
        log.info("dispatch %s [%s/%s] runde=%s versuch=%s/%s session=%s%s",
                 claimed["step_key"], claimed["type"], claimed["role"],
                 claimed["round"], claimed["attempts"], claimed["max_attempts"],
                 session_id, " (geteilt)" if keeps_session else "")

    def _entry_prompt(self, change_item_id: str) -> str | None:
        ci = store.change_item(change_item_id)
        return ci["entry_prompt"] if ci else None

    # ── Reap ───────────────────────────────────────────────────────────────
    def _reap(self) -> None:
        for future in [f for f in self.inflight if f.done()]:
            item = self.inflight.pop(future)
            role_name = item["role"]
            self.running_per_role[role_name] = max(0, self.running_per_role.get(role_name, 1) - 1)

            try:
                outcome = future.result()
            except Exception as exc:  # noqa: BLE001 — Executor-Fehler
                outcome = {"events": [], "seconds": 0.0, "error": exc}
            error = outcome["error"]

            # Verbrauch IMMER zurueckschreiben — auch (gerade) bei Abbruch.
            measured = usage.fold(outcome["events"])
            store.add_usage(
                item["id"], measured["rounds"], measured["tokens"], int(outcome["seconds"]),
                measured["cacheReadTokens"], measured["outputTokens"],
            )

            current = store.load_item(item["id"])
            if current is None:
                log.warning("Item %s ist verschwunden", item["id"])
                continue

            if current["status"] != "running":
                log.info("ok %s -> %s  (%s Runden, %s Tokens, %ss)",
                         current["step_key"], current["status"],
                         measured["rounds"], measured["tokens"], int(outcome["seconds"]))
                continue

            # Der Lauf ist zurueck, das Item steht aber noch auf running:
            # der Agent hat workitem_finish nicht aufgerufen.
            reason = f"Protokollverletzung: {type(error).__name__}: {error}" if error else "kein workitem_finish"
            if current["attempts"] < current["max_attempts"]:
                log.warning("%s: %s — neuer Versuch (%s/%s)",
                            current["step_key"], reason, current["attempts"], current["max_attempts"])
                store.requeue(item["id"], reason)
            else:
                log.error("%s: %s — endgueltig failed", current["step_key"], reason)
                store.fail(item["id"], reason)

    # ── Tick ───────────────────────────────────────────────────────────────
    def tick(self) -> None:
        self._reap()

        recovered = store.recover_leases()
        if recovered:
            log.warning("%s abgelaufene Lease(s) zurueck auf pending", recovered)
        skipped = store.skip_cascade()
        if skipped:
            log.info("%s Item(s) uebersprungen", skipped)

        for done in store.finalize_change_items():
            log.info("LAUF %s: %s", done["state"].upper(), done["title"])

        for item in store.ready_items():
            role_row = self._role(item["role"])
            if role_row is None:
                # Nicht ewig pending stehen lassen: das Item scheitert sichtbar,
                # die Skip-Kaskade laeuft, das Review bewertet die Luecke.
                if store.claim(item["id"], self.cfg.owner, self.cfg.lease_seconds):
                    store.fail(item["id"], f"unbekannte Rolle '{item['role']}'")
                    log.error("Item %s: Rolle '%s' existiert nicht — als failed markiert",
                              item["step_key"], item["role"])
                continue
            capacity = int(role_row["max_concurrency"])
            if self.running_per_role.get(item["role"], 0) >= capacity:
                continue
            self._dispatch(item, role_row)

    def run_forever(self) -> None:
        # SIGTERM sauber behandeln: systemd stop soll die laufenden Auftraege
        # nicht mitten im Satz abschneiden. Was nicht mehr fertig wird, faengt
        # der Lease-Ablauf beim naechsten Start auf.
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop())

        log.info("Sekretaerin startet: tick=%.1fs lease=%ss max_rounds=%s",
                 self.cfg.tick_seconds, self.cfg.lease_seconds, self.cfg.max_rounds)
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 — ein kaputter Tick darf die Schleife nicht toeten
                    log.exception("Tick fehlgeschlagen")
                elapsed = time.monotonic() - started
                self._stop.wait(max(0.2, self.cfg.tick_seconds - elapsed))
        finally:
            self.shutdown()

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        log.info("Sekretaerin faehrt herunter — warte auf %s laufende Auftraege", len(self.inflight))
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.pool.close()
