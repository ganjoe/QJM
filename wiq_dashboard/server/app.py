"""Der Dashboard-Server.

Ein schlanker Dienst auf dem QJM-Host. Er ist die einzige Stelle, die den
Service-Key kennt; der Client auf Windows/macOS spricht nur HTTP mit diesem
Server. Aufbau und Betrieb folgen dem Chart-Viewer-Muster:

  GET  /health              Zustand
  GET  /api/sync_version    Hash des Client-Codes
  GET  /api/sync            Tar des Client-Codes (Bootstrap beim Start)

  GET  /api/runs            Wurzeln + Vorkommen (Suche, Zustand, Art)
  GET  /api/detail/{id}     ein Lauf mit seinen Items
  GET  /api/roles           Rollennamen
  POST /api/submit          neuer Auftrag
  POST /api/schedule        stehende Aufgabe anlegen (Regel wird HIER geprueft)
  POST /api/schedule/{id}/cancel
  POST /api/schedule/{id}/run

Der Server schreibt nur zwei Dinge: einen neuen Auftrag und die Metadaten einer
stehenden Aufgabe. Workitem-Status setzt ausschliesslich die Sekretaerin.
"""
from __future__ import annotations

import hashlib
import io
import logging
import tarfile
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import postgrest as pg
from . import rules
from .config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("wiq-dashboard")

cfg = Config.from_env()
db = pg.Postgrest(cfg.supabase_url, cfg.service_key)
rules_loaded = rules.load(cfg.schedules_py)

app = FastAPI(title="WIQ Dashboard Server", version="1.0.0")


# ── Hilfen ─────────────────────────────────────────────────────────────────

# Die Rollen aendern sich selten, das Detail wird aber alle 5 s abgefragt:
# eine Minute Cache spart zwei PostgREST-Aufrufe pro Sekunde.
_ROLES_TTL = 60.0
_roles_cache: tuple[float, list[dict[str, Any]]] | None = None


def _roles() -> list[dict[str, Any]]:
    global _roles_cache
    now = time.monotonic()
    if _roles_cache is None or now - _roles_cache[0] > _ROLES_TTL:
        rows = db.get("roles", {
            "select": "name,description,max_concurrency,reasoning_effort",
            "order": "name.asc",
        })
        _roles_cache = (now, rows)
    return _roles_cache[1]


def _role_efforts() -> dict[str, str]:
    return {str(r["name"]): str(r.get("reasoning_effort") or "") for r in _roles()}


def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    """Regel in Worte fassen. Nur der Server kennt schedules.py — der Client
    zeigt den fertigen Satz an und hat keine eigene Terminlogik."""
    if row.get("is_template"):
        row["rule_text"] = rules.describe(row.get("schedule"))
        row["rule_short"] = rules.describe_short(row.get("schedule"))
    else:
        row["rule_text"] = ""
        row["rule_short"] = ""
    return row


