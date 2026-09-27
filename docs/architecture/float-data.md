# Free Float (aktuell + Historie)

## Warum überhaupt zwei Quellen

Der von QJM genutzte Anbieter **Massive** (ex-Polygon) hat einen eigenen Float-Endpunkt
(`GET /stocks/vX/float`, in allen Aktien-Plänen enthalten), liefert dort aber **nur einen
aktuellen Snapshot** je Ticker mit `effective_date`, `free_float` und `free_float_percent`.
Die Doku sagt dazu ausdrücklich *"Plan History: Not applicable to this endpoint"*.

Echte Historie gibt es stattdessen kostenlos bei der SEC (XBRL-Companyconcept-API). Deshalb
kombiniert der Sync beide Welten:

| Quelle | Inhalt | Historie | Kosten |
| :--- | :--- | :--- | :--- |
| Massive `/stocks/vX/float` | Free Float (Aktien + %) je US-Ticker, ~6.900 Treffer | ❌ nur Snapshot | im Massive-Plan enthalten |
| Massive `/v3/reference/splits` | Split-Faktoren (Normierung der SEC-Zahlen) | ✅ | im Plan enthalten |
| Massive `/vX/reference/financials` | Shares je Quartal (Legacy-Endpunkt; der neue `/stocks/financials/v1/...` antwortet mit 403) | ✅ ab ~2014 | im Plan enthalten |
| SEC `dei:EntityCommonStockSharesOutstanding` | ausstehende Aktien je 10-K/10-Q | ✅ ab ~2009 | frei |
| SEC `dei:EntityPublicFloat` | Public Float (USD, Nicht-Affiliate) von der 10-K-Cover-Page | ✅ ab ~2009 | frei |
| *abgeleitet* | `free_float_percent(Massive, aktuell) × shares_outstanding(SEC, historisch)` | ✅ | — (Schätzung, `is_estimate=true`) |

**Begriffe nicht verwechseln:** Der SEC-`EntityPublicFloat` ist nur der *Nicht-Affiliate*-Anteil und
damit systematisch höher als Massives Free Float, der zusätzlich 5 %-Halte, strategische Beteiligungen,
Mitarbeiterpläne usw. ausschließt. Beide Werte stehen deshalb getrennt in der Historientabelle
(`free_float` vs. `public_float_usd`) und werden nicht ineinander umgerechnet.

## Datenmodell (Migration 038)

| Struktur | Inhalt | Schreiber |
| :--- | :--- | :--- |
| `cda_master_universe.free_float` / `.free_float_percent` | aktueller Free Float (Aktien + %) | `float_sync.sync_current()` (täglich) |
| `cda_master_universe.float_effective_date` / `.float_source` / `.float_updated_at` | Stichtag, Herkunft, Sync-Zeitpunkt | dito |
| `cda_master_universe.cik` | SEC Central Index Key | Float-Backfill / SEC-Tooling |
| `cda_float_history` | 1 Zeile je (Ticker, Stichtag, Quelle): `massive_float`, `sec_xbrl`, `derived` | `float_sync` |
| `cda_float_series` (View) | eine Float-Zeitreihe je Ticker; gemessen schlägt abgeleitet | — |

`cda_float_history.shares_outstanding` und `free_float` sind auf die **heutige Split-Basis** normiert
(Split-Faktoren von Massive), damit die Reihe über Splits hinweg vergleichbar bleibt.

## Betrieb

Der Sync lebt im **stock-data-node** (dort liegen Massive-Key, Massive-Client und der 24/7-Loop):

    float_sync_loop()   startet 180 s nach dem Start und läuft danach alle
                        FLOAT_SYNC_INTERVAL_SEC (Default 86400 s = täglich)

Schalter (Umgebung): `FLOAT_SYNC_ENABLED` (Default true), `FLOAT_SYNC_INTERVAL_SEC`,
`FLOAT_SYNC_STARTUP_DELAY_SEC`, `SEC_USER_AGENT` (Pflicht-User-Agent der SEC), `SEC_MIN_INTERVAL_SEC`.

HTTP-Endpunkte (Port 8002):

| Aufruf | Wirkung |
| :--- | :--- |
| `POST /float/sync` | aktueller Massive-Snapshot → Master Universe + Historie |
| `POST /float/backfill` `{tickers?, limit?, fresh_days?, force?}` | SEC-Historie im Hintergrund (resumable: Ticker mit SEC-Zeilen jünger als `fresh_days` werden übersprungen) |
| `POST /float/backfill/cancel` | bricht den laufenden Backfill ab |
| `GET /float/status` | letzter Lauf + Backfill-Fortschritt |

Tägliche Wiederholung ist der Punkt: Massive liefert keine Historie, also **entsteht** sie hier —
jeder Lauf schreibt einen echten Messpunkt nach `cda_float_history`.

## Verbraucher

* **Chart-Topbar** (`chart_viewer/src/chart_viewer/fundamentals.py`): `Float: 92.1 %` bzw. `Float: -`,
  wenn keine Quelle greift. Weitere Felder derselben Zeile: Market Cap (Kurs × Shares), Shares Out,
  Währung, Next Earnings.
* **Universal Scanner**: `free_float`, `free_float_percent`, `float_effective_date`, `float_source`,
  `float_updated_at`, `cik` werden dynamisch aus `cda_master_universe` gelesen und sind damit
  sofort filterbar, z. B. `free_float <= 2e7 AND dollar_volume >= 5e7`.
* **Historie abfragen** (PostgREST/SQL):

      select * from cda_float_series where ticker = 'AAPL' order by as_of;
      select as_of, free_float, free_float_percent, is_estimate
        from cda_float_history
       where ticker = 'AAPL' and source = 'derived' order by as_of;

## Grenzen

* **ETFs haben keinen Free Float** (Massive liefert dafür keine Werte, z. B. IWM) — die Topbar zeigt `Float: -`.
* **OTC/ADR/ausländische Ticker** sind über Massive nur teilweise abgedeckt; die SEC-Historie gibt es nur
  für SEC-Filer (Ticker mit CIK).
* Die `derived`-Reihe ist eine **Schätzung**: sie unterstellt eine über die Zeit konstante Float-Quote.
  Sie ist als `is_estimate = true` markiert, damit Messwerte und Näherung nie verwechselt werden.
* Split-Normierung nutzt die Split-Listen von Massive; sehr alte oder exotische Splits können fehlen.
* Echte historische Float-*Quoten* je Quartal gibt es bei Massive nicht. Wer sie braucht, müsste
  einen spezialisierten Anbieter ergänzen (z. B. sec-api.io, `GET https://api.sec-api.io/float`,
  Historie ab 2011) — die Tabelle ist mit `source` bereits dafür vorbereitet.
