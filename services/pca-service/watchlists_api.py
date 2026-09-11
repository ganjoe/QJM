import os
import re
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("pca.watchlists")
router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://host.docker.internal:8001")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def _headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


class WatchlistAddRequest(BaseModel):
    list_name: str
    ticker: str
    position: Optional[int] = 0


class WatchlistBatchRequest(BaseModel):
    list_name: str
    tickers: List[str]
    replace: bool = Field(default=False, description="If true, clears existing list before adding")


@router.get("/watchlists")
async def get_watchlists():
    """Return all distinct watchlist names."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/pca_watchlists",
                params={"select": "list_name", "order": "list_name.asc"},
                headers=_headers(),
                timeout=10.0,
            )
            r.raise_for_status()
        names = sorted(set(row["list_name"] for row in r.json()))
        return {"watchlists": names}
    except Exception as e:
        logger.error("Failed to fetch watchlists: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to fetch watchlists from Supabase: {str(e)}")


@router.get("/watchlists/{list_name}")
async def get_watchlist(list_name: str):
    """Return all tickers in a named watchlist, ordered by position."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/pca_watchlists",
                params={
                    "list_name": f"eq.{list_name}",
                    "order": "position.asc",
                    "select": "ticker,position,added_at",
                },
                headers=_headers(),
                timeout=10.0,
            )
            r.raise_for_status()
        return {"list_name": list_name, "tickers": r.json()}
    except Exception as e:
        logger.error("Failed to fetch watchlist %s: %s", list_name, e)
        raise HTTPException(status_code=500, detail=f"Failed to fetch watchlist: {str(e)}")


@router.post("/watchlists")
async def add_to_watchlist(body: WatchlistAddRequest):
    """Add a single ticker to a watchlist."""
    ticker_clean = body.ticker.strip().upper()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/pca_watchlists",
                json={"list_name": body.list_name, "ticker": ticker_clean, "position": body.position or 0},
                headers={**_headers(), "Prefer": "return=minimal"},
                timeout=10.0,
            )
            if r.status_code == 409:
                return {"status": "already_exists", "ticker": ticker_clean, "list_name": body.list_name}
            r.raise_for_status()
        return {"status": "added", "ticker": ticker_clean, "list_name": body.list_name}
    except Exception as e:
        logger.error("Failed to add ticker to watchlist: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to add to watchlist: {str(e)}")


@router.post("/watchlists/batch")
async def batch_add_watchlist(body: WatchlistBatchRequest):
    """Adds multiple tickers to a watchlist. Optionally replaces existing tickers."""
    tickers_clean = []
    seen = set()
    for t in body.tickers:
        normalized = t.strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            tickers_clean.append(normalized)

    if not tickers_clean:
        raise HTTPException(status_code=400, detail="No valid tickers provided")

    try:
        async with httpx.AsyncClient() as client:
            if body.replace:
                await client.delete(
                    f"{SUPABASE_URL}/rest/v1/pca_watchlists",
                    params={"list_name": f"eq.{body.list_name}"},
                    headers=_headers(),
                    timeout=10.0,
                )

            payload = [
                {"list_name": body.list_name, "ticker": t, "position": i}
                for i, t in enumerate(tickers_clean)
            ]

            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/pca_watchlists",
                json=payload,
                headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                timeout=15.0,
            )
            r.raise_for_status()

        return {"status": "saved", "list_name": body.list_name, "count": len(tickers_clean), "tickers": tickers_clean}
    except Exception as e:
        logger.error("Failed batch add to watchlist %s: %s", body.list_name, e)
        raise HTTPException(status_code=500, detail=f"Failed batch update: {str(e)}")


@router.delete("/watchlists/{list_name}/{ticker}")
async def remove_from_watchlist(list_name: str, ticker: str):
    """Remove a single ticker from a watchlist."""
    ticker_clean = ticker.strip().upper()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.delete(
                f"{SUPABASE_URL}/rest/v1/pca_watchlists",
                params={"list_name": f"eq.{list_name}", "ticker": f"eq.{ticker_clean}"},
                headers=_headers(),
                timeout=10.0,
            )
            r.raise_for_status()
        return {"status": "removed", "ticker": ticker_clean, "list_name": list_name}
    except Exception as e:
        logger.error("Failed to remove ticker from watchlist: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to delete: {str(e)}")


