#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gap-Relation-Analyse
====================

Findet für eine Referenz-Aktie alle "Gap-Up"-Tage (Eröffnung mindestens X-mal so
weit über dem Vortages-Schluss wie die durchschnittliche Tagesrange) und sucht
dann im gesamten Aktien-Universum alle Aktien, die an DENSELBEN Tagen ebenfalls
einen solchen Gap-Up hatten.

Das Ergebnis wird nach der Anzahl gemeinsamer Gap-Tage gerankt.

Funktionsweise (vereinfacht):
  gap(t)   = (open(t) - close(t-1)) / close(t-1) * 100   [in Prozent]
  range(t) = (high(t) - low(t)) / close(t) * 100         [in Prozent]
  ADR20(t) = Mittelwert von range ueber die letzten 20 Tage
  Event(t) = gap(t) >= SCHWELLE * ADR20(t)   UND   gap(t) > 0

Hinweis: Der ADR20 wird hier NICHT neu berechnet, sondern direkt als
vorberechnetes 'adr_20'-Feature aus der Datei '1D_features.parquet' des
stock-data-node geladen (20-Tage-Mittel der Tagesrange in Prozent).

Aufruf:
  python gap_relation.py [TICKER]      # z.B. python gap_relation.py NVDA

Abhaengigkeiten (einmalig installieren):
  pip install pandas pyarrow
