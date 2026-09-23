#!/usr/bin/env python3
"""
services/pca-service/test_universal_scanner.py

Automatisierte Test- und Validierungs-Suite für den Universellen Stock Scanner und die
DCR/WCR-Scanner.

Die Tests rufen bewusst die Produktionsfunktionen auf (statt Formeln zu duplizieren), damit
Regressionen in der echten Logik erkannt werden. Abgedeckt:
 1. Schema-Introspektion (Parquet, Supabase, Computed, dynamische Supabase-Spalten)
 2. Filter-Operatoren inkl. `is_null`/`not_in` und Datums-`between`
 3. Null-Safety (Division-by-Zero, fehlende Stammdaten, negatives EPS)
 4. Cross-Column-Vergleiche
 5. Ausdrucks-Whitelist (blockt Tabellenfunktionen/Subqueries)
 6. Zero-Filter-Invarianz
 7. Rechengenauigkeit (DCR, Market Cap, Days to Earnings) über den Produktionscode
 8. Reales Parquet-Laden inkl. Bool-Typ
 9. WCR-Range-Konsistenz über Jahresgrenzen
10. Inline-Scanner-Specs
"""

import sys
import time
from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd

import universal_scanner as us
from universal_scanner import (
    get_dynamic_fields,
    _build_duckdb_where,
    _validate_expression,
    load_ticker_record,
    FilterCondition,
    KNOWN_ABBREVIATIONS,
)
from scanners import DcrScanner, WcrScanner, PhoenixScanner, parse_scanner_spec