@router.delete("/watchlists/{list_name}")
async def delete_entire_watchlist(list_name: str):
    """Delete an entire watchlist."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.delete(
                f"{SUPABASE_URL}/rest/v1/pca_watchlists",
                params={"list_name": f"eq.{list_name}"},
                headers=_headers(),
                timeout=10.0,
            )
            r.raise_for_status()
        return {"status": "deleted", "list_name": list_name}
    except Exception as e:
        logger.error("Failed to delete watchlist %s: %s", list_name, e)
        raise HTTPException(status_code=500, detail=f"Failed to delete watchlist: {str(e)}")


# ---------------------------------------------------------------------------
# Watchlist reference resolution (used by the scanner framework)
#
# A reference may be:
#   1. a bare list name           -> "current_positions"
#   2. a PCA service link         -> "http://10.20.0.23:8794/api/watchlists/current_positions"
#   3. a Supabase PostgREST link  -> "http://127.0.0.1:8001/rest/v1/pca_watchlists?list_name=eq.current_positions&select=ticker"
#   4. any URL carrying list_name -> "...?list_name:eq.current_positions" (dashboard style)
#   5. a text file                -> "ai_stocks.txt" / "/watchlists/ai_stocks.txt"
#
# Order: Supabase (pca_watchlists) first, text file as fallback.
# ---------------------------------------------------------------------------

_LIST_NAME_QUERY_RE = re.compile(r"list_name\s*(?:=|:|%3D|%3A)\s*(?:eq\.)?([^&#/\s]+)", re.IGNORECASE)
_WATCHLIST_PATH_RE = re.compile(r"/watchlists?/([^/?#]+)", re.IGNORECASE)
_IGNORED_PATH_SEGMENTS = {"pca_watchlists", "watchlists", "rest", "v1", "api"}
_FILE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T.*)?$")


def _watchlist_file_base() -> Path:
    candidates = [os.environ.get("WATCHLIST_FILE_BASE"), "/watchlists", "/app/watchlists",
                  "/home/daniel/stock-data-node/data/watchlists"]
    for cand in candidates:
        if cand and Path(cand).is_dir():
            return Path(cand)
    return Path("/app/watchlists")


def extract_list_name(reference: str) -> Optional[str]:
    """Extracts the watchlist name from a name/link reference (no database access)."""
    m = _LIST_NAME_QUERY_RE.search(reference)
    if m:
        return unquote(m.group(1)).strip().strip("'\"")

    m = _WATCHLIST_PATH_RE.search(reference)
    if m:
        tail = unquote(m.group(1)).strip()
        if tail.lower() not in _IGNORED_PATH_SEGMENTS:
            return tail

    if reference.lower().startswith(("http://", "https://")):
        tail = unquote(Path(urlparse(reference).path).name).strip()
        if tail and not tail.endswith(".txt") and tail.lower() not in _IGNORED_PATH_SEGMENTS:
            return tail
        return None

    # Bare list name (e.g. "current_positions", "ai_stocks")
    return reference.strip() or None


def load_watchlist_from_file(reference: str) -> Optional[List[str]]:
    """Loads tickers from a watchlist text file (relative names resolve against the watchlist base dir)."""
    candidates: List[Path] = []
    if Path(reference).is_absolute():
        candidates.append(Path(reference))
    else:
        base = _watchlist_file_base()
        candidates.append(base / reference)
        if not reference.endswith(".txt"):
            candidates.append(base / f"{reference}.txt")
        candidates.append(Path(reference))

    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            text = cand.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger.error("Failed to read watchlist file %s: %s", cand, e)
            continue

        tickers: List[str] = []
        seen = set()
        for token in re.split(r"[,\s;]+", text):
            t = token.strip().strip("'\"")
            if not t or t.startswith("#") or _FILE_DATE_RE.match(t):
                continue
            t = t.upper()
            if t not in seen:
                seen.add(t)
                tickers.append(t)
        return tickers
    return None


async def fetch_watchlist_tickers(list_name: str, client: Optional[httpx.AsyncClient] = None) -> List[str]:
    """Fetches all tickers of a Supabase watchlist (pca_watchlists), ordered by position."""
    url = f"{SUPABASE_URL}/rest/v1/pca_watchlists"
    params = {"list_name": f"eq.{list_name}", "order": "position.asc", "select": "ticker"}

    async def _run(c: httpx.AsyncClient) -> List[str]:
        r = await c.get(url, params=params, headers=_headers(), timeout=10.0)
        r.raise_for_status()
        tickers: List[str] = []
        seen = set()
        for row in r.json():
            t = str(row.get("ticker", "")).strip().upper()
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)
        return tickers

    if client is not None:
        return await _run(client)
    async with httpx.AsyncClient() as own_client:
        return await _run(own_client)


async def resolve_watchlist_reference(reference: str) -> Dict[str, Any]:
    """
    Resolves a watchlist reference (bare name, PCA link, PostgREST link or text file) into tickers.

    Returns {"reference", "list_name", "source": "supabase"|"file", "tickers": [...]}.
    Raises ValueError with a user readable message when the reference cannot be resolved.
    """
    ref = (reference or "").strip()
    if not ref:
        raise ValueError("Empty watchlist reference.")

    is_url = ref.lower().startswith(("http://", "https://"))

    if not is_url and (ref.endswith(".txt") or "/" in ref):
        file_tickers = load_watchlist_from_file(ref)
        if file_tickers is None:
            raise ValueError(f"Watchlist file '{ref}' not found (base dir: {_watchlist_file_base()}).")
        return {"reference": ref, "list_name": Path(ref).stem, "source": "file", "tickers": file_tickers}

    list_name = extract_list_name(ref)
    if not list_name:
        raise ValueError(
            f"Could not extract a watchlist name from '{ref}'. Accepted forms: a bare list name "
            f"(e.g. 'current_positions'), a PCA service link ('.../api/watchlists/current_positions'), "
            f"a Supabase link containing 'list_name=eq.<name>', or a '<name>.txt' file reference."
        )

    supabase_error: Optional[str] = None
    try:
        tickers = await fetch_watchlist_tickers(list_name)
        if tickers:
            return {"reference": ref, "list_name": list_name, "source": "supabase", "tickers": tickers}
    except Exception as e:
        supabase_error = str(e)
        logger.warning("Supabase lookup for watchlist '%s' failed: %s", list_name, e)

    file_tickers = load_watchlist_from_file(list_name)
    if file_tickers:
        return {"reference": ref, "list_name": list_name, "source": "file", "tickers": file_tickers}

    if supabase_error:
        raise ValueError(
            f"Watchlist '{list_name}' could not be resolved: Supabase lookup failed ({supabase_error}) "
            f"and no file '{list_name}.txt' was found in {_watchlist_file_base()}."
        )
    if file_tickers is not None:
        return {"reference": ref, "list_name": list_name, "source": "file", "tickers": []}
    return {"reference": ref, "list_name": list_name, "source": "supabase", "tickers": []}
