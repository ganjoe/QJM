"""Unit tests for the shared number formatting (chart axes, value boxes, topbar).

Vertrag (eine Regel fuer alle Anzeigen):
    0 .. 9.999     Ganzzahl, kein Tausenderpunkt
    ab 10.000      k / M / G / T mit Punkt als Dezimaltrenner
Hoeckstens vier signifikante Stellen, damit ein Label nie breiter als fuenf
Zeichen wird; ein Wert, der auf die naechste Stufe rundet, wird dorthin
befoerdert (999950 -> "1M" statt "1000k").
"""

import pytest

from chart_viewer.formatting import (
    compact_de,
    de_num,
    format_axis_value,
    format_value,
    format_volume,
)


def test_de_num_uses_german_separators():
    assert de_num(1234.5) == "1.234,50"


@pytest.mark.parametrize(
    "value, expected",
    [
        # Die Vorgabe woertlich: 1000, 1.120k, 12.30k, 102.1k, 1.123M, 10.52M, 100.1M
        (1000, "1,000"),
        (1120, "1,120"),
        (12300, "12.3k"),
        (102100, "102.1k"),
        (1_123_000, "1.123M"),
        (10_520_000, "10.52M"),
        (100_100_000, "100.1M"),
        (1_234_000_000, "1.234G"),
        (10_520_000_000, "10.52G"),
        (1_234_000_000_000, "1.234T"),
        # Unter 10.000 bleibt die Ganzzahl (vier Ziffern brauchen kein Suffix)
        (0, "0"),
        (950, "950"),
        (9999, "9,999"),
        (10_000, "10k"),
        # Stufenpromotion: nie fuenf Ziffern vor dem Suffix
        (999_949, "999.9k"),
        (999_950, "1M"),
        (999_999, "1M"),
        (999_999_999, "1G"),
        (999_950_000_000, "1T"),
        # Groesste Stufe: kein erfundenes Q, lieber kurz und grob
        (1.3e15, "1300T"),
        # Vorzeichen bleibt aussen
        (-42_000, "-42k"),
        (-999_999, "-1M"),
        (-999_949, "-999.9k"),
    ],
)
def test_compact_de_rule(value, expected):
    assert compact_de(value) == expected


def test_compact_de_rounds_half_up_not_bankers():
    """999.95 muss auf 1000.0 runden, sonst entstuende doch ein '1000k'."""
    assert compact_de(999_950) == "1M"
    assert compact_de(2_500_000) == "2.5M"
    assert compact_de(1_050_000) == "1.05M"


def test_compact_de_precision_is_bounded():
    """Vier signifikante Stellen: 10er-Band zwei, 100er-Band eine Nachkommastelle."""
    assert compact_de(12_345) == "12.35k"
    assert compact_de(123_456) == "123.5k"
    assert compact_de(1_234_567) == "1.235M"
    assert compact_de(12_345_678) == "12.35M"


def test_compact_de_never_breaks_on_junk():
    assert compact_de(float("nan")) == "nan"
    assert compact_de("nope") == "nope"


def test_format_value_precision():
    assert format_value(185.321) == "185.32"
    assert format_value(2.5) == "2.50"
    assert format_value(0.00871) == "0.0087"
    assert format_value(0.0) == "0.00"
    # Ab 1000 gilt die Suffix-Regel, darunter bleibt der volle Kurs stehen.
    assert format_value(950.0) == "950.00"
    assert format_value(102_500_000.0) == "102.5M"


def test_format_axis_value_is_short():
    """Achsen-Labels: ganze Zahlen, ab 10.000 dieselbe Suffix-Regel."""
    assert format_axis_value(1000) == "1,000"
    assert format_axis_value(9999) == "9,999"
    assert format_axis_value(12_300) == "12k"
    assert format_axis_value(102_100) == "102k"
    assert format_axis_value(1_123_000) == "1M"
    assert format_axis_value(100_100_000) == "100M"
    assert format_axis_value(1_234_000_000) == "1G"


def test_format_volume_uses_suffix_notation():
    assert format_volume(12345678.0) == "12.35M"
    assert format_volume(950.0) == "950"
    assert format_volume(250_000_000.0) == "250M"
