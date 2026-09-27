"""Fundamentale Kennzahlen fuer die Topbar der Chart-Fenster.

Datenquelle ist die Supabase-Tabelle cda_master_universe (PostgREST). Sie kennt
shares_outstanding, currency, earnings (letzter gemeldeter Termin) und
next_earnings (naechster geplanter Termin). Eine Marktkapitalisierung ist dort
NICHT gespeichert - sie wird hier aus Schlusskurs x Aktienanzahl berechnet.

Alles ist optional: fehlt ein Feld, faellt der Baustein weg bzw. bleibt als "-"
sichtbar, damit ein fehlender Datenstand nicht stillschweigend verschwindet.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.parse
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from chart_viewer.agent.supabase import supabase_get
from chart_viewer.formatting import compact_si

logger = logging.getLogger("chart_viewer.fundamentals")

# TTL des prozessweiten Universe-Caches: die Info-Zeile wird bei jedem
# DISPLAY_STOCK neu gebaut, Stammdaten aendern sich hoechstens taeglich.
UNIVERSE_CACHE_TTL_SEC = float(os.environ.get("CV_UNIVERSE_CACHE_TTL_SEC", "300"))
UNIVERSE_TIMEOUT_SEC = float(os.environ.get("CV_UNIVERSE_TIMEOUT_SEC", "5.0"))

PLACEHOLDER = "-"

# Spaltennamen, unter denen ein Free Float liegen kann. Primaerquelle ist heute
# cda_master_universe.free_float / .free_float_percent (Massive /stocks/vX/float,
# taeglich per float_sync im stock-data-node aktualisiert); die weiteren Namen
# halten die Anzeige offen, falls eine zweite Quelle nachgezogen wird (Lookup ist
# case-insensitiv, "floatShares" faellt also mit rein).
FLOAT_SHARE_KEYS: Tuple[str, ...] = (
    "free_float",          # cda_master_universe (Massive /stocks/vX/float)
    "float_shares",
    "free_float_shares",
    "floatshares",
    "freefloatshares",
)
FLOAT_PCT_KEYS: Tuple[str, ...] = (
    "free_float_percent",  # cda_master_universe (Massive /stocks/vX/float)
    "float_pct",
    "free_float_pct",
    "float_percent",
    "float_percentage",
    "floatpercent",
)

_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}


def _to_float(value: Any) -> Optional[float]:
    """Zahl oder None; NaN/Inf und nicht-numerische Werte zaehlen als None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    return num


