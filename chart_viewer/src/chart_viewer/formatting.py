"""Shared number formatting for chart values and topbar metrics.

Single source of truth so the topbar (orchestrator) and the chart's Y-axis value
boxes render the same numbers the same way.
"""

from __future__ import annotations


def de_num(value: float, decimals: int = 2) -> str:
    """Zahl mit deutschem Format: Komma als Dezimal-, Punkt als Tausendertrenner."""
    text = f"{value:,.{decimals}f}"
    # Separatoren ueber einen Platzhalter tauschen, damit sie sich nicht ueberschreiben
    return text.replace(",", "\u00a0").replace(".", ",").replace("\u00a0", ".")


def compact_number(value: float, currency: bool = False) -> str:
    """Grosse Zahlen kompakt darstellen: 1080860352.96 -> '1,08 Mrd. $'."""
    sign = "-" if value < 0 else ""
    amount = abs(value)
    for limit, suffix in ((1e12, "Bio."), (1e9, "Mrd."), (1e6, "Mio."), (1e3, "Tsd.")):
        if amount >= limit:
            scaled = f"{amount / limit:.2f}".rstrip("0").rstrip(".")
            scaled = scaled.replace(".", ",")
            return f"{sign}{scaled} {suffix}{' $' if currency else ''}"
    return f"{sign}{de_num(amount, 0)}{' $' if currency else ''}"


def compact_si(value: float) -> str:
    """Grosse Stueckzahlen in Suffix-Notation: 1300000 -> '1.3M', 100000 -> '100k'.

    Bewusst NICHT die deutsche Kurzform ('1,3 Mio.'): Volumen und Achsen nutzen
    compact_number, die fundamentalen Topbar-Felder (Market Cap, Shares
    Outstanding) lesen sich mit k/M/B/T kompakter - und in einer Zeile, die
    ohnehin schon Kurs und ADR zeigt, ist jedes Zeichen knapp.
    """
    sign = "-" if value < 0 else ""
    amount = abs(float(value))
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if amount >= limit:
            scaled = amount / limit
            text = f"{scaled:.1f}" if scaled < 100 else f"{scaled:.0f}"
            if text.endswith(".0"):
                text = text[:-2]
            return f"{sign}{text}{suffix}"
    return f"{sign}{amount:.0f}"


def format_value(value: float) -> str:
    """Kurs-/Indikatorwert fuer die Crosshair-Wert-Box.

    Dezimaltrenner ist der Punkt wie bei den Achsen-Labels des Charts; grosse
    Zahlen werden kompakt dargestellt, sehr kleine behalten vier Nachkommastellen
    (z. B. ADR-Prozente).
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    amount = abs(num)
    if amount >= 1e6:
        return compact_number(num)
    if amount >= 1:
        return f"{num:.2f}"
    if amount > 0:
        return f"{num:.4f}"
    return "0.00"


def format_volume(value: float) -> str:
    """Volumen kompakt: 12345678 -> '12,35 Mio.'."""
    try:
        return compact_number(float(value))
    except (TypeError, ValueError):
        return str(value)
