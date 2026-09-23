"""Zugriff auf die Datenbank — ueber PostgREST.

Warum nicht psycopg: der Server laeuft als Container und hat kein
Datenbank-Passwort. PostgREST laeuft ohnehin und ist der Weg, den alle
MCP-Server in diesem Repo benutzen. Damit steht das Passwort weiterhin in
KEINER Datei.

Geschrieben wird nur, was das Dashboard darf: ein neuer Auftrag und die
Metadaten einer stehenden Aufgabe. Statusuebergaenge an Workitems macht
ausschliesslich die Sekretaerin.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("wiq-dashboard")

# PostgREST-Filterwerte duerfen diese Zeichen nicht enthalten: sie strukturieren
# den Ausdruck. Eine Suche nach "a,b" wuerde sonst den Filter zerlegen.
_FILTER_BREAKERS = [",", "(", ")", "*", "%", chr(92), '"', "'"]
_UNSAFE = str.maketrans({c: " " for c in _FILTER_BREAKERS})

STATES = ("running", "scheduled", "done", "failed", "cancelled", "silent")


class PostgrestError(RuntimeError):
    """Fehler von PostgREST, mit lesbarem Text."""


class Postgrest:
    def __init__(self, base_url: str, service_key: str, timeout: float = 20.0) -> None:
        self.base = base_url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": service_key,
            "Authorization": "Bearer " + service_key,
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _check(self, response: httpx.Response) -> Any:
        if response.status_code >= 300:
            raise PostgrestError(f"HTTP {response.status_code}: {response.text[:400]}")
        if not response.content:
            return None
        return response.json()

    def get(self, table: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        response = self._client.get(f"{self.base}/{table}", params=params, headers=self.headers)
        return list(self._check(response) or [])

    def insert(self, table: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        headers = dict(self.headers)
        headers["Prefer"] = "return=representation"
        response = self._client.post(f"{self.base}/{table}", json=body, headers=headers)
        return list(self._check(response) or [])

    def update(self, table: str, params: dict[str, Any], body: dict[str, Any]) -> list[dict[str, Any]]:
        headers = dict(self.headers)
        headers["Prefer"] = "return=representation"
        response = self._client.patch(f"{self.base}/{table}", params=params, json=body, headers=headers)
        return list(self._check(response) or [])

    def delete(self, table: str, params: dict[str, Any]) -> None:
        headers = dict(self.headers)
        headers["Prefer"] = "return=minimal"
        response = self._client.delete(f"{self.base}/{table}", params=params, headers=headers)
        self._check(response)


# ── Lesen ──────────────────────────────────────────────────────────────────

def fetch_runs(db: Postgrest, search: str = "", state: str = "", kind: str = "",
               limit: int = 400) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Wurzeln (ad-hoc-Laeufe und stehende Aufgaben) plus ihre Vorkommen."""
    params: dict[str, Any] = {
        "select": "*",
        "order": "created_at.desc",
        "limit": str(limit),
    }
    if search.strip():
        needle = search.strip().translate(_UNSAFE)
        params["or"] = f"(title.ilike.*{needle}*,entry_prompt.ilike.*{needle}*)"
    if state:
        if state not in STATES:
            raise PostgrestError(f"unbekannter Zustand {state!r}")
        params["state"] = "eq." + state
    if kind == "stehend":
        params["is_template"] = "is.true"
    elif kind == "adhoc":
        params["is_template"] = "is.false"

    roots = db.get("wiq_runs", {**params, "source_change_item_id": "is.null"})
    ids = [str(r["id"]) for r in roots if r.get("is_template")]
    occurrences: dict[str, list[dict[str, Any]]] = {}
    if ids:
        rows = db.get("wiq_runs", {
            "select": "*",
            "order": "created_at.desc",
            "source_change_item_id": "in.(" + ",".join(ids) + ")",
        })
        for row in rows:
            occurrences.setdefault(str(row["source_change_item_id"]), []).append(row)
    return roots, occurrences