"""

import argparse
import os
import sys
import pandas as pd


# ============================================================
#  KONFIGURATION  --  hier nur diese Werte anpassen
# ============================================================
REF_TICKER    = "AAPL"    # Die Aktie, die untersucht wird
LOOKBACK_TAGE = 180       # Kalendertage zurueckschauen (180 = ca. 6 Monate)
GAP_MIN_ADR   = 1.0       # Schwelle: Gap muss mindestens X-mal ADR20 sein
PARQUET_DIR   = "/home/daniel/stock-data-node/data/parquet"
TOP_N         = 25        # Wie viele Ergebnisse in der Konsole angezeigt werden
OUTPUT_CSV    = "gap_relation_ergebnis.csv"   # Dateiname der Ergebnis-Tabelle
# ============================================================


def lade_ticker(ticker, verzeichnis):
    """Laedt die vorberechneten Feature-Daten (OHLCV + Indikatoren) eines Tickers.

    Nutzt direkt die Datei '1D_features.parquet' aus dem stock-data-node.
    Liefert None, wenn keine/zu wenige Daten vorhanden sind.
    """
    datei = os.path.join(verzeichnis, ticker, "1D_features.parquet")
    if not os.path.exists(datei):
        return None
    try:
        # Nur die benoetigten Spalten laden (schneller + weniger Speicher).
        df = pd.read_parquet(datei, columns=["timestamp", "open", "close", "adr_20"])
    except Exception:
        return None
    if df is None or df.empty or len(df) < 21:
        return None

    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def gap_events(df, schwelle=1.0):
    """Liefert die Menge der Timestamps mit einem Gap-Up >= schwelle * ADR20.

    Der ADR20 stammt hier direkt aus dem vorberechneten 'adr_20'-Feature.
    Lediglich der Gap (open vs. Vortages-close) wird aus OHLCV abgeleitet,
    da dafuer kein vorberechnetes Feature existiert.
    """
    close = df["close"]
    gap = (df["open"] - close.shift(1)) / close.shift(1) * 100.0
    adr20 = df["adr_20"]
    event = (gap >= schwelle * adr20) & (gap > 0) & adr20.notna()
    return set(df.loc[event, "timestamp"].tolist())


def datums_string(ts_liste, format_="%Y-%m-%d"):
    """Wandelt Unix-Timestamps in lesbare Daten um."""
    if not ts_liste:
        return ""
    return ", ".join(pd.to_datetime(sorted(ts_liste), unit="s").strftime(format_))


def ist_normaler_ticker(name):
    """True, wenn der Ticker mit einem alphanumerischen Zeichen beginnt.

    Sortiert virtuelle Ticker wie '$STATS.MARKET_BREADTH' aus dem Universum aus.
    """
    return bool(name) and name[0].isalnum()


def main():
    parser = argparse.ArgumentParser(
        description="Gap-Relation-Analyse: findet Aktien mit denselben "
                    "Gap-Up-Tagen wie ein Referenz-Ticker."
    )
    parser.add_argument(
        "ticker", nargs="?", default=REF_TICKER,
        help="Referenz-Ticker (Standard: %(default)s)",
    )
    args = parser.parse_args()
    ref_ticker = args.ticker.upper()

    verzeichnis = PARQUET_DIR
    if not os.path.isdir(verzeichnis):
        print(f"FEHLER: Parquet-Verzeichnis nicht gefunden: {verzeichnis}")
        sys.exit(1)

    # 1) Referenz-Aktie laden
    ref = lade_ticker(ref_ticker, verzeichnis)
    if ref is None:
        print(f"FEHLER: Keine (ausreichenden) Daten fuer '{ref_ticker}' gefunden.")
        sys.exit(1)

    ref_events = gap_events(ref, GAP_MIN_ADR)

    # Lookback-Fenster anwenden (nur die letzten X Tage betrachten)
    letztes_datum = ref["timestamp"].max()
    untergrenze = letztes_datum - LOOKBACK_TAGE * 24 * 3600
    ref_events = {ts for ts in ref_events if ts >= untergrenze}

    if not ref_events:
        print(f"'{ref_ticker}' hatte in den letzten {LOOKBACK_TAGE} Tagen keinen "
              f"Gap-Up >= {GAP_MIN_ADR}x ADR20.")
        sys.exit(0)

    print("=" * 74)
    print(f"Referenz-Aktie : {ref_ticker}")
    print(f"Lookback       : {LOOKBACK_TAGE} Tage")
    print(f"Schwelle       : {GAP_MIN_ADR}x ADR20")
    print(f"Gap-Up-Tage    : {len(ref_events)}  ->  {datums_string(list(ref_events))}")
    print("=" * 74)

    # 2) Alle uebrigen Ticker des Universums durchgehen
    alle_ticker = sorted(
        d for d in os.listdir(verzeichnis)
        if (os.path.isdir(os.path.join(verzeichnis, d))
            and ist_normaler_ticker(d)
            and d.upper() != ref_ticker)
    )

    ergebnisse = []
    gesamt = len(alle_ticker)
    print(f"\nDurchsuche {gesamt} Ticker ...\n")

    for i, ticker in enumerate(alle_ticker, 1):
        df = lade_ticker(ticker, verzeichnis)
        if df is None:
            continue
        ev = gap_events(df, GAP_MIN_ADR)
        gemeinsam = ref_events & ev
        if gemeinsam:
            ergebnisse.append({
                "ticker": ticker,
                "gemeinsame_gap_tage": len(gemeinsam),
                "gap_tage_gesamt": len(ev),          # ueber die gesamte Historie
                "gemeinsame_daten": sorted(gemeinsam),
            })
        if i % 500 == 0:
            print(f"  ... {i}/{gesamt} Ticker geprueft")

    # 3) Ranken nach Anzahl gemeinsamer Gap-Tage
    ergebnisse.sort(key=lambda x: x["gemeinsame_gap_tage"], reverse=True)

    print("\n" + "=" * 74)
    print(f"Top {TOP_N} verwandte Aktien (nach gemeinsamen Gap-Tagen):")
    print("=" * 74)
    print(f"{'Ticker':<8} {'gemeinsam':>10} {'eigene Gaps*':>13}   Gemeinsame Daten (Monat-Tag)")
    print("-" * 74)

    for r in ergebnisse[:TOP_N]:
        daten = datums_string(r["gemeinsame_daten"], format_="%m-%d")
        print(f"{r['ticker']:<8} {r['gemeinsame_gap_tage']:>10} "
              f"{r['gap_tage_gesamt']:>13}   {daten}")

    print("-" * 74)
    print("* 'eigene Gaps' = wie oft diese Aktie insgesamt (gesamte Historie) gap-up")
    print("  gegappt ist. Ein hoher Wert deutet auf eine generell zappelige Aktie hin.")

    # 4) Ergebnis als CSV speichern
    if ergebnisse:
        ausgabe_pfad = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_CSV)
        zeilen = [
            {
                "ticker": r["ticker"],
                "gemeinsame_gap_tage": r["gemeinsame_gap_tage"],
                "gap_tage_gesamt": r["gap_tage_gesamt"],
                "gemeinsame_daten": datums_string(r["gemeinsame_daten"]),
            }
            for r in ergebnisse
        ]
        pd.DataFrame(zeilen).to_csv(ausgabe_pfad, index=False)
        print(f"\nVollstaendige Liste ({len(ergebnisse)} Treffer) gespeichert in:")
        print(f"  {ausgabe_pfad}")
    else:
        print("\nKeine verwandten Aktien gefunden.")


if __name__ == "__main__":
    main()
