#!/usr/bin/env python3
"""
scripts/update_ticker_metadata.py — Aktien-Metadaten Updater für Supabase (cda_master_universe)

Befüllt fundamentale Stammdaten (shares_outstanding, currency, eps, revenue, earnings)
für Aktien in der Supabase-Tabelle `cda_master_universe`.

Features:
- Konfigurierbare Timeouts, Delays, Batch-Größen und Limits via CLI und Umgebungsvariablen.
- Resumable: Standardmäßig werden nur Ticker aktualisiert, bei denen `shares_outstanding` fehlt.
- Schnelle Abfrage via yfinance (fast_info + Fallback auf info).
- Robuster PostgREST Bulk-Upsert mit 'Prefer: resolution=merge-duplicates'.
- Graceful Shutdown: Fängt SIGINT/SIGTERM ab und speichert den aktuellen Batch.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

try:
    import yfinance as yf
except ImportError:
    print("FEHLER: 'yfinance' ist nicht installiert. Bitte im venv ausführen (z.B. /home/daniel/stock-data-node/.venv/bin/python3).", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("metadata_updater")

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

# Synthetische Ticker-Präfixe, die übersprungen werden
SKIP_PREFIXES = ("$", "=", "^")


def find_env_var(key: str, default: str = "") -> str:
    """Sucht nach Umgebungsvariablen im System und bekannten .env-Dateien."""
    val = os.environ.get(key)
    if val:
        return val

    candidates = [
        Path("/home/daniel/QJM/ibkr_live_daemon/.env"),
        Path("/home/daniel/QJM/mcp/agent-cda/.env"),
        Path("/home/daniel/QJM/llm-gateway/.env"),
        Path("/home/daniel/stock-data-node/.env"),
        Path("/home/daniel/QJM/.env"),
    ]

    for p in candidates:
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == key:
                            cleaned = v.strip().strip("'\"")
                            if cleaned:
                                return cleaned
            except Exception:
                pass
    return default


def normalize_supabase_url(url: str) -> str:
    """Ersetzt Docker-interne Hostnamen durch localhost für Host-Prozesse."""
    if not url:
        return "http://127.0.0.1:8001"
    if "host.docker.internal" in url or "gateway" in url:
        return url.replace("host.docker.internal", "127.0.0.1").replace("http://gateway:80", "http://127.0.0.1:8001")
    return url


def normalize_ticker_for_yf(ticker: str) -> Optional[str]:
    """Mappt interne Ticker auf das passende Yahoo Finance Symbol."""
    t = ticker.strip().upper()
    if not t or any(t.startswith(p) for p in SKIP_PREFIXES):
        return None

    if t in STATIC_YF_MAPPINGS:
        mapped = STATIC_YF_MAPPINGS[t]
        return None if mapped == "SKIP" else mapped

    # US-Ticker mit Dots (wie BRK.B) -> BRK-B
    # Aber internationale Suffixe (.DE, .PA, .L, .TO, .T, .OL, .ST) beibehalten
    parts = t.split(".")
    if len(parts) == 2:
        suffix = parts[1]
        if suffix in {"A", "B", "C", "WS", "U"}:
            return f"{parts[0]}-{suffix}"
    return t


class MasterUniverseClient:
    """Client für Lese- und Schreibzugriff auf Supabase cda_master_universe."""

    def __init__(self, supabase_url: str, supabase_key: str, timeout_sec: float = 15.0):
        self.url = normalize_supabase_url(supabase_url).rstrip("/")
        self.key = supabase_key
        self.timeout = timeout_sec

        if not self.key:
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY fehlt. Bitte in .env setzen oder via Argument übergeben.")

        self.headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }

    def fetch_tickers(self, only_missing: bool = True, explicit_tickers: Optional[List[str]] = None) -> List[str]:
        """Holt Ticker-Liste aus cda_master_universe."""
        if explicit_tickers:
            return [t.strip().upper() for t in explicit_tickers if t.strip()]

        endpoint = f"{self.url}/rest/v1/cda_master_universe"
        params = ["select=ticker,shares_outstanding", "order=ticker.asc"]
        if only_missing:
            params.append("shares_outstanding=is.null")

        full_url = f"{endpoint}?{'&'.join(params)}"
        req = urllib.request.Request(full_url, headers=self.headers, method="GET")

        tickers: List[str] = []
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for row in data:
                    t = str(row.get("ticker", "")).strip().upper()
                    if t and not any(t.startswith(p) for p in SKIP_PREFIXES):
                        tickers.append(t)
        except Exception as e:
            logger.error("Fehler beim Abrufen der Ticker aus Supabase: %s", e)
            raise

        return tickers

    def upsert_batch(self, rows: List[Dict[str, Any]]) -> bool:
        """Führt einen Bulk-Upsert in cda_master_universe durch."""
        if not rows:
            return True

        endpoint = f"{self.url}/rest/v1/cda_master_universe"
        data_bytes = json.dumps(rows).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data_bytes, headers=self.headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status in (200, 201, 204):
                    return True
                body = resp.read().decode("utf-8", errors="ignore")
                logger.error("Upsert fehlgeschlagen (Status %d): %s", resp.status, body)
                return False
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
            logger.error("HTTP-Fehler beim Upsert (%d): %s", e.code, err_body)
            return False
        except Exception as e:
            logger.error("Netzwerkfehler beim Upsert: %s", e)
            return False


def fetch_ticker_metadata(ticker: str, timeout_sec: float) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Fragt Aktien-Metadaten über yfinance ab.
    
    Rückgabe: (payload, should_retry)
    """
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

        # 2. Fallback / Fundamentaldaten aus info
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

        # PostgREST verlangt, dass alle Objekte im Batch exakt dieselben Keys haben!
        payload: Dict[str, Any] = {
            "ticker": ticker,
            "shares_outstanding": float(shares) if shares is not None else None,
            "currency": str(currency).strip().upper() if currency is not None else None,
            "eps": float(eps) if eps is not None else None,
            "revenue": float(revenue) if revenue is not None else None,
            "earnings": earnings_iso if earnings_iso else None,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        return payload, False

    except Exception as e:
        err_msg = str(e).lower()
        if "404" in err_msg or "not found" in err_msg or "delisted" in err_msg:
            return None, False
        logger.debug("Fehler beim Abruf von %s (%s): %s", ticker, yf_symbol, e)
        return None, True


def main() -> None:
    # CLI & ENV Konfiguration (Keine hartverdrahteten Limits)
    env_supabase_url = find_env_var("SUPABASE_URL", "http://127.0.0.1:8001")
    env_supabase_key = find_env_var("SUPABASE_SERVICE_ROLE_KEY", "")

    env_batch_size = int(os.environ.get("METADATA_BATCH_SIZE", "50"))
    env_delay = float(os.environ.get("METADATA_REQUEST_DELAY_SEC", "0.35"))
    env_timeout = float(os.environ.get("METADATA_TIMEOUT_SEC", "8.0"))
    env_max_retries = int(os.environ.get("METADATA_MAX_RETRIES", "3"))

    parser = argparse.ArgumentParser(
        description="Aktualisiert Metadaten (shares_outstanding, currency, etc.) in cda_master_universe"
    )
    parser.add_argument("--supabase-url", default=env_supabase_url, help=f"Supabase URL (Standard: {env_supabase_url})")
    parser.add_argument("--supabase-key", default=env_supabase_key, help="Supabase Service Role Key")
    parser.add_argument("--batch-size", type=int, default=env_batch_size, help=f"Batch-Größe für DB-Upserts (Standard: {env_batch_size})")
    parser.add_argument("--delay", type=float, default=env_delay, help=f"Pause in Sekunden zwischen Anfragen (Standard: {env_delay})")
    parser.add_argument("--timeout", type=float, default=env_timeout, help=f"Timeout pro Abfrage in Sekunden (Standard: {env_timeout})")
    parser.add_argument("--max-retries", type=int, default=env_max_retries, help=f"Wiederholungen bei Fehlern (Standard: {env_max_retries})")
    parser.add_argument("--limit", type=int, default=None, help="Maximal zu verarbeitende Ticker (z.B. für Tests)")
    parser.add_argument("--tickers", type=str, default=None, help="Kommagetrennte Liste spezifischer Ticker (z.B. AAPL,MSFT,BESI)")
    parser.add_argument("--all", action="store_true", help="Alle Ticker aktualisieren (auch bereits befüllte)")
    parser.add_argument("--dry-run", action="store_true", help="Daten abrufen, aber nicht in die DB schreiben")
    parser.add_argument("--verbose", "-v", action="store_true", help="Detaillierte Debug-Logs aktivieren")

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    try:
        client = MasterUniverseClient(args.supabase_url, args.supabase_key, timeout_sec=args.timeout)
    except Exception as err:
        logger.error("Initialisierungsfehler: %s", err)
        sys.exit(1)

    explicit_list = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
    logger.info("Lade Ticker-Liste aus cda_master_universe (only_missing=%s)...", not args.all)
    tickers = client.fetch_tickers(only_missing=not args.all, explicit_tickers=explicit_list)

    if args.limit and args.limit > 0:
        tickers = tickers[:args.limit]

    total = len(tickers)
    logger.info("Bereit zur Verarbeitung von %d Tickern (Batch-Größe: %d, Delay: %.2fs, Dry-Run: %s)",
                total, args.batch_size, args.delay, args.dry_run)

    if total == 0:
        logger.info("Keine Ticker zu aktualisieren. Fertig.")
        return

    # Graceful Shutdown Handler
    shutdown_requested = False
    pending_batch: List[Dict[str, Any]] = []

    def handle_signal(sig, frame):
        nonlocal shutdown_requested
        logger.warning("\nSignal %s empfangen! Beende sauber und sichere ausstehenden Batch...", sig)
        shutdown_requested = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    success_count = 0
    failed_count = 0
    skipped_count = 0
    start_time = time.time()

    for idx, ticker in enumerate(tickers, start=1):
        if shutdown_requested:
            break

        res = None
        for attempt in range(1, args.max_retries + 1):
            res, should_retry = fetch_ticker_metadata(ticker, timeout_sec=args.timeout)
            if res or not should_retry:
                break
            if attempt < args.max_retries:
                time.sleep(args.delay * attempt)

        pct = (idx / total) * 100
        if res:
            success_count += 1
            shares_val = res.get("shares_outstanding")
            shares_str = f"{int(shares_val):,}" if shares_val is not None else "N/A"
            curr_str = res.get("currency") or "N/A"
            logger.info("[%d/%d] (%.1f%%) %s -> Shares: %s, Währung: %s", idx, total, pct, ticker, shares_str, curr_str)
            pending_batch.append(res)
        else:
            failed_count += 1
            logger.warning("[%d/%d] (%.1f%%) %s -> Keine Metadaten gefunden", idx, total, pct, ticker)

        # Batch-Upsert ausführen
        if len(pending_batch) >= args.batch_size:
            if not args.dry_run:
                client.upsert_batch(pending_batch)
                logger.info("💾 Batch von %d Tickern erfolgreich in DB gespeichert.", len(pending_batch))
            else:
                logger.info("🔍 [Dry-Run] %d Ticker gesammelt (nicht geschrieben).", len(pending_batch))
            pending_batch = []

        if args.delay > 0:
            time.sleep(args.delay)

    # Verbleibende Einträge schreiben
    if pending_batch:
        if not args.dry_run:
            client.upsert_batch(pending_batch)
            logger.info("💾 Letzten Batch von %d Tickern erfolgreich in DB gespeichert.", len(pending_batch))
        else:
            logger.info("🔍 [Dry-Run] Letzten Batch von %d Tickern verarbeitet.", len(pending_batch))
        pending_batch = []

    elapsed = time.time() - start_time
    logger.info("Zusammenfassung:")
    logger.info("• Gesamtdauer: %.1f Sekunden", elapsed)
    logger.info("• Erfolgreich: %d", success_count)
    logger.info("• Fehlgeschlagen / keine Daten: %d", failed_count)
    logger.info("• Status: %s", "Vorzeitig abgebrochen" if shutdown_requested else "Vollständig abgeschlossen")


if __name__ == "__main__":
    main()
