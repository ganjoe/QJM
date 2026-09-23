"""Anzeigeformate fuer das Fenster.

Wichtig: Der Server liefert JSON, also sind Zeitstempel ZEICHENKETTEN
("2026-09-21T22:24:08.465272+00:00") und keine datetime-Objekte. Genau daran
ist die erste Fassung gescheitert — sie fiel still auf str() zurueck und zeigte
den rohen ISO-Wert mit Mikrosekunden.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

GERMAN_MONTHS = ("", "Jan", "Feb", "Maerz", "Apr", "Mai", "Jun",
                 "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")


def as_datetime(value: Any) -> datetime | None:
    """ISO-Zeichenkette oder datetime -> datetime (mit Zeitzone). None, wenn unbrauchbar."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def dt(value: Any, seconds: bool = False) -> str:
    """Menschenlesbar in LOKALER Zeit: '22.09.2026 00:19'."""
    parsed = as_datetime(value)
    if parsed is None:
        return "-" if value in (None, "") else str(value)
    try:
        local = parsed.astimezone()
    except (OSError, ValueError):
        local = parsed
    return local.strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")


def relative(value: Any) -> str:
    """'in 3 Tagen', 'vor 2 Std' — fuer den naechsten Termin."""
    parsed = as_datetime(value)
    if parsed is None:
        return "-"
    delta = parsed - datetime.now(timezone.utc)
    seconds = int(delta.total_seconds())
    past = seconds < 0
    seconds = abs(seconds)
    if seconds < 90:
        text = "gleich"
    elif seconds < 3600:
        text = f"{seconds // 60} Min"
    elif seconds < 108_000:
        text = f"{seconds // 3600} Std"
    else:
        text = f"{seconds // 86400} Tg"
    if text == "gleich":
        return text
    return ("vor " if past else "in ") + text


def elapsed(since: Any) -> str:
    """Laufzeit als '8:31' bzw. '1:02:11'. Leer, wenn unbrauchbar.

    Die Sekretaerin schreibt den Verbrauch eines Items erst beim Einsammeln,
    also nach dem Ende — waehrend eines Laufs ist die verstrichene Zeit die
    einzige ehrliche Fortschrittsanzeige.
    """
    parsed = as_datetime(since)
    if parsed is None:
        return ""
    seconds = int((datetime.now(timezone.utc) - parsed).total_seconds())
    if seconds < 0:
        seconds = 0
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def tokens(value: Any) -> str:
    number = int(value or 0)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.0f}k"
    return str(number) if number else "-"


def tokens_exact(value: Any) -> str:
    return f"{int(value or 0):,}".replace(",", ".")
