"""Tests fuer die fundamentalen Topbar-Felder (Market Cap, Shares, Float, Earnings).

Die Anzeige lebt von cda_master_universe: shares_outstanding und currency sind
Pflichtfelder fuer Market Cap / Shares Out, ein Free Float ist dort heute nicht
gespeichert - "Float: -" macht diese Luecke sichtbar, statt sie zu verstecken.
"""

from datetime import datetime, timezone

import chart_viewer.fundamentals as fund
from chart_viewer.orchestrator import (
    TOPBAR_LABEL_OVERRIDES,
    _short_topbar_label,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _record(**overrides):
    base = {
        "ticker": "AAPL",
        "shares_outstanding": 15_000_000_000.0,
        "currency": "USD",
        "earnings": "2026-07-30T20:00:00+00:00",
        "next_earnings": "2026-10-29T20:00:00+00:00",
    }
    base.update(overrides)
    return base


# -- Zahlen ----------------------------------------------------------------


def test_market_cap_is_close_times_shares():
    assert fund.market_cap(_record(), 200.0) == 3e12


def test_market_cap_needs_both_inputs():
    assert fund.market_cap(_record(shares_outstanding=None), 200.0) is None
    assert fund.market_cap(_record(), None) is None


def test_float_pct_from_share_column():
    rec = _record(shares_outstanding=1000.0, float_shares=780.0)
    assert fund.float_pct(rec) == 78.0


def test_float_pct_prefers_explicit_percentage_column():
    rec = _record(shares_outstanding=1000.0, float_pct=63.5, float_shares=1.0)
    assert fund.float_pct(rec) == 63.5


def test_float_pct_reads_the_master_universe_columns():
    """Primaerquelle: cda_master_universe.free_float_percent / .free_float (Massive-Sync)."""
    rec = _record(free_float_percent=92.1, free_float=13_521_179_933)
    assert fund.float_pct(rec) == 92.1
    # Ohne Prozent-Spalte wird die Stueckzahl auf shares_outstanding bezogen.
    assert fund.float_pct(_record(shares_outstanding=1_000.0, free_float=780.0)) == 78.0


def test_float_pct_bare_column_is_ambiguous_but_usable():
    # Werte bis 100 sind Prozent, groessere Werte eine Stueckzahl.
    assert fund.float_pct(_record(float=42.0)) == 42.0
    assert fund.float_pct(_record(shares_outstanding=1000.0, float=250.0)) == 25.0


def test_float_pct_is_none_without_any_float_source():
    assert fund.float_pct(_record()) is None


# -- Earnings --------------------------------------------------------------


def test_earnings_prefers_the_upcoming_date():
    label, when = fund.earnings_info(_record(), now=NOW)
    assert (label, when.isoformat()) == ("Next Earnings", "2026-10-29")


def test_earnings_falls_back_to_last_report_when_next_is_stale():
    rec = _record(earnings="2026-07-30T20:00:00+00:00", next_earnings="2026-09-01T20:00:00+00:00")
    label, when = fund.earnings_info(rec, now=NOW)
    assert (label, when.isoformat()) == ("Earnings", "2026-09-01")


def test_earnings_without_any_date():
    assert fund.earnings_info(_record(earnings=None, next_earnings=None), now=NOW) == (None, None)


# -- Topbar-Bausteine ------------------------------------------------------


def test_parts_render_market_cap_shares_float_and_currency():
    parts = fund.build_fundamental_parts(200.0, _record(), now=NOW)
    assert parts == [
        "Mkt Cap: 3T USD",
        "Shares Out: 15G",
        "Float: -",
        "Next Earnings: 29.10.2026",
    ]


def test_parts_use_suffix_notation_for_small_caps():
    rec = _record(shares_outstanding=120_000.0, currency="EUR")
    parts = fund.build_fundamental_parts(0.5, rec, now=NOW)
    assert parts[0] == "Mkt Cap: 60k EUR"
    assert parts[1] == "Shares Out: 120k"


def test_parts_are_empty_without_any_real_data():
    """Ein Ticker ohne Fundamentaldaten soll die Zeile nicht mit Platzhaltern fuellen."""
    assert fund.build_fundamental_parts(200.0, {"ticker": "IWM"}, now=NOW) == []
    assert fund.build_fundamental_parts(200.0, None, now=NOW) == []


def test_currency_stays_visible_without_shares():
    """4GLD-Fall: Waehrung ohne Stueckzahl -> "Currency: EUR" statt gar nichts."""
    rec = {"ticker": "4GLD", "currency": "EUR", "shares_outstanding": None}
    assert fund.build_fundamental_parts(14.2, rec, now=NOW) == ["Currency: EUR"]


def test_fetch_is_cached_and_never_raises(monkeypatch):
    calls = []

    def fake_get(path, **kwargs):
        calls.append(path)
        return [{"ticker": "AAPL", "shares_outstanding": 10.0}]

    fund.clear_cache()
    monkeypatch.setattr(fund, "supabase_get", fake_get)

    assert fund.fetch_universe_record("aapl")["shares_outstanding"] == 10.0
    assert fund.fetch_universe_record("AAPL")["shares_outstanding"] == 10.0
    assert len(calls) == 1, "zweiter Aufruf muss aus dem TTL-Cache kommen"

    def boom(path, **kwargs):
        raise RuntimeError("Supabase weg")

    monkeypatch.setattr(fund, "supabase_get", boom)
    fund.clear_cache()
    assert fund.fetch_universe_record("AAPL") is None
    fund.clear_cache()


# -- Orchestrator-Info-Zeile ----------------------------------------------


def test_short_label_maps_ibd_rs_rating():
    assert TOPBAR_LABEL_OVERRIDES["ibd_rs"] == "IBD-RS"
    assert _short_topbar_label("ibd_rs", "ibd_rs", "IBD RS Rating") == "IBD-RS"
    assert _short_topbar_label("adr_20", "adr_20", "ADR 20") == "ADR 20"


def test_orchestrator_topbar_row(monkeypatch):
    """Die Info-Zeile: IBD-RS kurz, kein Overlay-Zaehler, Fundamentaldaten hinten."""
    import chart_viewer.orchestrator as orch

    t0 = 1_700_000_000
    monkeypatch.setattr(
        orch,
        "fetch_chart_data",
        lambda symbol, timeframe="1D", limit=2000: {
            "status": "ok",
            "columns": ["timestamp", "open", "high", "low", "close", "volume", "adr_20", "ibd_rs"],
            "data": [[t0, 100.0, 101.0, 99.0, 100.5, 1_000_000.0, 1.38, 92.0]],
            "features_stale": False,
        },
    )
    monkeypatch.setattr(
        orch,
        "_pca_get",
        lambda path: {
            "features": [
                {"canonical_id": "adr_20", "alias": "adr_20", "display_name": "ADR 20", "calc_type": "ADR"},
                {"canonical_id": "ibd_rs", "alias": "ibd_rs", "display_name": "IBD RS Rating", "calc_type": "IBD_RS"},
            ]
        },
    )
    monkeypatch.setattr(
        orch,
        "fundamental_topbar_parts",
        lambda symbol, last_close: fund.build_fundamental_parts(last_close, _record(), now=NOW),
    )

    cmd = orch.build_display_stock(
        "AAPL", indicators=[], topbar_metrics=["adr_20", "ibd_rs"], window_id="w1"
    )
    content = cmd["topbar"]["content"]

    assert content == (
        "AAPL | Last: $100.50 | ADR 20: 1,38 % | IBD-RS: 92 "
        "| Mkt Cap: 1.508T USD | Shares Out: 15G | Float: - "
        "| Next Earnings: 29.10.2026 | Bars: 1"
    )
    assert "Overlays" not in content
