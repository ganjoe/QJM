"""
services/pca-service/scan_watchlist.py — Auto-Watchlist für Scanner-Ergebnisse.

Nach jedem Scanner-Lauf (aktuell: Universal Scanner) wird das **vollständige** Treffer-Ergebnis
in eine rollierende Supabase-Watchlist geschrieben (Default-Name ``scan_latest``). Die Liste
enthält damit immer exakt den letzten Scan-Stand und kann direkt im Chart Viewer geöffnet,
aktualisiert und gelesen werden.

Eigenschaften:
- Replace-Semantik: die Liste wird vor jedem Schreiben geleert (kein Vermischen zweier Läufe).
- Kein Ticker-Cap: es werden alle Treffer geschrieben (Batch-Insert in Chunks).
- 0 Treffer werden ebenfalls geschrieben, d.h. die Liste wird geleert (kein veralteter Stand,
  der als frisches Signal missverstanden werden könnte).
- Fehler beim Schreiben dürfen einen Scan NIE scheitern lassen: sie werden geloggt und als
  ``status="error"`` an den Aufrufer zurückgegeben.
- Sequence-Guard (``run_seq``) verhindert, dass ein älterer, langsamer Parallel-Lauf einen
  bereits geschriebenen neueren Stand überschreibt.

Metadaten (Filter, Trefferzahl, Zeitpunkt) liegen in ``pca_scan_watchlist_meta``
(Migration ``migrations/006_scan_watchlist_meta.sql``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from watchlists_api import is_master_universe

logger = logging.getLogger("pca.scan_watchlist")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://host.docker.internal:8001").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

_FALSY = {"0", "false", "no", "off"}


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in _FALSY


# --- Configuration (RULE[user_global]: No magic numbers) ---
SCAN_WATCHLIST_ENABLED = _env_flag("SCAN_WATCHLIST_ENABLED", "1")
SCAN_WATCHLIST_NAME = os.environ.get("SCAN_WATCHLIST_NAME", "scan_latest").strip()
SCAN_WATCHLIST_CHUNK_SIZE = max(1, int(os.environ.get("SCAN_WATCHLIST_CHUNK_SIZE", "500")))
SCAN_WATCHLIST_META_ENABLED = _env_flag("SCAN_WATCHLIST_META_ENABLED", "1")
SCAN_WATCHLIST_TIMEOUT_SEC = float(os.environ.get("SCAN_WATCHLIST_TIMEOUT_SEC", "15.0"))
SCAN_WATCHLIST_READ_PAGE = max(1, int(os.environ.get("SCAN_WATCHLIST_READ_PAGE", "1000")))
SCAN_WATCHLIST_SOURCE = os.environ.get("SCAN_WATCHLIST_SOURCE", "universal_scanner").strip()

# Monotonic run sequence per list name; guards against out-of-order parallel runs.
_run_seq_counter = 0
_last_applied_seq: Dict[str, int] = {}
_locks: Dict[str, asyncio.Lock] = {}


def _headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _lock_for(list_name: str) -> asyncio.Lock:
    lock = _locks.get(list_name)
    if lock is None:
        lock = asyncio.Lock()
        _locks[list_name] = lock
    return lock


def _next_run_seq() -> int:
    global _run_seq_counter
    _run_seq_counter += 1
    return _run_seq_counter


def query_signature(query_meta: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Builds a stable (hash, json) signature of the scan query for the metadata table."""
    payload = query_meta or {}
    try:
        normalized = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        normalized = str(payload)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return digest, normalized


def _normalize_tickers(tickers: Optional[Sequence[Any]]) -> List[str]:
    """Upper-cases, strips and de-duplicates tickers while preserving the scan sort order."""
    seen = set()
    normalized: List[str] = []
    for raw in tickers or []:
        ticker = str(raw or "").strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            normalized.append(ticker)
    return normalized


def _info(
    status_value: str,
    list_name: str,
    count: int,
    matched_total: int,
    response_count: int,
    truncated: bool,
    updated_at: Optional[str] = None,
    message: Optional[str] = None,
) -> Dict[str, Any]:
    """Uniform result block returned to the caller (and embedded in the scan response)."""
    return {
        "list_name": list_name,
        "status": status_value,
        "count": count,
        "matched_total": matched_total,
        "response_count": response_count,
        "truncated": truncated,
        "updated_at": updated_at,
        "message": message,
    }


