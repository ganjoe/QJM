# Earnings-Kalender (Vergangenheit + nächster Termin)

## Datenmodell

| Struktur | Inhalt | Schreiber |
| :--- | :--- | :--- |
| `cda_earnings_history` | **Vergangene** Termine, 1 Zeile je Ticker + Termin (report_date, fiscal_quarter_end/-year/-quarter, eps_actual, eps_estimate, eps_surprise_pct, source) | `manage_earnings_calendar` (SYNC/UPSERT), `scripts/earnings_backfill.py` |
| `cda_master_universe.next_earnings` (+ `next_earnings_source`) | **Nächster** Termin, vorwärtsgerichtet | SYNC, SET_NEXT, Backfill |
| `cda_master_universe.earnings` | Letzter gemeldeter Termin (Rückblick) | Metadata-Reconciler (yfinance), SYNC, Backfill |

Die Trennung ist bewusst: `earnings` war vorher ambivalent (je nach Quelle letzter **oder** nächster Termin).
Seit Migration 036 gilt: Zukunft steht ausschließlich in `next_earnings`, Vergangenheit in der Historientabelle.

## Quellen und Priorität

`next_earnings_source` dokumentiert die Herkunft; beim Schreiben gilt eine Rangfolge:

    manual (100) > confirmed (90) > yfinance (60) > nasdaq_zacks_estimate (40) > migrated_from_earnings (20)

Regel: Eine höherwertige, **noch zukünftige** Quelle wird von einer niedrigerwertigen nicht ersetzt
(`manage_earnings_calendar SYNC` schreibt nur `nasdaq_zacks_estimate`). Abgelaufene Termine werden immer ersetzt bzw.
geleert, damit `days_to_earnings` nicht dauerhaft negativ wird.

## Verbraucher

* `services/pca-service/universal_scanner.py`: `days_to_earnings` rechnet aus `next_earnings`, mit `earnings` als Fallback
  für Altdaten. `next_earnings` ist als Datumsfeld filterbar, `next_earnings_source` ist ein Systemfeld.
* `mcp/agent-pca/tools/analysis.ts`: Spalte *Earnings* zeigt `in Xd` / `heute` / `vor Xd`.
* Typischer Ablauf: `manage_earnings_calendar {action: LIST, days: 14}` → „wer berichtet demnächst",
  danach `ticker_evidence`/Chart-Viewer für die Kandidaten.

## Betrieb

    # Migration (idempotent, enthält NOTIFY pgrst reload schema)
    docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 < migrations/036_earnings_calendar.sql

    # Services nach Code-Änderungen
    bash restart.sh mcp-cda && bash restart.sh pca-service

    # Tiefe Historie (yfinance, läuft im stock-data-node-Container)
    docker exec -i stock-data-node python3 - --tickers META --history 12 < scripts/earnings_backfill.py
    docker exec -i stock-data-node python3 - --watchlist 00_positions --history 12 --only-missing < scripts/earnings_backfill.py

    # Tests
    docker exec -w /app qjm-pca-service python3 test_universal_scanner.py

Das Backfill-Skript wird per stdin in den Container gereicht (`python3 -`) — es muss dort nicht installiert werden.
Ohne `--tickers`/`--watchlist` läuft es nicht (kein implizites Universum). Live-Ausgabe mit `--dry-run`.

## Grenzen

* **Nasdaq** (Tool-Pfad, kein Key): nur US-Titel, Historie maximal ~4 Quartale je Aufruf, der nächste Termin ist
  eine Zacks-**Schätzung** („estimated to report"), also unbestätigt. Die Tabelle füllt sich über die Quartale.
* **yfinance** (Backfill-Pfad): tiefe Historie (bis ~20+ Quartale) inkl. exakter Termin-Zeit; einzelne Ticker
  liefern nichts (z. B. BRK B, CCXI, REP, TYC1) — das Skript meldet sie als „keine yfinance-Daten".
* `revenue_actual`/`revenue_estimate` sind Reservefelder; es ist noch keine Umsatzquelle angeschlossen.
* Nicht-US-Ticker: Nasdaq-Pfad greift nicht, yfinance über Symbol-Suffix (z. B. `LPK.DE`).
