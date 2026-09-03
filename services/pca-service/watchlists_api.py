import os
import logging
from typing import List, Optional
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