def _client_files() -> list[tuple[str, Any]]:
    """Die Dateien, die den Client ausmachen. Eine Liste fuer Hash UND Tar —
    sonst koennten beide ueber verschiedene Mengen reden."""
    root = cfg.client_dir
    if not root.exists():
        raise HTTPException(500, f"Client-Verzeichnis fehlt: {root}")
    files: list[tuple[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = str(path.relative_to(root))
        if "__pycache__" in name or name.endswith(".pyc") or name.startswith("tests/"):
            continue
        files.append((name, path))
    return files


def _bundle_hash() -> str:
    """Hash ueber den INHALT, nicht ueber den Tar.

    Ein Tar traegt Zeitstempel; ein blosses `touch` wuerde sonst bei allen
    Arbeitsrechnern einen Neu-Download ausloesen.
    """
    digest = hashlib.sha256()
    for name, path in _client_files():
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _bundle_bytes() -> bytes:
    """Tar des Client-Codes. Der Client laeuft damit immer auf dem Stand des
    Servers (Muster: chart_viewer /api/sync). Zeitstempel werden auf 0 gesetzt,
    damit zwei Bauten derselben Dateien identisch sind."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, path in _client_files():
            info = tar.gettarinfo(str(path), arcname=name)
            info.mtime = 0
            with path.open("rb") as handle:
                tar.addfile(info, handle)
    return buffer.getvalue()


# ── Bootstrap ──────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "server": "wiq-dashboard",
        "version": "1.0.0",
        "rule_check": rules_loaded,
        "client_dir": str(cfg.client_dir),
    }


@app.get("/api/sync_version")
def sync_version() -> Response:
    return Response(_bundle_hash(), media_type="text/plain",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/sync")
def sync_bundle() -> Response:
    return Response(_bundle_bytes(), media_type="application/x-tar",
                    headers={"Content-Disposition": 'attachment; filename="client_bundle.tar"'})


# ── Lesen ──────────────────────────────────────────────────────────────────

@app.get("/api/runs")
def api_runs(search: str = "", state: str = "", kind: str = "",
             limit: int = Query(400, ge=1, le=2000)) -> dict[str, Any]:
    try:
        roots, occurrences = pg.fetch_runs(db, search, state, kind, limit)
    except pg.PostgrestError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        "roots": [_decorate(r) for r in roots],
        "occurrences": {k: [_decorate(v) for v in values] for k, values in occurrences.items()},
    }


@app.get("/api/detail/{change_item_id}")
def api_detail(change_item_id: str) -> dict[str, Any]:
    try:
        change_item, items = pg.fetch_detail(db, change_item_id)
    except pg.PostgrestError as exc:
        raise HTTPException(502, str(exc)) from exc
    if change_item is None:
        raise HTTPException(404, "Unbekanntes change_item")
    # Effektives Reasoning-Level je Item: eigene Ueberschreibung schlaegt die
    # Rolle. Der Client soll den Wert zeigen, der wirklich gilt.
    efforts = _role_efforts()
    for item in items:
        item["reasoning_effort"] = item.get("reasoning_effort") or efforts.get(str(item.get("role")), "")
    return {"change_item": _decorate(change_item), "items": items}


@app.get("/api/roles")
def api_roles() -> dict[str, Any]:
    try:
        return {"roles": _roles()}
    except pg.PostgrestError as exc:
        raise HTTPException(502, str(exc)) from exc


# ── Schreiben ──────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    prompt: str
    title: str | None = None
    rounds: int | None = Field(default=None, ge=1, le=200)
    tokens: int | None = Field(default=None, ge=1000)
    max_rounds: int | None = Field(default=None, ge=1, le=10)


class ScheduleRequest(BaseModel):
    prompt: str
    role: str
    rule: dict[str, Any]
    title: str | None = None
    rounds: int | None = Field(default=None, ge=1, le=200)
    tokens: int | None = Field(default=None, ge=1000)
    max_rounds: int | None = Field(default=None, ge=1, le=10)


def _budget(rounds: int | None, tokens: int | None) -> dict[str, int]:
    budget: dict[str, int] = {}
    if rounds:
        budget["rounds"] = rounds
    if tokens:
        budget["tokens"] = tokens
    return budget


@app.post("/api/submit")
def api_submit(request: SubmitRequest) -> dict[str, Any]:
    try:
        change_item_id, workitem_id = pg.submit(
            db, request.prompt, request.title, _budget(request.rounds, request.tokens),
            request.max_rounds)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except pg.PostgrestError as exc:
        raise HTTPException(502, str(exc)) from exc
    log.info("submit %s", change_item_id)
    return {"change_item_id": change_item_id, "workitem_id": workitem_id}


@app.post("/api/schedule")
def api_schedule(request: ScheduleRequest) -> dict[str, Any]:
    # Die Regel wird HIER geprueft — mit derselben Datei, die die Sekretaerin
    # benutzt. Damit bekommt der Client sofort eine Fehlermeldung statt einer
    # Definition, die beim Scharfstellen still scheitert.
    try:
        canonical = rules.parse(request.rule)
    except rules.RuleError as exc:
        raise HTTPException(400, f"Regel unbrauchbar: {exc}") from exc

    try:
        known = [str(r["name"]) for r in _roles()]
    except pg.PostgrestError as exc:
        raise HTTPException(502, str(exc)) from exc
    if request.role not in known:
        raise HTTPException(400, f"Rolle '{request.role}' gibt es nicht. Bekannt: {', '.join(known)}")

    try:
        definition_id = pg.create_schedule(
            db, request.prompt, request.role, canonical, request.title,
            _budget(request.rounds, request.tokens), request.max_rounds)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except pg.PostgrestError as exc:
        raise HTTPException(502, str(exc)) from exc
    log.info("schedule %s role=%s", definition_id, request.role)
    return {"change_item_id": definition_id,
            "rule": canonical,
            "rule_text": rules.describe(canonical)}


@app.post("/api/schedule/{definition_id}/cancel")
def api_schedule_cancel(definition_id: str) -> dict[str, Any]:
    try:
        ok = pg.cancel_schedule(db, definition_id)
    except pg.PostgrestError as exc:
        raise HTTPException(502, str(exc)) from exc
    if not ok:
        raise HTTPException(409, "Nicht mehr aktiv — nichts geaendert.")
    return {"cancelled": definition_id}


@app.post("/api/schedule/{definition_id}/run")
def api_schedule_run(definition_id: str) -> dict[str, Any]:
    try:
        ok = pg.run_schedule_now(db, definition_id)
    except pg.PostgrestError as exc:
        raise HTTPException(502, str(exc)) from exc
    if not ok:
        raise HTTPException(409, "Noch nicht scharfgestellt oder nicht aktiv.")
    return {"triggered": definition_id}
