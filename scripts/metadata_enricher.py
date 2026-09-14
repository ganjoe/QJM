"""
metadata_enricher.py — Ticker Metadata Enrichment & Reconciler for stock-data-node / QJM.

Provides automatic retrieval and database updates for stock fundamental metadata
(shares_outstanding, currency, eps, revenue, earnings) using yfinance.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

try:
    import yfinance as yf
except ImportError:
    yf = None

logger = logging.getLogger("metadata_enricher")

# Bekannte Ticker-Mappings für Yahoo Finance
STATIC_YF_MAPPINGS: Dict[str, str] = {
    "LPK": "LPK.DE",
    "HPS.A": "HPS-A.TO",
    "SOI": "SOI.PA",
    "XFAB": "XFAB.PA",
    "SIVE": "SIVE.ST",
    "4GLD": "4GLD.DE",
    "SYSTEM": "SKIP",
    "NOD": "NOD.OL",
    "KCLI.CN": "KCLI.CN",
    "VLX.L": "VLX.L",
    "VLXGF": "VLXGF",
    "AMD": "AMD",
    "BRK.A": "BRK-A",
    "BRK.B": "BRK-B",
    "BF.B": "BF-B",
}

SKIP_PREFIXES = ("$", "=", "^")


def get_config_val(env_key: str, default: Any, cast_type=str) -> Any:
    """Holt Konfigurationswert aus ENV mit Typkonvertierung."""
    val = os.environ.get(env_key)
    if val is None or val == "":
        return default
    try:
        return cast_type(val)
    except (ValueError, TypeError):
        return default


def normalize_supabase_url(url: str) -> str:
    """Normalisiert Supabase URL für Container- und Host-Umgebungen."""
    if not url:
        return "http://host.docker.internal:8001"
    return url


def normalize_ticker_for_yf(ticker: str) -> Optional[str]:
    """Mappt interne Ticker-Symbole auf das Yahoo Finance Format."""
    t = ticker.strip().upper()
    if not t or any(t.startswith(p) for p in SKIP_PREFIXES):
        return None

    if t in STATIC_YF_MAPPINGS:
        mapped = STATIC_YF_MAPPINGS[t]
        return None if mapped == "SKIP" else mapped

    parts = t.split(".")
    if len(parts) == 2:
        suffix = parts[1]
        if suffix in {"A", "B", "C", "WS", "U"}:
            return f"{parts[0]}-{suffix}"
    return t


def fetch_ticker_metadata(ticker: str, timeout_sec: Optional[float] = None) -> Tuple[Optional[Dict[str, Any]], bool]:
    """
    Fragt Metadaten über yfinance ab.
    Rückgabe: (payload_dict, should_retry_bool)
    """
    if yf is None:
        logger.error("yfinance ist nicht installiert.")
        return None, False

    timeout = timeout_sec or get_config_val("METADATA_TIMEOUT_SEC", 8.0, float)
    yf_symbol = normalize_ticker_for_yf(ticker)
    if not yf_symbol:
        return None, False

    try:
        t = yf.Ticker(yf_symbol)

        # 1. Schneller Pfad: fast_info
        shares = None
        currency = None
        try:
            fi = t.fast_info
            shares = getattr(fi, "shares", None)
            currency = getattr(fi, "currency", None)
        except Exception as fi_err:
            err_str = str(fi_err).lower()
            if "not found" in err_str or "404" in err_str or "delisted" in err_str:
                return None, False

        # 2. Fundamentaldaten aus info
        eps = None
        revenue = None
        earnings_iso = None

        try:
            info = t.info or {}
            if shares is None:
                shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
            if currency is None:
                currency = info.get("currency")

            eps = info.get("trailingEps") or info.get("forwardEps")
            revenue = info.get("totalRevenue")

            earnings_ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
            if earnings_ts and isinstance(earnings_ts, (int, float)):
                earnings_iso = datetime.fromtimestamp(earnings_ts, tz=timezone.utc).isoformat()
        except Exception as info_err:
            err_str = str(info_err).lower()
            if "not found" in err_str or "404" in err_str:
                if shares is None and currency is None:
                    return None, False

        if shares is None and currency is None and eps is None and revenue is None:
            return None, False

        # Einheitliche Key-Struktur für PostgREST
        payload: Dict[str, Any] = {
            "ticker": ticker,
            "shares_outstanding": float(shares) if shares is not None else None,
            "currency": str(currency).strip().upper() if currency is not None else None,
            "eps": float(eps) if eps is not None else None,
            "revenue": float(revenue) if revenue is not None else None,
            "earnings": earnings_iso if earnings_iso else None,
            "has_parquet": True,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        return payload, False

    except Exception as e:
        err_msg = str(e).lower()
        if "404" in err_msg or "not found" in err_msg or "delisted" in err_msg:
            return None, False
        logger.debug("Fehler beim Metadaten-Abruf von %s (%s): %s", ticker, yf_symbol, e)
        return None, True


def upsert_metadata_to_supabase(
    rows: List[Dict[str, Any]],
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None,
    timeout_sec: float = 15.0,
) -> bool:
    """Führt Bulk-Upsert in cda_master_universe aus."""
    if not rows:
        return True

    sb_url = normalize_supabase_url(supabase_url or os.environ.get("SUPABASE_URL", "http://host.docker.internal:8001")).rstrip("/")
    sb_key = supabase_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not sb_key:
        logger.warning("Kein SUPABASE_SERVICE_ROLE_KEY konfiguriert. Überspringe Metadaten-Upsert.")
        return False

    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

    endpoint = f"{sb_url}/rest/v1/cda_master_universe"
    data_bytes = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if resp.status in (200, 201, 204):
                return True
            logger.error("Upsert in cda_master_universe fehlgeschlagen: Status %d", resp.status)
            return False
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
        logger.error("HTTP-Fehler beim Upsert in cda_master_universe (%d): %s", e.code, err_body)
        return False
    except Exception as e:
        logger.error("Fehler beim Upsert in cda_master_universe: %s", e)
        return False


def enrich_tickers_batch(
    tickers: List[str],
    timeout_sec: Optional[float] = None,
    delay_sec: Optional[float] = None,
    max_retries: Optional[int] = None,
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Reichert eine Liste von Tickern an und speichert sie in cda_master_universe.
    Gibt die Liste der erfolgreich angereicherten Payloads zurück.
    """
    effective_timeout = timeout_sec or get_config_val("METADATA_TIMEOUT_SEC", 8.0, float)
    effective_delay = delay_sec if delay_sec is not None else get_config_val("METADATA_REQUEST_DELAY_SEC", 0.35, float)
    effective_retries = max_retries or get_config_val("METADATA_MAX_RETRIES", 3, int)
    batch_size = get_config_val("METADATA_BATCH_SIZE", 50, int)

    clean_tickers = [t.strip().upper() for t in tickers if t.strip() and not any(t.strip().upper().startswith(p) for p in SKIP_PREFIXES)]
    if not clean_tickers:
        return []

    logger.info("Starte Metadaten-Anreicherung für %d Ticker...", len(clean_tickers))
    enriched_results: List[Dict[str, Any]] = []
    current_batch: List[Dict[str, Any]] = []

    for idx, ticker in enumerate(clean_tickers, start=1):
        res = None
        for attempt in range(1, effective_retries + 1):
            res, should_retry = fetch_ticker_metadata(ticker, timeout_sec=effective_timeout)
            if res or not should_retry:
                break
            if attempt < effective_retries:
                time.sleep(effective_delay * attempt)

        if res:
            logger.info("✅ [%d/%d] Metadaten ermittelt für %s (Shares: %s, Currency: %s)",
                        idx, len(clean_tickers), ticker, res.get("shares_outstanding"), res.get("currency"))
            current_batch.append(res)
            enriched_results.append(res)
        else:
            logger.warning("⚠️ [%d/%d] Keine Metadaten gefunden für %s", idx, len(clean_tickers), ticker)
            current_batch.append({
                "ticker": ticker,
                "shares_outstanding": None,
                "currency": None,
                "eps": None,
                "revenue": None,
                "earnings": None,
                "has_parquet": True,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })

        if len(current_batch) >= batch_size:
            upsert_metadata_to_supabase(current_batch, supabase_url=supabase_url, supabase_key=supabase_key)
            current_batch = []

        if effective_delay > 0:
            time.sleep(effective_delay)

    if current_batch:
        upsert_metadata_to_supabase(current_batch, supabase_url=supabase_url, supabase_key=supabase_key)

    logger.info("Metadaten-Anreicherung abgeschlossen. %d/%d Ticker erfolgreich angereichert.",
                len(enriched_results), len(clean_tickers))
    return enriched_results