def fetch_detail(db: Postgrest, change_item_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    rows = db.get("wiq_runs", {"select": "*", "id": "eq." + change_item_id, "limit": "1"})
    if not rows:
        return None, []
    items = db.get("workitems", {
        "select": ("id,step_key,type,role,status,round,attempts,max_attempts,budget,usage,"
                   "payload,result,created_at,started_at,finished_at"),
        "change_item_id": "eq." + change_item_id,
        "order": "created_at.asc,step_key.asc",
    })
    return rows[0], items


def fetch_roles(db: Postgrest) -> list[str]:
    return [str(r["name"]) for r in db.get("roles", {"select": "name", "order": "name.asc"})]


# ── Schreiben (nur die zwei erlaubten Wege) ────────────────────────────────

def submit(db: Postgrest, prompt: str, title: str | None, budget: dict[str, Any],
           max_rounds: int | None = None) -> tuple[str, str]:
    """Neuer Auftrag: change_item + initial-Workitem.

    Zwei Inserts, weil dieses PostgREST keine verschachtelten Inserts kann.
    Faellt der zweite aus, wird der erste zurueckgenommen — ein change_item
    ohne Wurzel-Item waere ein Lauf, den niemand abarbeiten kann (dieselbe
    Regel wie in run_submit).
    """
    clean = prompt.strip()
    if not clean:
        raise ValueError("Der Auftrag ist leer.")
    created = db.insert("change_items", {
        "title": (title or clean.splitlines()[0])[:120],
        "entry_prompt": clean,
        # max_rounds ist NOT NULL mit Default 2 — nur setzen, wenn gewaehlt.
        **({"max_rounds": max_rounds} if max_rounds else {}),
    })
    change_item_id = str(created[0]["id"])

    try:
        item = db.insert("workitems", {
            "change_item_id": change_item_id,
            "step_key": "initial",
            "type": "initial",
            "role": "lead_engineer",
            "priority": 100,
            "payload": {"prompt": clean},
            "budget": budget or {},
        })
    except Exception:
        db.delete("change_items", {"id": "eq." + change_item_id})
        raise
    return change_item_id, str(item[0]["id"])


def create_schedule(db: Postgrest, prompt: str, role: str, rule: dict[str, Any],
                    title: str | None, budget: dict[str, Any],
                    max_rounds: int | None = None) -> str:
    """Stehende Aufgabe: Definition + Vorlagen-Workitem."""
    clean = prompt.strip()
    if not clean:
        raise ValueError("Der Auftrag ist leer.")
    created = db.insert("change_items", {
        "title": (title or clean.splitlines()[0])[:120],
        "entry_prompt": clean,
        "state": "scheduled",
        "is_template": True,
        "schedule": rule,
        **({"max_rounds": max_rounds} if max_rounds else {}),
    })
    definition_id = str(created[0]["id"])
    try:
        db.insert("workitems", {
            "change_item_id": definition_id,
            "step_key": "aufgabe",
            "type": "template",
            "role": role,
            "payload": {"prompt": clean},
            "priority": 0,
            "max_attempts": 2,
            "budget": budget or {},
        })
    except Exception:
        db.delete("change_items", {"id": "eq." + definition_id})
        raise
    return definition_id


def cancel_schedule(db: Postgrest, definition_id: str) -> bool:
    rows = db.update(
        "change_items",
        {"id": "eq." + definition_id, "state": "eq.scheduled", "is_template": "is.true"},
        {"state": "cancelled", "next_run_at": None, "finished_at": _now_iso()},
    )
    return bool(rows)


def run_schedule_now(db: Postgrest, definition_id: str) -> bool:
    rows = db.update(
        "change_items",
        {"id": "eq." + definition_id, "state": "eq.scheduled", "is_template": "is.true",
         "next_run_at": "not.is.null"},
        {"next_run_at": _now_iso()},
    )
    return bool(rows)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