async def _delete_list(client: httpx.AsyncClient, list_name: str) -> None:
    response = await client.delete(
        f"{SUPABASE_URL}/rest/v1/pca_watchlists",
        params={"list_name": f"eq.{list_name}"},
        headers=_headers(),
        timeout=SCAN_WATCHLIST_TIMEOUT_SEC,
    )
    response.raise_for_status()


async def _insert_rows(client: httpx.AsyncClient, rows: List[Dict[str, Any]]) -> None:
    """Inserts rows in chunks; retries once so a transient PostgREST hiccup does not abort the sync."""
    for start in range(0, len(rows), SCAN_WATCHLIST_CHUNK_SIZE):
        chunk = rows[start:start + SCAN_WATCHLIST_CHUNK_SIZE]
        last_error: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                response = await client.post(
                    f"{SUPABASE_URL}/rest/v1/pca_watchlists",
                    json=chunk,
                    headers={**_headers(), "Prefer": "return=minimal"},
                    timeout=SCAN_WATCHLIST_TIMEOUT_SEC,
                )
                response.raise_for_status()
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - retried, reported by the caller
                last_error = exc
                if attempt == 1:
                    logger.warning("Watchlist insert chunk %d..%d failed, retrying: %s",
                                   start, start + len(chunk) - 1, exc)
        if last_error is not None:
            raise last_error


async def _upsert_meta(client: httpx.AsyncClient, meta: Dict[str, Any]) -> None:
    response = await client.post(
        f"{SUPABASE_URL}/rest/v1/pca_scan_watchlist_meta",
        json=[meta],
        params={"on_conflict": "list_name"},
        headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        timeout=SCAN_WATCHLIST_TIMEOUT_SEC,
    )
    response.raise_for_status()


