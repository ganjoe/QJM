"""Shared number formatting for chart values, axes and topbar metrics.

Single source of truth so axis labels, crosshair value boxes (the "data cursor")
and the topbar always print the same number the same way.

The one rule every caller obeys (see compact_de):

    0 .. 9.999        plain integer, no thousands separator
    10.000 and up     k / M / G / T, dot as decimal separator

Suffix values carry at most four significant digits, so a label never grows
past five characters: 1000, 1.120k, 12.35k, 102.1k, 1.123M, 10.52M, 100.1M,
1.234G, 10.52T. A value that quantizes onto the next tier is promoted instead
of spilling (999950 -> "1M", not "1000k"). That is what keeps a nine-digit
dollar volume from blowing up the pane or the data cursor.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP

# Suffix tiers as (factor, suffix). Order matters: largest factor first.
SUFFIX_TIERS: tuple = (
    (1e12, "T"),
    (1e9, "G"),
    (1e6, "M"),
    (1e3, "k"),
)
# Below this value the plain integer is shorter than any suffix.
PLAIN_INTEGER_LIMIT = 10_000.0
# Each tier unit spans 1..999.99..; 1000.0 means "the next tier is the truth".
TIER_CEILING = 1000.0
# Four significant digits -> at most this many decimals per magnitude band.
BAND_DECIMALS = ((100.0, 1), (10.0, 2))


def de_num(value: float, decimals: int = 2) -> str:
    """Zahl mit deutschem Format: Komma als Dezimal-, Punkt als Tausendertrenner."""
    text = f"{value:,.{decimals}f}"
    # Separatoren ueber einen Platzhalter tauschen, damit sie sich nicht ueberschreiben
    return text.replace(",", "\u00a0").replace(".", ",").replace("\u00a0", ".")


def _drop_trailing_zeros(text: str) -> str:
    """'1.120' -> '1.12', '12.30' -> '12.3', '100.0' -> '100', '1.000' -> '1'."""
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".")


def _quantize(value: float, decimals: int) -> float:
    """Kaufmaennisch auf die gewaehlte Stellenzahl runden.

    Decimal statt round(): nur so wird 999.95 bei einer Nachkommastelle
    zuverlaessig 1000.0 (round() liefert dort 999.9, weil der Binaerwert knapp
    darunter liegt) - genau der Fall, der ueber die Stufenpromotion entscheidet.
    """
    exponent = Decimal(1).scaleb(-max(decimals, 0))
    return float(Decimal(str(value)).quantize(exponent, rounding=ROUND_HALF_UP))


def _band_decimals(scaled: float, decimals: int) -> int:
    """Stellenzahl der Groessenordnung: 1..9.99 drei, ab 10 zwei, ab 100 eine."""
    precision = max(decimals, 0)
    for threshold, band_precision in BAND_DECIMALS:
        if scaled >= threshold:
            return min(precision, band_precision)
    return precision


def compact_de(value: float, decimals: int = 3) -> str:
    """Zahl kompakt mit k/M/G/T-Suffix - die eine Regel fuer alle Anzeigen.

    decimals ist das Maximum innerhalb der gewaehlten Stufe (Default 3 = vier
    signifikante Stellen fuer 1..9.99); die Stufe selbst reduziert auf 2 Stellen
    ab 10 und auf 1 Stelle ab 100. Werte unter 10.000 bleiben ganzzahlig.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(num):
        return str(value)

    sign = "-" if num < 0 else ""
    amount = abs(num)

    if amount < PLAIN_INTEGER_LIMIT:
        return f"{sign}{amount:,.0f}"

    for index, (factor, suffix) in enumerate(SUFFIX_TIERS):
        if amount < factor:
            continue
        scaled = amount / factor
        precision = _band_decimals(scaled, decimals)
        if index > 0 and _quantize(scaled, precision) >= TIER_CEILING:
            # Die eigene Stufe zeigt 1000: die naechstgroessere ist die Wahrheit
            # (999950 -> '1M' statt '1000k').
            factor, suffix = SUFFIX_TIERS[index - 1]
            scaled = amount / factor
            precision = _band_decimals(scaled, decimals)
        if _quantize(scaled, precision) >= TIER_CEILING:
            # Groesste Stufe und immer noch voll: nicht auf eine erfundene
            # Nachfolgestufe ausweichen, sondern grob und kurz bleiben.
            return f"{sign}{_quantize(scaled, 0):.0f}{suffix}"
        if precision == 0:
            return f"{sign}{_quantize(scaled, 0):.0f}{suffix}"
        text = _drop_trailing_zeros(f"{_quantize(scaled, precision):.{precision}f}")
        return f"{sign}{text}{suffix}"
    return f"{sign}{amount:,.0f}"


def format_value(value: float) -> str:
    """Kurs-/Indikatorwert fuer die Crosshair-Wert-Box (den Daten-Cursor).

    Derselbe Vertrag wie die Achsen-Labels: grosse Zahlen kompakt in
    Suffix-Notation, Kurse mit zwei, sehr kleine Werte (ADR-Prozente) mit vier
    Nachkommastellen.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    amount = abs(num)
    if amount >= 1000:
        return compact_de(num)
    if amount >= 1:
        return f"{num:.2f}"
    if amount > 0:
        return f"{num:.4f}"
    return "0.00"


def format_axis_value(value: float) -> str:
    """Y-Achsen-Label und Crosshair-Preis-Badge: dieselbe Suffix-Regel."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return compact_de(num, decimals=0)


def format_volume(value: float) -> str:
    """Volumen kompakt: 12345678 -> '12.30M'."""
    return compact_de(value, decimals=2)
