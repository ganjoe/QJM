#!/usr/bin/env python3
"""
scripts/earnings_backfill.py - Tiefer Earnings-Historie-Backfill via yfinance.

Laeuft IM stock-data-node-Container (dort sind yfinance + pandas installiert) und schreibt
direkt per PostgREST nach Supabase:

  * cda_earnings_history              : alle VERGANGENEN Termine (report_date, EPS estimate/actual, Surprise)
  * cda_master_universe.earnings      : letzter gemeldeter Termin (nur vorwaerts, nie rueckwaerts)
  * cda_master_universe.next_earnings : naechster Termin (ausser source=manual und noch zukuenftig)

Aufruf aus dem QJM-Workspace:

  docker exec -i stock-data-node python3 - --tickers META --dry-run < scripts/earnings_backfill.py
  docker exec -i stock-data-node python3 - --watchlist current_positions --history 12 < scripts/earnings_backfill.py
  docker exec -i stock-data-node python3 - --tickers META,MSFT,NVDA --history 20 < scripts/earnings_backfill.py

Abgrenzung: manage_earnings_calendar (mcp-cda) ist der agentenfaehige Weg fuer einzelne Ticker
und liefert zusaetzlich das Geschaeftsquartal; dieses Skript ist fuer die Masse und die tiefe Historie.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/app/src")
try:  # Container-Konventionen wiederverwenden (Ticker-Mapping, URL-Normalisierung)
    from metadata_enricher import normalize_supabase_url, normalize_ticker_for_yf  # type: ignore
except Exception:  # pragma: no cover - Fallback fuer Aufrufe ausserhalb des Containers
    def normalize_supabase_url(url: str) -> str:
        return (url or "http://host.docker.internal:8001").rstrip("/")

    def normalize_ticker_for_yf(ticker: str) -> Optional[str]:
        t = (ticker or "").strip().upper()
        if not t or t[0] in "$=^":
            return None
        parts = t.split(".")
        if len(parts) == 2 and parts[1] in ("A", "B", "C", "WS", "U"):
            return parts[0] + "-" + parts[1]
        return t


def _sb_config():
    url = normalize_supabase_url(os.environ.get("SUPABASE_URL", "http://host.docker.internal:8001"))
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return url, key


def _headers(key: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _request(method: str, url: str, key: str, body: Any = None, extra: Optional[Dict[str, str]] = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(key, extra), method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8") or "null"
        return resp.status, json.loads(raw)


def load_watchlist(url: str, key: str, list_name: str) -> List[str]:
    endpoint = url + "/rest/v1/pca_watchlists?select=ticker&list_name=eq." + urllib.parse.quote(list_name)
    _, rows = _request("GET", endpoint, key)
    return [str(r.get("ticker", "")).strip().upper() for r in (rows or []) if r.get("ticker")]


def load_master_row(url: str, key: str, ticker: str) -> Optional[Dict[str, Any]]:
    endpoint = (url + "/rest/v1/cda_master_universe?select=ticker,earnings,next_earnings,next_earnings_source"
                "&ticker=eq." + urllib.parse.quote(ticker))
    _, rows = _request("GET", endpoint, key)
    return rows[0] if rows else None


def count_history(url: str, key: str, ticker: str) -> int:
    endpoint = (url + "/rest/v1/cda_earnings_history?select=report_date&ticker=eq." + urllib.parse.quote(ticker))
    _, rows = _request("GET", endpoint, key)
    return len(rows or [])


def upsert_history(url: str, key: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    endpoint = url + "/rest/v1/cda_earnings_history?on_conflict=ticker,report_date"
    _request("POST", endpoint, key, rows, {"Prefer": "resolution=merge-duplicates,return=minimal"})


def patch_master(url: str, key: str, ticker: str, patch: Dict[str, Any]) -> None:
    if not patch:
        return
    endpoint = url + "/rest/v1/cda_master_universe?ticker=eq." + urllib.parse.quote(ticker)
    _request("PATCH", endpoint, key, patch, {"Prefer": "return=minimal"})


def _num(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        f = float(value)
        if f != f:  # NaN
            return None
        return round(f, 4)
    except (TypeError, ValueError):
        return None


def fetch_earnings_frame(symbol: str, history: int):
    import yfinance as yf  # lokal importiert: nur im Container verfuegbar

    ticker = yf.Ticker(symbol)
    return ticker.get_earnings_dates(limit=max(4, history))


def rows_from_frame(ticker: str, frame, history_limit: int):
    """Zerlegt den yfinance-Frame in (historie_rows, next_iso, last_report_iso)."""
    now = datetime.now(timezone.utc)
    hist: List[Dict[str, Any]] = []
    next_iso: Optional[str] = None
    last_report: Optional[str] = None

    if frame is None or len(frame) == 0:
        return hist, next_iso, last_report

    ordered = sorted(frame.index, reverse=True)
    for idx in ordered:
        row = frame.loc[idx]
        if hasattr(row, "columns"):  # DataFrame bei doppelten Indizes -> erste Zeile nehmen
            row = row.iloc[0]
        ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        iso = ts.astimezone(timezone.utc).isoformat()
        estimate = _num(row.get("EPS Estimate"))
        actual = _num(row.get("Reported EPS"))
        surprise = _num(row.get("Surprise(%)"))

        if ts > now:
            if next_iso is None or iso < next_iso:
                next_iso = iso
            continue

        hist.append({
            "ticker": ticker,
            "report_date": iso,
            "eps_actual": actual,
            "eps_estimate": estimate,
            "eps_surprise_pct": surprise,
            "source": "yfinance",
        })
        if actual is not None and (last_report is None or iso > last_report):
            last_report = iso

    hist = sorted(hist, key=lambda r: r["report_date"], reverse=True)[:history_limit]
    return hist, next_iso, last_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Earnings-Historie-Backfill via yfinance (im stock-data-node-Container ausfuehren).")
    parser.add_argument("--tickers", help="Komma-Liste von Tickern")
    parser.add_argument("--watchlist", help="Supabase-Watchlist (z.B. current_positions)")
    parser.add_argument("--history", type=int, default=12, help="Quartale je Ticker (Default 12)")
    parser.add_argument("--sleep", type=float, default=1.0, help="Pause zwischen Tickern in Sekunden (Default 1.0)")
    parser.add_argument("--limit", type=int, default=0, help="max. Anzahl Ticker (0 = alle)")
    parser.add_argument("--only-missing", action="store_true", help="Ticker mit vorhandener Historie ueberspringen")
    parser.add_argument("--no-next", action="store_true", help="next_earnings nicht anfassen")
    parser.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts schreiben")
    args = parser.parse_args()

    url, key = _sb_config()
    if not key:
        print("FEHLER: SUPABASE_SERVICE_ROLE_KEY fehlt (im stock-data-node-Container gesetzt?)")
        return 2

    tickers: List[str] = []
    if args.tickers:
        tickers += [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if args.watchlist:
        tickers += load_watchlist(url, key, args.watchlist)
    tickers = list(dict.fromkeys([t for t in tickers if t]))
    if not tickers:
        print("FEHLER: --tickers oder --watchlist angeben.")
        return 2
    if args.limit and args.limit > 0:
        tickers = tickers[: args.limit]

    print("Earnings-Backfill: %d Ticker, %d Quartale, dry_run=%s" % (len(tickers), args.history, args.dry_run))
    print("%-10s %-8s %-12s %-12s %s" % ("Ticker", "Historie", "Letzter", "Naechster", "Hinweis"))
    print("-" * 78)

    written_total = 0
    for i, ticker in enumerate(tickers):
        symbol = normalize_ticker_for_yf(ticker)
        if not symbol:
            print("%-10s %-8s %-12s %-12s %s" % (ticker, "-", "-", "-", "uebersprungen (kein Symbol)"))
            continue
        try:
            master = load_master_row(url, key, ticker)
            if not master:
                print("%-10s %-8s %-12s %-12s %s" % (ticker, "-", "-", "-", "nicht im Master Universe"))
                continue
            if args.only_missing and count_history(url, key, ticker) >= args.history:
                print("%-10s %-8s %-12s %-12s %s" % (ticker, "-", "-", "-", "uebersprungen (Historie vorhanden)"))
                continue

            frame = fetch_earnings_frame(symbol, args.history)
            hist, next_iso, last_report = rows_from_frame(ticker, frame, args.history)
            if not hist and not next_iso:
                print("%-10s %-8s %-12s %-12s %s" % (ticker, "-", "-", "-", "keine yfinance-Daten"))
                continue

            patch: Dict[str, Any] = {}
            if last_report and (not master.get("earnings") or last_report > master["earnings"]):
                patch["earnings"] = last_report

            if not args.no_next and next_iso:
                cur_next = master.get("next_earnings")
                protected = (master.get("next_earnings_source") or "") in ("manual", "confirmed")
                cur_future = bool(cur_next) and cur_next > datetime.now(timezone.utc).isoformat()
                if protected and cur_future:
                    note = "next: " + master.get("next_earnings_source") + " geschuetzt"
                else:
                    patch["next_earnings"] = next_iso
                    patch["next_earnings_source"] = "yfinance"
                    note = ""
            else:
                note = "next: uebersprungen" if args.no_next else ""

            if not args.dry_run:
                upsert_history(url, key, hist)
                patch_master(url, key, ticker, patch)
                written_total += len(hist)

            print("%-10s %-8s %-12s %-12s %s" % (
                ticker,
                "%d%s" % (len(hist), "" if args.dry_run else " geschr."),
                (last_report or "-")[:10],
                (next_iso or "-")[:10],
                note,
            ))
        except Exception as exc:  # ein kaputter Ticker stoppt den Lauf nicht
            print("%-10s %-8s %-12s %-12s FEHLER: %s" % (ticker, "-", "-", "-", str(exc)[:60]))

        if i < len(tickers) - 1 and args.sleep > 0:
            time.sleep(args.sleep)

    print("-" * 78)
    print("Fertig. %s%d Historien-Zeilen." % ("(dry-run) " if args.dry_run else "", written_total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
