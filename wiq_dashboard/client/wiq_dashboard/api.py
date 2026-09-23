"""HTTP-Zugriff auf den Dashboard-Server.

Nur die Standardbibliothek: der Client braucht damit ausser PySide6 nichts.
Die Methodennamen sind dieselben wie in der frueheren Direkt-DB-Fassung, damit
die Oberflaeche unveraendert bleibt.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ApiError(RuntimeError):
    """Der Server hat abgelehnt oder ist nicht erreichbar."""


class DashboardApi:
    def __init__(self, base_url: str, timeout: float = 25.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    # ── Transport ──────────────────────────────────────────────────────────
    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None,
              query: dict[str, Any] | None = None) -> Any:
        url = self.base + path
        if query:
            clean = {k: str(v) for k, v in query.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = str(json.loads(exc.read().decode("utf-8")).get("detail", ""))
            except Exception:  # noqa: BLE001 - Antwort war kein JSON
                pass
            raise ApiError(detail or f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"Server nicht erreichbar ({self.base}): {exc.reason}") from exc
        except OSError as exc:
            raise ApiError(f"Verbindung fehlgeschlagen: {exc}") from exc
        return json.loads(body.decode("utf-8")) if body else None

    # ── Lesen ──────────────────────────────────────────────────────────────
    def fetch_runs(self, search: str = "", state: str = "", kind: str = "",
                   limit: int = 400) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        data = self._call("GET", "/api/runs", query={
            "search": search, "state": state, "kind": kind, "limit": limit})
        return list(data["roots"]), {k: list(v) for k, v in data["occurrences"].items()}

    def fetch_detail(self, change_item_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        data = self._call("GET", f"/api/detail/{change_item_id}")
        return data["change_item"], list(data["items"])

    def roles(self) -> list[dict[str, Any]]:
        """Rollen als Objekte: {name, reasoning_effort, max_concurrency, description}."""
        return list(self._call("GET", "/api/roles")["roles"])

    def health(self) -> dict[str, Any]:
        return dict(self._call("GET", "/health"))

    # ── Schreiben (nur die zwei erlaubten Wege) ────────────────────────────
    def submit(self, prompt: str, title: str | None = None,
               max_rounds: int | None = None) -> str:
        data = self._call("POST", "/api/submit", {
            "prompt": prompt, "title": title or None, "max_rounds": max_rounds})
        return str(data["change_item_id"])

    def create_schedule(self, prompt: str, role: str, rule: dict[str, Any],
                        title: str | None = None, max_rounds: int | None = None) -> str:
        data = self._call("POST", "/api/schedule", {
            "prompt": prompt, "role": role, "rule": rule, "title": title or None,
            "max_rounds": max_rounds})
        return str(data["change_item_id"])

    def cancel_schedule(self, definition_id: str) -> bool:
        self._call("POST", f"/api/schedule/{definition_id}/cancel")
        return True

    def run_schedule_now(self, definition_id: str) -> bool:
        self._call("POST", f"/api/schedule/{definition_id}/run")
        return True
