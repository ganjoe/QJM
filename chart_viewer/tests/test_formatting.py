"""Unit tests for the shared number formatting (chart value boxes + topbar)."""

from chart_viewer.formatting import (
    compact_number,
    compact_si,
    de_num,
    format_value,
    format_volume,
)


def test_de_num_uses_german_separators():
    assert de_num(1234.5) == "1.234,50"


def test_compact_number_suffixes():
    assert compact_number(1080860352.96) == "1,08 Mrd."
    assert compact_number(1234567.0) == "1,23 Mio."
    assert compact_number(1500.0) == "1,5 Tsd."
    assert compact_number(250.0) == "250"


def test_compact_number_currency():
    assert compact_number(2.5e9, currency=True) == "2,5 Mrd. $"


def test_compact_si_suffixes():
    """Suffix-Notation der Topbar: 1.3M, 100k, 2.2T."""
    assert compact_si(1_300_000) == "1.3M"
    assert compact_si(100_000) == "100k"
    assert compact_si(2.2e12) == "2.2T"
    assert compact_si(65_400_000_000) == "65.4B"
    assert compact_si(1_000_000_000) == "1B"
    assert compact_si(950) == "950"
    assert compact_si(-42_000) == "-42k"


def test_format_value_precision():
    assert format_value(185.321) == "185.32"
    assert format_value(4500.25) == "4500.25"
    assert format_value(1234567.0) == "1,23 Mio."
    assert format_value(0.00871) == "0.0087"
    assert format_value(0.0) == "0.00"
    assert format_value(2.5) == "2.50"


def test_format_volume_is_compact():
    assert format_volume(12345678.0) == "12,35 Mio."
    assert format_volume(950.0) == "950"
