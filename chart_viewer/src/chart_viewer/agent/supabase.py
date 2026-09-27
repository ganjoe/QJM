"""Supabase/PostgREST access helpers for the chart agent.

The agent (running in `qjm-chart-viewer-server`) is the only component that talks
to the database; the desktop viewer never sees these credentials. These helpers
were extracted from `agent_client` so the control service (viewer control panel)
can reuse them without creating an import cycle.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Configurable timeout (via environment variables)
CV_SUPABASE_TIMEOUT_SEC = float(os.environ.get("CV_SUPABASE_TIMEOUT_SEC", "10.0"))

# Supabase configuration (same as agent-pca shared.ts)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://host.docker.internal:8001")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_REST_URL = f"{SUPABASE_URL}/rest/v1"
SUPABASE_HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


class SupabaseError(RuntimeError):
    """Backend (Supabase/PostgREST) unreachable or answered with an HTTP error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def extract_url_error_msg(e: urllib.error.URLError) -> str:
    """Extract informative error text from URLError / HTTPError."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode(errors="replace")
            return f"HTTP {e.code}: {e.reason} - {body}"
        except Exception:
            return f"HTTP {e.code}: {e.reason}"
    return str(e)


def _timeout(timeout: float | None) -> float:
    return CV_SUPABASE_TIMEOUT_SEC if timeout is None else timeout


def supabase_get(path: str, *, timeout: float | None = None, headers: dict | None = None) -> Any:
    """GET from the Supabase REST API."""
    url = f"{SUPABASE_REST_URL}{path}"
    req = urllib.request.Request(url, headers={**SUPABASE_HEADERS, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=_timeout(timeout)) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        msg = extract_url_error_msg(e)
        logger.error("Supabase GET %s failed: %s", url, msg)
        status = getattr(e, "code", None)
        raise SupabaseError(f"Supabase unreachable: {msg}", status=status) from e


def supabase_post(
    path: str,
    payload: Any,
    *,
    timeout: float | None = None,
    headers: dict | None = None,
) -> Any:
    """POST to the Supabase REST API."""
    url = f"{SUPABASE_REST_URL}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={**SUPABASE_HEADERS, **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_timeout(timeout)) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else None
    except urllib.error.URLError as e:
        msg = extract_url_error_msg(e)
        logger.error("Supabase POST %s failed: %s", url, msg)
        raise SupabaseError(f"Supabase unreachable: {msg}", status=getattr(e, "code", None)) from e


def supabase_patch(
    path: str,
    payload: Any,
    *,
    timeout: float | None = None,
    headers: dict | None = None,
) -> Any:
    """PATCH to the Supabase REST API."""
    url = f"{SUPABASE_REST_URL}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={**SUPABASE_HEADERS, **(headers or {})}, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=_timeout(timeout)) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else None
    except urllib.error.URLError as e:
        msg = extract_url_error_msg(e)
        logger.error("Supabase PATCH %s failed: %s", url, msg)
        raise SupabaseError(f"Supabase unreachable: {msg}", status=getattr(e, "code", None)) from e


def supabase_delete(
    path: str,
    *,
    timeout: float | None = None,
    headers: dict | None = None,
) -> None:
    """DELETE from the Supabase REST API."""
    url = f"{SUPABASE_REST_URL}{path}"
    req = urllib.request.Request(url, headers={**SUPABASE_HEADERS, **(headers or {})}, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=_timeout(timeout)):
            pass
    except urllib.error.URLError as e:
        msg = extract_url_error_msg(e)
        logger.error("Supabase DELETE %s failed: %s", url, msg)
        raise SupabaseError(f"Supabase unreachable: {msg}", status=getattr(e, "code", None)) from e


def supabase_count(path: str, *, timeout: float | None = None) -> int:
    """Return the exact row count of a PostgREST query (Prefer: count=exact).

    Only a single row is transferred; the total comes from the Content-Range
    response header (e.g. `0-0/41723`).
    """
    url = f"{SUPABASE_REST_URL}{path}"
    headers = {**SUPABASE_HEADERS, "Prefer": "count=exact", "Range": "0-0"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_timeout(timeout)) as resp:
            content_range = resp.headers.get("Content-Range", "") or ""
    except urllib.error.URLError as e:
        msg = extract_url_error_msg(e)
        logger.error("Supabase COUNT %s failed: %s", url, msg)
        raise SupabaseError(f"Supabase unreachable: {msg}", status=getattr(e, "code", None)) from e

    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    return 0


# Backwards-compatible private aliases (older call sites used the underscore names)
_extract_url_error_msg = extract_url_error_msg
_supabase_get = supabase_get
_supabase_post = supabase_post
_supabase_patch = supabase_patch
_supabase_delete = supabase_delete
_supabase_count = supabase_count