def run_all_tests():
    print("=" * 70)
    print("🧪 Validierungs-Suite: Universal Stock Scanner / DCR / WCR")
    print("=" * 70)

    total_tests = 0
    passed_tests = 0

    def test(name: str):
        nonlocal total_tests
        total_tests += 1
        print(f"\n[Test {total_tests}] {name} ...", end=" ")

    def ok():
        nonlocal passed_tests
        passed_tests += 1
        print("✅ PASS")

    def fail(reason: str):
        print(f"❌ FAIL: {reason}")
        sys.exit(1)

    # ── Dimension 1: Schema-Introspektion ───────────────────────────────────
    test("D1: Dynamische Feld-Introspektion liefert Parquet-, Supabase- und Computed-Felder")
    fields = get_dynamic_fields()
    field_names = {f["name"] for f in fields}
    required = {
        "ticker", "close", "high", "low", "open", "volume",
        "shares_outstanding", "currency", "eps", "revenue", "earnings",
        "market_cap", "dcr", "wcr", "pe_ratio", "days_to_earnings",
        "price_to_sma50_pct", "price_to_sma200_pct",
    }
    missing = required - field_names
    if missing:
        fail(f"Fehlende Pflichtfelder: {missing}")
    for f in fields:
        if not f.get("name") or not f.get("type") or not f.get("source"):
            fail(f"Ungültige Feld-Definition: {f}")
    ok()

    test("D1b: Unbekannte Spalten erhalten leere Beschreibung ohne Crash")
    if KNOWN_ABBREVIATIONS.get("custom_new_indicator_xyz", "") != "":
        fail("Unbekanntes Feld sollte leere Beschreibung haben")
    ok()

    test("D1c: Supabase-Systemspalten werden ausgefiltert, neue Spalten dynamisch erkannt")
    saved = us._metadata_cache
    try:
        us._metadata_cache = (time.time(), {"TEST": {
            "ticker": "TEST", "shares_outstanding": 1.0, "new_fundamental": "ABC",
            "has_parquet": True, "created_at": "2026-01-01T00:00:00+00:00",
        }})
        catalog = dict(us._supabase_field_catalog())
        if "new_fundamental" not in catalog:
            fail(f"Neue Supabase-Spalte nicht dynamisch erkannt: {catalog}")
        for syscol in ("has_parquet", "created_at", "ticker"):
            if syscol in catalog:
                fail(f"Systemspalte '{syscol}' darf nicht im Katalog sein")
    finally:
        us._metadata_cache = saved
    ok()

    # ── Dimension 2: Filter-Operatoren ──────────────────────────────────────
    test("D2: Operatoren >=, between (numerisch + Datum), in, not_in, is_null, is_not_null, bool")
    vf = {f["name"]: f["type"] for f in fields}
    where_sql, applied = _build_duckdb_where([
        FilterCondition(field="ibd_rs", op=">=", value=90),
        FilterCondition(field="dcr", op="between", value=[80, 100]),
        FilterCondition(field="earnings", op="between", value=["2026-01-01", "2027-01-01"]),
        FilterCondition(field="currency", op="==", value="USD"),
        FilterCondition(field="currency", op="in", value=["USD", "EUR"]),
        FilterCondition(field="currency", op="not_in", value=["JPY"]),
        FilterCondition(field="eps", op="is_not_null"),
        FilterCondition(field="currency", op="is_null"),
        FilterCondition(field="minervini_trend_template", op="==", value=True),
    ], None, vf)
    for fragment in (
        '"ibd_rs" >= 90.0',
        '"dcr" BETWEEN 80.0 AND 100.0',
        '"earnings" BETWEEN \'2026-01-01\' AND \'2027-01-01\'',
        '"currency" = \'USD\'',
        '"currency" IN (\'USD\', \'EUR\')',
        '"currency" NOT IN (\'JPY\')',
        '"eps" IS NOT NULL',
        '"currency" IS NULL',
        '"minervini_trend_template" = TRUE',
    ):
        if fragment not in where_sql:
            fail(f"Fragment fehlt: {fragment}\nSQL: {where_sql}")
    ok()

    test("D2b: Ungültige Filter werden mit Fehler abgelehnt (leeres in, Wert-pflichtige Ops)")
    bad_conditions = [
        [FilterCondition(field="eps", op="in", value=[])],
        [FilterCondition(field="close", op=">")],
        [FilterCondition(field="unknown_field", op=">=", value=1)],
    ]
    for conds in bad_conditions:
        try:
            _build_duckdb_where(conds, None, vf)
            fail(f"Ungültiger Filter wurde akzeptiert: {conds}")
        except Exception:
            pass
    ok()

    # ── Dimension 3: Null-Safety (Produktionscode) ──────────────────────────
    test("D3: DCR/WCR bei Flat-Day sicher 50 % (Produktionsscanner)")
    flat = pd.DataFrame([{
        "timestamp": 1789444800, "open": 10.0, "high": 10.0, "low": 10.0,
        "close": 10.0, "volume": 100,
    }])
    dcr = DcrScanner().evaluate("TEST", flat)
    wcr = WcrScanner().evaluate("TEST", flat)
    if dcr["score"] != us.UNIVERSAL_SCANNER_FLAT_RANGE_SCORE or not dcr["matched"] == dcr["matched"]:
        fail(f"DCR Flat-Day sollte {us.UNIVERSAL_SCANNER_FLAT_RANGE_SCORE} sein, war {dcr['score']}")
    if wcr["score"] != us.UNIVERSAL_SCANNER_FLAT_RANGE_SCORE:
        fail(f"WCR Flat-Day sollte {us.UNIVERSAL_SCANNER_FLAT_RANGE_SCORE} sein, war {wcr['score']}")
    ok()

    test("D3b: Fehlende Supabase-Stammdaten -> market_cap/pe_ratio None")
    rec = load_ticker_record("AAPL", {})
    if not rec:
        fail("AAPL konnte nicht geladen werden")
    if rec.get("market_cap") is not None or rec.get("pe_ratio") is not None:
        fail(f"Erwartete None bei fehlenden Stammdaten: mc={rec.get('market_cap')} pe={rec.get('pe_ratio')}")
    ok()

    test("D3c: Negatives EPS -> pe_ratio None, market_cap bleibt berechnet")
    rec = load_ticker_record("AAPL", {"shares_outstanding": 1000, "eps": -2.5})
    if rec.get("pe_ratio") is not None:
        fail(f"pe_ratio bei negativem EPS sollte None sein: {rec.get('pe_ratio')}")
    if not rec.get("market_cap") or rec["market_cap"] <= 0:
        fail(f"market_cap sollte trotz negativem EPS berechnet sein: {rec.get('market_cap')}")
    ok()

    # ── Dimension 4: Cross-Column ───────────────────────────────────────────
    test("D4: Cross-Column-Vergleiche (close > ma_sma_50 > ma_sma_200)")
    cross_where, _ = _build_duckdb_where([
        FilterCondition(field="close", op=">", field_compare="ma_sma_50"),
        FilterCondition(field="ma_sma_50", op=">", field_compare="ma_sma_200"),
    ], None, vf)
    sample = pd.DataFrame([
        {"ticker": "BULL", "close": 150.0, "ma_sma_50": 140.0, "ma_sma_200": 130.0},
        {"ticker": "BEAR", "close": 120.0, "ma_sma_50": 130.0, "ma_sma_200": 140.0},
        {"ticker": "MIXED", "close": 145.0, "ma_sma_50": 140.0, "ma_sma_200": 150.0},
    ])
    db = duckdb.connect()
    db.register("stocks", sample)
    matched = [r[0] for r in db.execute(f"SELECT ticker FROM stocks WHERE {cross_where}").fetchall()]
    db.close()
    if matched != ["BULL"]:
        fail(f"Erwartet ['BULL'], erhalten {matched}")
    ok()

    # ── Dimension 5: Expression-Whitelist ───────────────────────────────────
    test("D5: Gültige Ausdrücke mit Klammern, AND/OR und Skalarfunktionen")
    for expr in (
        "(ibd_rs >= 90 OR dcr >= 95) AND market_cap >= 1000000000",
        "coalesce(eps, 0) > 1 AND close > ma_sma_50",
        'earnings >= \'2026-01-01\'',
    ):
        _validate_expression(expr, vf)
    where_expr, _ = _build_duckdb_where(None, "(ibd_rs >= 90 OR dcr >= 95) AND market_cap >= 1000000000", vf)
    if "market_cap >= 1000000000" not in where_expr:
        fail(f"Expression nicht korrekt übernommen: {where_expr}")
    ok()

    test("D5b: Whitelist blockt Tabellenfunktionen, Subqueries und Kommentare")
    malicious = [
        "ibd_rs >= 90; DROP TABLE stocks",
        "market_cap > 1e9 -- kommentar",
        "dcr >= 90; DELETE FROM stocks",
        "/* block comment */ ibd_rs > 50",
        "EXISTS (SELECT 1 FROM read_text('/etc/hostname'))",
        "read_parquet('/etc/passwd') IS NOT NULL",
        "1=1 UNION SELECT * FROM stocks",
    ]
    for bad in malicious:
        try:
            _build_duckdb_where(None, bad, vf)
            fail(f"Böswillige Eingabe akzeptiert: {bad}")
        except Exception:
            pass
    ok()

    # ── Dimension 6: Zero-Filter-Invarianz ──────────────────────────────────
    test("D6: Keine Filter -> 1=1 ohne künstliche Schwellen")
    empty_where, empty_applied = _build_duckdb_where(None, None, vf)
    if empty_where != "1=1" or empty_applied:
        fail(f"Leere Filter falsch: {empty_where} / {empty_applied}")
    ok()

    # ── Dimension 7: Rechengenauigkeit über Produktionscode ─────────────────
    test("D7: Rechengenauigkeit DCR / Market Cap / Days to Earnings")
    bar = pd.DataFrame([{
        "timestamp": 1789444800, "open": 90.0, "high": 100.0, "low": 80.0,
        "close": 95.0, "volume": 100,
    }])
    dcr = DcrScanner().evaluate("TEST", bar)["score"]
    if dcr != 75.0:
        fail(f"DCR Rechenfehler: {dcr}")
    target = (datetime.now(timezone.utc).date() + timedelta(days=15)).isoformat()
    rec = load_ticker_record("AAPL", {
        "shares_outstanding": 3844000, "eps": 6.5,
        "revenue": 100.0, "currency": "USD", "earnings": target + "T20:00:00+00:00",
    })
    if rec.get("days_to_earnings") != 15:
        fail(f"days_to_earnings falsch: {rec.get('days_to_earnings')}")
    if round(rec["close"] * 3844000, 2) != rec.get("market_cap"):
        fail(f"market_cap falsch: {rec.get('market_cap')} vs close={rec.get('close')}")
    ok()

    # ── Dimension 8: Reales Parquet inkl. Bool-Typ ──────────────────────────
    test("D8: Reales Laden (AAPL & MLAB) inkl. Bool-Typ minervini_trend_template")
    for ticker in ("AAPL", "MLAB"):
        rec = load_ticker_record(ticker, {"shares_outstanding": 1000000, "currency": "USD", "eps": 1.0})
        if not rec:
            fail(f"{ticker} konnte nicht aus Parquet geladen werden")
        if not isinstance(rec.get("minervini_trend_template"), bool):
            fail(f"{ticker}: minervini_trend_template ist {type(rec.get('minervini_trend_template')).__name__}, erwartet bool")
        if rec.get("market_cap") is None or rec["market_cap"] <= 0:
            fail(f"{ticker}: market_cap ungültig")
    ok()

    # ── Dimension 9: WCR-Range über Jahresgrenze ────────────────────────────
    test("D9: WCR evaluate_range == evaluate über ISO-Jahresgrenze")
    spec = [
        ("2025-12-29", 200.0, 90.0, 150.0),
        ("2025-12-30", 200.0, 90.0, 150.0),
        ("2025-12-31", 200.0, 90.0, 150.0),
        ("2026-01-01", 110.0, 100.0, 105.0),
        ("2026-01-02", 110.0, 100.0, 105.0),
    ]
    rows = []
    for ds, h, l, c in spec:
        rows.append({"timestamp": int(pd.Timestamp(ds, tz="UTC").timestamp()),
                     "open": c, "high": h, "low": l, "close": c, "volume": 1000})
    wdf = pd.DataFrame(rows)
    ws = WcrScanner()
    rng = ws.evaluate_range("TEST", wdf)
    for i in range(len(wdf)):
        a = ws.evaluate("TEST", wdf.iloc[: i + 1])["score"]
        b = rng["score"].iloc[i]
        if abs(a - b) > 0.01:
            fail(f"WCR Mismatch am {spec[i][0]}: evaluate={a} range={b}")
    ok()

    # ── Dimension 10: Inline-Scanner-Specs ──────────────────────────────────
    test("D10: parse_scanner_spec akzeptiert gültige und lehnt defekte Specs ab")
    if parse_scanner_spec("dcr:85") != ("dcr", {"cutoff": 85.0}):
        fail("dcr:85 falsch geparst")
    if parse_scanner_spec("dcr:short:15") != ("dcr", {"direction": "short", "cutoff": 15.0}):
        fail("dcr:short:15 falsch geparst")
    if parse_scanner_spec("wcr:long") != ("wcr", {"direction": "long"}):
        fail("wcr:long falsch geparst")
    for bad in ("dcr:abc", "dcr:short:abc", "dcr:1:2:3", ":85"):
        try:
            parse_scanner_spec(bad)
            fail(f"Defekte Spec akzeptiert: {bad}")
        except ValueError:
            pass
    ok()

    # ── Dimension 11: Phoenix RS-Scanner ────────────────────────────────────
    test("D11: Phoenix evaluate == evaluate_range, Schwellen, Custom-Params")
    def _mk_rs(rs):
        n = len(rs)
        return pd.DataFrame({
            "timestamp": [1789000000 + i * 86400 for i in range(n)],
            "open": [1.0] * n, "high": [1.0] * n, "low": [1.0] * n,
            "close": [1.0] * n, "volume": [1.0] * n, "ibd_rs": rs,
        })

    phx = PhoenixScanner()

    up = _mk_rs([10, 20, 30, 50, 70, 85, 90])
    if not phx.evaluate("TEST", up)["matched"]:
        fail("Phoenix: Anstieg 10->90 innerhalb 20 Tagen sollte matchen")
    if phx.evaluate("TEST", up)["score"] != 90.0:
        fail("Phoenix: Score sollte das aktuelle RS-Rating sein")

    late = _mk_rs([10, 20, 40, 50, 60, 70, 90])
    if phx.evaluate_range("TEST", late, days=5)["matched"].any():
        fail("Phoenix: zu alter Tiefpunkt (ausserhalb days=5) darf nicht matchen")
    if not phx.evaluate_range("TEST", late, days=6)["matched"].iloc[-1]:
        fail("Phoenix: Tiefpunkt innerhalb days=6 sollte matchen")

    high = _mk_rs([90, 90, 90])
    if phx.evaluate("TEST", high)["matched"]:
        fail("Phoenix: reine Hochphase darf nicht matchen")

    f = phx.evaluate_range("TEST", up)
    if bool(f["matched"].iloc[-1]) != bool(phx.evaluate("TEST", up)["matched"]):
        fail("Phoenix: evaluate/evaluate_range uneinig am letzten Bar")

    custom = _mk_rs([10, 15, 25, 35, 45, 55])
    if not phx.evaluate("TEST", custom, days=3, rs_from=40, rs_to=50)["matched"]:
        fail("Phoenix: Custom-Parameter days=3 rs_from=40 rs_to=50 sollte matchen")

    no_feat = up.drop(columns=["ibd_rs"])
    if phx.evaluate("TEST", no_feat)["matched"]:
        fail("Phoenix: fehlende ibd_rs-Spalte darf nicht matchen")
    ok()

    print("\n" + "=" * 70)
    print(f"🎉 Alle {passed_tests} von {total_tests} Tests erfolgreich bestanden!")
    print("=" * 70)


if __name__ == "__main__":
    run_all_tests()