def _lookup_float(record: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    """Erster numerischer Wert der genannten Spalten (Spaltennamen case-insensitiv)."""
    lowered = {str(key).lower(): value for key, value in record.items()}
    for key in keys:
        if key in lowered:
            value = _to_float(lowered[key])
            if value is not None:
                return value
    return None


def fetch_universe_record(symbol: str, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """Stammdatensatz eines Tickers aus cda_master_universe (mit TTL-Cache).

    Fehler werden geloggt und als None zurueckgegeben: die Topbar ist Kosmetik
    und darf ein Chart nie verhindern.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return None

    now = time.time()
    if not force:
        cached = _cache.get(symbol)
        if cached and (now - cached[0]) < UNIVERSE_CACHE_TTL_SEC:
            return cached[1]

    record: Optional[Dict[str, Any]] = None
    try:
        query = urllib.parse.quote(symbol, safe="")
        path = f"/cda_master_universe?ticker=eq.{query}&select=*&limit=1"
        rows = supabase_get(path, timeout=UNIVERSE_TIMEOUT_SEC)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            record = rows[0]
    except Exception as exc:  # SupabaseError oder Ueberraschungen: nie durchreichen
        logger.warning("Universe-Metadaten fuer %s nicht verfuegbar: %s", symbol, exc)

    _cache[symbol] = (now, record)
    return record


def clear_cache() -> None:
    """Cache leeren (Tests / manuelles Nachladen)."""
    _cache.clear()


def market_cap(record: Dict[str, Any], last_close: Any) -> Optional[float]:
    """Market Cap = Schlusskurs x ausstehende Aktien (in Handelswaehrung)."""
    shares = _to_float(record.get("shares_outstanding"))
    close = _to_float(last_close)
    if not shares or shares <= 0 or not close or close <= 0:
        return None
    return shares * close


def float_pct(record: Dict[str, Any]) -> Optional[float]:
    """Free Float in Prozent der ausstehenden Aktien (None, wenn unbekannt).

    Eine explizite Prozent-Spalte gewinnt; sonst wird eine Stueckzahl-Spalte auf
    shares_outstanding bezogen. Die nackte Spalte "float" ist mehrdeutig: Werte
    bis 100 gelten als Prozent, groessere als Stueckzahl.
    """
    shares = _to_float(record.get("shares_outstanding"))

    pct = _lookup_float(record, FLOAT_PCT_KEYS)
    if pct is not None:
        return pct

    float_shares = _lookup_float(record, FLOAT_SHARE_KEYS)
    if float_shares is not None and float_shares > 0 and shares and shares > 0:
        return float_shares / shares * 100.0

    bare = _to_float(record.get("float"))
    if bare is not None and bare > 0:
        if bare <= 100:
            return bare
        if shares and shares > 0:
            return bare / shares * 100.0
    return None


def _parse_date_value(value: Any) -> Optional[date]:
    """ISO-Datum/-Timestamp (auch mit Z und Offset) als date, sonst None."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def earnings_info(
    record: Dict[str, Any], *, now: Optional[datetime] = None
) -> Tuple[Optional[str], Optional[date]]:
    """Label + Datum des Quartalstermins.

    Ein zukuenftiges next_earnings heisst "Next Earnings"; liegt der Wert in der
    Vergangenheit (nicht nachgezogen) oder fehlt er, wird der spaeteste bekannte
    Termin als "Earnings" gezeigt.
    """
    reference = now or datetime.now(timezone.utc)
    today = reference.date()

    next_date = _parse_date_value(record.get("next_earnings"))
    last_date = _parse_date_value(record.get("earnings"))

    if next_date and next_date >= today:
        return "Next Earnings", next_date
    known = [d for d in (next_date, last_date) if d]
    if known:
        return "Earnings", max(known)
    return None, None


def build_fundamental_parts(
    last_close: Any,
    record: Optional[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> List[str]:
    """Topbar-Bausteine fuer Market Cap, Shares Outstanding, Float und Earnings."""
    if not record:
        return []

    shares = _to_float(record.get("shares_outstanding"))
    currency = str(record.get("currency") or "").strip().upper()
    cap = market_cap(record, last_close)
    earnings_label, earnings_date = earnings_info(record, now=now)

    parts: List[str] = []
    if cap is not None:
        parts.append(f"Mkt Cap: {compact_si(cap)}{' ' + currency if currency else ''}")
    elif currency:
        # Ohne Stueckzahl gibt es keine Market Cap - die Waehrung soll trotzdem
        # sichtbar sein, gerade bei nicht-US-Tickern.
        parts.append(f"Currency: {currency}")
    if shares is not None and shares > 0:
        parts.append(f"Shares Out: {compact_si(shares)}")

    # Ohne jeden echten Fundamentaldatenpunkt waere der Rest der Zeile nur
    # Rauschen ("Float: -"), also bleibt es bei dem, was da ist.
    if cap is None and not shares and earnings_date is None:
        return parts

    pct = float_pct(record)
    parts.append(f"Float: {pct:.1f} %" if pct is not None else f"Float: {PLACEHOLDER}")
    if earnings_date is not None:
        parts.append(f"{earnings_label}: {earnings_date.strftime('%d.%m.%Y')}")
    return parts


def fundamental_topbar_parts(symbol: str, last_close: Any) -> List[str]:
    """Wie build_fundamental_parts, holt den Datensatz aber selbst und schluckt Fehler."""
    try:
        record = fetch_universe_record(symbol)
        return build_fundamental_parts(last_close, record)
    except Exception as exc:
        logger.warning("Fundamentaldaten fuer %s fehlten: %s", symbol, exc)
        return []