def reconcile_missing_metadata(
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None,
    limit: Optional[int] = None,
) -> int:
    """
    Sucht in cda_master_universe nach allen Tickern mit shares_outstanding IS NULL
    und reichert sie automatisch an (Self-Healing Safety Net).
    """
    sb_url = normalize_supabase_url(supabase_url or os.environ.get("SUPABASE_URL", "http://host.docker.internal:8001")).rstrip("/")
    sb_key = supabase_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not sb_key:
        return 0

    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
    }

    endpoint = f"{sb_url}/rest/v1/cda_master_universe"
    params = ["shares_outstanding=is.null", "has_parquet=eq.true", "select=ticker", "order=ticker.asc"]
    if limit and limit > 0:
        params.append(f"limit={limit}")

    query_url = f"{endpoint}?{'&'.join(params)}"
    req = urllib.request.Request(query_url, headers=headers, method="GET")

    missing_tickers: List[str] = []
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                for row in data:
                    t = str(row.get("ticker", "")).strip().upper()
                    if t and not any(t.startswith(p) for p in SKIP_PREFIXES):
                        missing_tickers.append(t)
    except Exception as e:
        logger.error("Fehler beim Abrufen fehlender Ticker für Reconcile: %s", e)
        return 0

    if not missing_tickers:
        logger.debug("Reconciler: Keine unbefüllten Ticker gefunden.")
        return 0

    logger.info("Reconciler aktiv: %d Ticker ohne Shares Outstanding gefunden. Starte Anreicherung...", len(missing_tickers))
    results = enrich_tickers_batch(missing_tickers, supabase_url=sb_url, supabase_key=sb_key)
    return len(results)