async def sync_scan_watchlist(
    tickers: Optional[Sequence[Any]],
    *,
    scanner: str = SCAN_WATCHLIST_SOURCE,
    query_meta: Optional[Dict[str, Any]] = None,
    evaluated_count: int = 0,
    response_count: int = 0,
    list_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Replaces the rolling scan watchlist with the full result set of the current scan.

    Never raises: every failure is reported through the returned ``status`` field so a scan
    result stays usable even when Supabase is unreachable.
    """
    target = (list_name or SCAN_WATCHLIST_NAME).strip()

    if not SCAN_WATCHLIST_ENABLED:
        return _info("disabled", target, 0, 0, response_count, False,
                     message="Auto-Watchlist ist deaktiviert (SCAN_WATCHLIST_ENABLED=0).")

    if not target or is_master_universe(target):
        logger.error("Refusing to write scan results into protected watchlist '%s'.", target)
        return _info("blocked", target, 0, 0, response_count, False,
                     message=f"Watchlist '{target}' ist geschützt und wird nicht beschrieben.")

    normalized = _normalize_tickers(tickers)
    matched_total = len(normalized)
    truncated = response_count < matched_total
    seq = _next_run_seq()

    lock = _lock_for(target)
    async with lock:
        # A newer run already wrote this list while we were waiting for the lock -> do not
        # overwrite the fresher state with an older result.
        if seq < _last_applied_seq.get(target, 0):
            logger.info("Scan run #%d superseded by run #%d for '%s' - skipping write.",
                        seq, _last_applied_seq.get(target, 0), target)
            return _info("superseded", target, matched_total, matched_total, response_count, truncated,
                         message="Ein neuerer Scan-Lauf hat die Watchlist bereits aktualisiert.")

        query_hash, query_json = query_signature({
            **(query_meta or {}),
            "scanner": scanner,
            "list_name": target,
        })
        updated_at = datetime.now(timezone.utc).isoformat()

        try:
            async with httpx.AsyncClient() as client:
                # Replace semantics: clear first so two consecutive runs can never mix.
                await _delete_list(client, target)

                if normalized:
                    rows = [
                        {"list_name": target, "ticker": ticker, "position": position}
                        for position, ticker in enumerate(normalized)
                    ]
                    await _insert_rows(client, rows)

                _last_applied_seq[target] = seq
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            logger.error("Failed to sync scan watchlist '%s': %s", target, exc)
            return _info("error", target, 0, matched_total, response_count, truncated,
                         message=f"Watchlist-Sync fehlgeschlagen: {exc}")

        meta_warning: Optional[str] = None
        if SCAN_WATCHLIST_META_ENABLED:
            try:
                async with httpx.AsyncClient() as client:
                    await _upsert_meta(client, {
                        "list_name": target,
                        "scanner": scanner,
                        "query_hash": query_hash,
                        "query_json": json.loads(query_json),
                        "matched_count": matched_total,
                        "evaluated_count": int(evaluated_count or 0),
                        "response_count": int(response_count or 0),
                        "truncated": truncated,
                        "run_seq": seq,
                        "source": scanner,
                        "updated_at": updated_at,
                    })
            except Exception as exc:  # noqa: BLE001 - meta is secondary to the ticker list
                logger.warning("Failed to upsert scan watchlist meta for '%s': %s", target, exc)
                meta_warning = f"Metadaten konnten nicht geschrieben werden: {exc}"

    status_value = "updated" if matched_total else "cleared"
    message = (f"{matched_total} Ticker in '{target}' geschrieben."
               if matched_total else
               f"Kein Treffer - '{target}' wurde geleert.")
    if truncated:
        message += (f" Die API-Response zeigt nur {response_count} Treffer (limit); "
                    f"die Watchlist enthält alle {matched_total}.")
    if meta_warning:
        message += f" {meta_warning}"

    logger.info("Scan watchlist '%s': %s (evaluated=%d, response=%d).",
                target, status_value, evaluated_count, response_count)

    return _info(status_value, target, matched_total, matched_total, response_count, truncated,
                 updated_at=updated_at, message=message)


async def _fetch_list_rows(client: httpx.AsyncClient, list_name: str) -> List[Dict[str, Any]]:
    """Reads every row of a watchlist with explicit pagination.

    A single PostgREST request can be capped (``db-max-rows``), which would silently truncate a
    large scan result (an unfiltered universe scan holds 5,500+ tickers), so pages are requested
    until a short page arrives.
    """
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        response = await client.get(
            f"{SUPABASE_URL}/rest/v1/pca_watchlists",
            params={
                "list_name": f"eq.{list_name}",
                "order": "position.asc",
                "select": "ticker,position,added_at",
                "limit": str(SCAN_WATCHLIST_READ_PAGE),
                "offset": str(offset),
            },
            headers=_headers(),
            timeout=SCAN_WATCHLIST_TIMEOUT_SEC,
        )
        response.raise_for_status()
        page = response.json()
        rows.extend(page)
        if len(page) < SCAN_WATCHLIST_READ_PAGE:
            return rows
        offset += len(page)


async def fetch_scan_watchlist(list_name: Optional[str] = None, include_tickers: bool = True) -> Dict[str, Any]:
    """Reads the rolling scan watchlist (tickers + metadata) for the agent."""
    target = (list_name or SCAN_WATCHLIST_NAME).strip()
    result: Dict[str, Any] = {
        "list_name": target,
        "count": 0,
        "tickers": [],
        "meta": None,
        "warning": None,
    }

    try:
        async with httpx.AsyncClient() as client:
            rows = await _fetch_list_rows(client, target)
            result["count"] = len(rows)
            if include_tickers:
                result["tickers"] = [str(row.get("ticker")) for row in rows]
            else:
                result["tickers"] = []
                result["preview"] = [str(row.get("ticker")) for row in rows[:50]]
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        logger.error("Failed to read scan watchlist '%s': %s", target, exc)
        result["warning"] = f"Watchlist konnte nicht gelesen werden: {exc}"
        return result

    if SCAN_WATCHLIST_META_ENABLED:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{SUPABASE_URL}/rest/v1/pca_scan_watchlist_meta",
                    params={"list_name": f"eq.{target}", "select": "*", "limit": "1"},
                    headers=_headers(),
                    timeout=SCAN_WATCHLIST_TIMEOUT_SEC,
                )
                response.raise_for_status()
                rows = response.json()
                result["meta"] = rows[0] if rows else None
        except Exception as exc:  # noqa: BLE001 - meta is optional
            logger.warning("Failed to read scan watchlist meta for '%s': %s", target, exc)
            result["warning"] = (result["warning"] or "") + \
                f" Metadaten nicht verfügbar (Migration 006 angewendet?): {exc}"

    return result
