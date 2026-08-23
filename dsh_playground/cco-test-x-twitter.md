# CCO X/Twitter Tool-Test – Protokoll

**Datum:** 23.08.2026 · **Tester:** DSH-Agent (Claude)
**Ziel:** Alle 5 X/Twitter-CCO-Tools (alle Action-Varianten) testen → Bugs + Optimierungspotenziale dokumentieren.
**Schema:** ID · Parameter · Erwartung · Ergebnis (Fehlermeldungen wortwörtlich) · Status · Notizen
**Statuslegende:** 🟢 OK · 🟡 TEILWEISE/VERDÄCHTIG · 🔴 FEHLER/BLOCKIERT · ❓ OFFEN

> **Nachtrag ~17:00:** Sandbox-Container wurde neu gestartet (Hostname b2c1a19562de → 45c8c6d30ee9), Originaldatei verloren → Protokoll neu aufgesetzt inkl. Phase 6 (Embedding-Re-Verifizierung).

---

## ⚡ Executive Summary (Kurzversion)

**Stand nach Embedding-Fix (~16:54):**
1. ✅ **B1 BEHOBEN:** Ollama-Ausfall weg — Embedding-Route über Switchyard-Gateway aktiv, **1071 Backlog-Posts ge-embeddet** (Pending 1071→0), Sync-Zyklen fehlerfrei.
2. 🔴 **B2 OFFEN (Regressions-Test nach Fix):** `discover_ticker_mentions` meldet weiterhin Serenity „Erste NVDA-Erwähnung: 19.8." — aber der 16.8.-Post (ID `238277ef…`, Ticker-Metadaten: `SPCX,AMD,NVDA`) wird nicht gezählt. **Bestätigt: Logik-Bug, kein Embedding-Problem.**
3. 🟡 **B13 NEU:** Default-Threshold 0.5 zu strikt für Multi-Word-Queries („quantum computing risk" → 0 Treffer; bei 0.3 → Treffer).
4. 🟡 **B14 NEU:** Semantischer Drift — generische „risk"-Posts matchen „quantum computing risk" hoch (vermutlich schwaches Embedding-Modell).
5. ❌ **ADD-Test weiterhin blockiert** — `X API 402 Payment Required` (Kredite noch nicht gekauft, bestätigt durch User). Nach Token-Kauf erneut testen.
6. 🔴 **B3 OFFEN:** `manage_x_sync STOP`-Zustandsinkonsistenz (ungetestet nach Fix).
7. 🔴 **B4 OFFEN:** REMOVE-Erfolgsmeldung bei nie-getrackten Usern (ungetestet nach Fix).
8. 🔴 **LLM-Kategorisierung: GESTOPPT** (separate Pipeline, LM Studio — nicht Teil des Embedding-Fix).
9. 🟢 **Semantische-Suche-Tiefentest (Phase 7):** Embeddings leisten echte Konzeptsuche (Serenity „quantum" → 10/10 relevant; Konzept-Query → echte Chokepoint-Calls). **2 neue Datenqualitäts-Bugs:** B15 (Date ≠ published_at), B16 (leere tickers bei alten Posts).

---

## Scorecard

| Tool | Aktionen | 🟢 | 🟡 | 🔴 | Bewertung |
|---|---|---|---|---|---|
| `manage_x_sync` | START (4), STOP (1), STATUS (4) | 4 | 2 | 2 | 🟡 funktional, aber Zustands- & Validierungs-Bugs |
| `show_x_content` | DATABASE (5), ONLINE (4) | 4 | 3 | 1 | 🟡 gut, Sortierung & Fehlerhandling polieren |
| `search_influencer_posts` | READ (7), SHOW (1), READ_IDS (1) | 7 | 3 | 0 | 🟢 robust, `threshold`-Semantik unklar |
| `discover_ticker_mentions` | global (2), gefiltert (2), dump (1), start_date (1) | 2 | 4 | 1 | 🔴 Kernlogik (Erstnennung) fehlerhaft |
| `manage_influencers` | LIST (3), ADD (4), REMOVE (2) | 3 | 1 | 2 | 🔴 ADD blockiert (X-API-402), REMOVE unvalidiert |

---

## Phase 0 – Setup & Baseline

### 0.2 `manage_influencers LIST`
- **Ergebnis:** 13 Influencer, sauber formatiert.
- **Status:** 🟢
- **Notizen (Datenqualität):**
  - `@pradeepbonde (stockbee)` UND `@stockbee (Brenda English Manes)` → **zwei Accounts mit gleichem Anzeigennamen „stockbee"** (vermutlich Fehlzugeordnung oder Duplikat).
  - `@stockbee` hat **0 Posts** → Account ohne Daten, Sync scheinbar nie erfolgreich.

### 0.3 `manage_x_sync STATUS`
- **Ergebnis:** Ausführlicher Report (Loop-Zyklen, Pipeline, Log, Influencer-Übersicht).
- **Status:** 🟢 (Funktion) / 🔴 (enthaltene Fehler)
- **Notizen:**
  - 🔴 Wiederholte `[ERROR] Embedding-Fehler: error sending request for url (http://ollama:11434/v1/embeddings): client error (Connect): dns error` — **in jedem Zyklus** (vor Fix).
  - 🟡 Inkonsistente Endpunkt-Konfiguration (`ollama` vs. `localhost`) erschwerte Diagnose.
  - 1062+ Posts „Pending (ohne Embedding)"; LLM-Kategorisierung: GESTOPPT.

### 0.4 `show_x_content DATABASE` (limit 5, ohne Filter)
- **Status:** 🟡
- **Bugs:**
  - 🔴 **Nicht chronologisch sortiert:** 15:36:10 → 15:35:41 → **15:36:23** → 15:34:19 → 15:31:33 (wiederholt in 4.3).
  - 🟡 Datumsformat deutsch — andere Tools nutzen `M/D/YYYY` → Inkonsistenz.

### 0.5 `search_influencer_posts READ` (leere Query)
- **Ergebnis:** gleiche 5 Posts wie 0.4 (Konsistenz zwischen Tools ✅).
- **Status:** 🟢 / 🟡 (Datum ohne Zeit; UUID-IDs geliefert — gut für READ_IDS)

---

## Phase 1 – Read-Pfade

### 1.1 `show_x_content DATABASE` · `username=@serenity, days_back=7, limit=5`
- **Status:** 🟢 — **Fuzzy-Matching über Anzeigennamen funktioniert** (`@serenity` → @aleabitoreddit).

### 1.2 `show_x_content ONLINE` · 4 Varianten
| Variante | Ergebnis | Status |
|---|---|---|
| `tweet_id="1"` | `Error: Fehler: Tweet not found` | ❓ alte Tweets nicht abrufbar (API-Limit?) |
| `tweet_id="https://x.com/jack/status/1"` | `Error: Fehler: Tweet not found` (gleiche) | ❓ |
| `tweet_id="999999999999999999999"` (21 St.) | `Error: Fehler: X API failed: 400` | 🔴 rohe API-Fehlermeldung, keine Client-Validierung |
| `tweet_id="2091488091110531106"` (echt) | ✅ Live-Fetch erfolgreich | 🟢 |
- **Notizen:** 🟡 Ausgabe zeigt `Author ID: 1940360837547565056` (numerisch) statt Handle.

### 1.3 `search_influencer_posts READ` · `query="AMD", authors=["serenity"], threshold=0.8`
- **Status:** 🟢 — 5 plausibel AMD-relevante Posts.

### 1.4 `search_influencer_posts READ` · `return_mode=full_text` / `ids_only`
- **Status:** 🟢 — `external_id` = echte Tweet-ID → Brücke zu `show_x_content ONLINE`.

### 1.5 `search_influencer_posts READ_IDS` · 2 UUIDs
- **Status:** 🟢 — exakte 2 Posts, Volltext + Metadaten.

### 1.6 `search_influencer_posts SHOW` · `query="NVDA", limit=5`
- **Status:** 🟢 — `Success. 5 posts dumped to chat. [STOP]`
- **Notizen:** 🟡 Response ohne IDs/Details → schwer nachvollziehbar.

### 1.7 `discover_ticker_mentions` · `keywords=["NVDA"]` (global)
- **Ergebnis:** 5 „Erstnennungen" — **alle zwischen 5.8. und 23.8.2026** (letzte 3 Wochen!).
- **Status:** 🔴 (Logik)
- **Bug-Details:**
  - Beschreibung verspricht „gesamte Post-Historie" — alle Treffer < 3 Wochen alt → **undokumentiertes Fensterlimit**.
  - **Konkreter Widerspruch:** Serenity „erstmals NVDA: 19.8.2026 02:51:57" — aber der 16.8.-Post (ID `238277ef-55ea-42d8-b7b3-581280dc3366`) enthält `$NVDA` (Metadaten: `tickers:["SPCX","AMD","NVDA"]`, published_at 2026-08-16T03:48:31Z).
- **Verdacht:** JOIN/Filter-Problem zwischen Post-Historie und Ticker-Metadaten.

### 1.7b `discover_ticker_mentions` · `authors=["serenity"]`
- **Status:** 🟢 (Filter-Mechanik) / 🔴 (Inhalt s. 1.7)

### 1.8 `discover_ticker_mentions` · `dump_to_chat=true`
- **Status:** 🟡 — identische Response wie ohne Parameter; keine Bestätigung des Chat-Dumps.

### 1.9 (Zusatz) `discover_ticker_mentions` · `start_date="2026-01-01"`
- **Ergebnis:** unverändert (19.8.) → 16.8.-Post wird auch mit weitem Fenster **nicht** gezählt.
- **Status:** 🔴 bestätigt B2 (echte Logiklücke, nicht nur Fensterproblem).

---

## Phase 2 – Write-Pfade (Influencer-DB)

### 2.1a `manage_influencers ADD` · `@test_cco_probe` (existiert nicht)
- **Ergebnis:** `Error: Fehler: User @test_cco_probe not found on X.`
- **Status:** 🟢 — Validierung vor DB-Schreibzugriff.

### 2.1b `manage_influencers ADD` · `@elonmusk` (Duplikat)
- **Ergebnis:** (vor Fix) Ollama-Embedding-Fehler → 🔴 kein Duplikatcheck; Embedding-Abhängigkeit war harte Blockade.
- **Nach Fix:** X-API-402 (Kredite) — Pfad bis X-Resolution ok.

### 2.1c `manage_influencers ADD` · `elonmusk` (ohne `@`)
- **Status:** 🟢 (Format-Toleranz) — `@`-Prefix optional.

### 2.1d `manage_influencers ADD` · `"not a valid user name"`
- **Ergebnis:** `Error: Fehler: X API failed to resolve user: 400`
- **Status:** 🟡 abgelehnt, aber rohe HTTP-400 ohne Erklärung.

### 2.2 `manage_influencers LIST` (nach ADD-Versuchen)
- **Status:** 🟢 — keine halben DB-Einträge (Atomarität).

### 2.3 `manage_influencers REMOVE` · nie-getrackte User
- **Ergebnis:** `Influencer @test_cco_probe wurde deaktiviert (Soft-Delete). Posts bleiben erhalten.`
- **Status:** 🔴 **irreführende Erfolgsmeldung ohne Existenzprüfung** (gleiche Meldung bei `@not_tracked_user_xyz`).
- **Notizen:** REMOVE = Soft-Delete (dokumentiert, ok).

### 2.4 Vollrunden-Test ADD → LIST → REMOVE
- **Status:** ❌ **nicht durchführbar** — blockiert durch X-API-402 (Kredite). Nach Token-Kauf erneut testen.

---

## Phase 3 – Sync

### 3.1 `manage_x_sync START` · `username=all, limit=5`
- **Status:** 🟢 — Massen-Sync gestartet, sequenziell im Hintergrund.

### 3.2 `manage_x_sync STATUS` (direkt danach)
- **Ergebnis:** `Aktuell aktive User-Syncs: Keine` — trotz laufendem Massen-Sync!
- **Status:** 🔴 **Zustandsinkonsistenz (B3).**

### 3.3 Abschluss des Massen-Syncs
- **Ergebnis:** ✅ abgeschlossen (~4 Min., 13 Influencer). Total Posts 15020→15025.
- **Status:** 🟢 / 🔴 (neue Posts in „Pending", da Embedding kaputt war — vor Fix)
- **Notizen:** 🟡 versprochene Benachrichtigung kam nicht proaktiv an.

### 3.4 `manage_x_sync STOP`
- **Ergebnis:** `Es laufen aktuell keine Syncs.`
- **Status:** 🔴 **widerspricht laufendem Sync** — STOP/START-Registry getrennt (B3).
- **Notizen:** 🟡 STOP global, kein per-User-Stop.

### 3.5 `manage_x_sync START` · `start_time="2027-01-01T00:00:00Z"` (Zukunft)
- **Ergebnis:** erfolgreich gestartet — **keine Validierung**, 0 Posts gespeichert.
- **Status:** 🟡 (B8).

### 3.5b Doppel-START während aktiver Sync
- **Ergebnis:** `Ein Hintergrund-Sync für @elonmusk läuft bereits.`
- **Status:** 🟢 — Duplikatschutz pro User funktioniert.

---

## Phase 4 – Robustheit

### 4.1 `show_x_content` · `action="DELETE"` (ungültig)
- **Ergebnis:** `MCP error -32602: Invalid option: expected one of "DATABASE"|"ONLINE"`
- **Status:** 🟢 — exzellente Schema-Fehlermeldung.

### 4.2 `search_influencer_posts` · `threshold=0` vs. `threshold=1`
- **Ergebnis:** **identische Top-3** — Parameter-Effekt unbeobachtbar (B6).
- **Status:** 🟡

### 4.3 `show_x_content DATABASE` · `limit=0`
- **Ergebnis:** 10 Posts (Default) — leises Defaulting.
- **Status:** 🟡 + Sortierungs-Bug wiederholt.

### 4.4 `search_influencer_posts` · unexistenter Author
- **Ergebnis:** `Keine Ergebnisse gefunden.`
- **Status:** 🟢

---

## Phase 5 – Auswertung (siehe Scorecard & Bug-Liste unten)

---

## Phase 6 – Embedding-Re-Verifizierung (nach Fix, 23.8. ~16:54–17:00)

**Kontext:** Ollama-Ausfall behoben, Embedding-Route über Switchyard-Gateway (`host.docker.internal:4000/v1/embeddings`) ergänzt.

### 6.1 Pipeline-Status (manage_x_sync STATUS)
- **Ergebnis:**
  - `[16:54] [EMBEDDING] 1071 Posts erfolgreich ge-embeddet (status: embedded).`
  - **Pending: 1071 → 0** (Backlog in einem Zug abgearbeitet)
  - Embedded: 2562 → 3633 (+1071 — exakt die Backlog-Größe)
  - Discovery-Zyklen 45–48 laufen **ohne Embedding-Fehler**
- **Status:** 🟢 **B1 BEHOBEN** ✅

### 6.2 Semantische Suche nach Fix
| Query | Threshold | Ergebnis |
|---|---|---|
| `quantum` (1 Wort) | 0.5 (Default) | 🟢 3 relevante Treffer (@paradislabs) |
| `quantum risk` (2 Wörter) | 0.5 (Default) | 🟡 Treffer, Qualität gemischt |
| `quantum computing risk` (3 Wörter) | 0.5 (Default) | 🔴 **keine Ergebnisse** |
| `quantum computing risk` (3 Wörter) | 0.3 | 🟡 Treffer — aber **semantischer Drift**: „It is a real risk, that's underappreciated" (generisch) rangiert oben |
| `AMD` | 0.5 | 🟢 funktioniert |

- **Status:** 🟡 Embedding-Funktion ✅, aber **B13** (Default-Threshold zu strikt für Multi-Word) + **B14** (semantischer Drift / schwaches Modell) neu.

### 6.3 `discover_ticker_mentions` B2-Regressions-Test
- **Ergebnis:** **unverändert** — „Erstmals erwähnt: 19.8.2026", 16.8.-Post wird nicht gezählt.
- **Status:** 🔴 **B2 bleibt offen** — **kein Embedding-Problem, reine Logik-Bug.**

### 6.4 `manage_influencers ADD` nach Fix
- **Ergebnis:** `X API failed to resolve user: 402` (Payment Required)
- **Status:** ❌ **erwarteter Blocker** (Kredite nicht gekauft, bestätigt). Nach Token-Kauf erneut testen.

---

## 🐛 Bug-Liste (konsolidiert)

| # | Schwere | Tool | Problem | Status |
|---|---|---|---|---|
| B1 | 🔴 System | Embedding-Pipeline | Ollama unerreichbar → ADD blockiert, 1071 Posts ohne Embedding | ✅ **BEHOBEN** (16:54, Route via Switchyard) |
| B2 | 🔴 | `discover_ticker_mentions` | „Erstnennung" übersieht ältere, korrekt getaggte Posts (Serenity: 16.8. vs. gemeldete 19.8.) | 🔴 **OFFEN** (Regressions-Test nach Fix: unverändert) |
| B3 | 🔴 | `manage_x_sync STOP`/`STATUS` | Zustandsinkonsistenz: STOP meldet „keine Syncs" bei laufendem Sync | 🔴 OFFEN |
| B4 | 🔴 | `manage_influencers REMOVE` | Erfolgsmeldung für nie-getrackte User (keine Existenzprüfung) | 🔴 OFFEN |
| B5 | 🟡 | `show_x_content DATABASE` | Ausgabe nicht chronologisch | 🔴 OFFEN |
| B6 | 🟡 | `search_influencer_posts` | `threshold` 0 vs. 1 → gleiche Ergebnisse; Effekt unbeobachtbar | 🔴 OFFEN (verschärft durch B13) |
| B7 | 🟡 | `show_x_content ONLINE` | Ungültige ID → rohe `X API failed: 400` | 🔴 OFFEN |
| B8 | 🟡 | `manage_x_sync START` | Zukunftsdatum `start_time` ohne Warnung akzeptiert | 🔴 OFFEN |
| B9 | 🟡 | `discover_ticker_mentions` | `dump_to_chat=true` ohne Bestätigung (still) | 🔴 OFFEN |
| B10 | 🟡 | `manage_influencers ADD` | Kein Duplikatcheck | 🔴 OFFEN (Nach-Test nach Token-Kauf) |
| B11 | 🟡 | `show_x_content ONLINE` | `Author ID` (numerisch) statt Handle | 🔴 OFFEN |
| B12 | 🟡 | DB-Belegung | `@pradeepbonde` & `@stockbee` gleicher Anzeigename; `@stockbee` 0 Posts | 🔴 OFFEN |
| B13 | 🟡 NEU | `search_influencer_posts` | Default-Threshold 0.5 zu strikt für Multi-Word-Queries (0 Treffer vs. Treffer bei 0.3) | 🔴 OFFEN |
| B14 | 🟡 NEU | `search_influencer_posts` | Semantischer Drift — generische Posts matchen spezifische Queries hoch (schwaches Embedding-Modell?) | 🔴 OFFEN |
| B15 | 🟡 NEU | `search_influencer_posts` | `Date:`-Feld = Ingest-/Sync-Datum, ≠ `published_at` (bis 2,5 Monate abweichend) | 🔴 OFFEN |
| B16 | 🟡 NEU | `search_influencer_posts` / Pipeline | `tickers:[]` leer bei alten Posts trotz `$TICKER` im Text (inconsistent Extraktion) | 🔴 OFFEN |

**Externe Blocker (nicht CCO-Bugs):**
- ⚫ X API 402 (Payment Required) — Kredite nicht gekauft (bestätigt) → blockiert ADD + User-Resolution.
- ⚫ LLM-Kategorisierung GESTOPPT (separate Pipeline, LM Studio).

---

## 💡 Optimierungspotenziale

**Architektur/Robustheit**
1. **Zentrale Sync-Registry** für START/STATUS/STOP; per-User `STOP username=...`
2. **Graceful Degradation statt Hard-Fail:** `ADD` bei Embedding-Ausfall speichern + Warn-Flag
3. **X-API-Fehlerkontext:** 402 → „Kredite/Plan prüfen" statt roher Meldung

**Logik/Korrektheit**
4. `discover_ticker_mentions`: über **gesamte** Historie rechnen (oder Fenster parametrierbar + dokumentiert); Erstnennungs-Query auf Ticker-Metadaten über **alle** Posts prüfen (B2)
5. `REMOVE`/`ADD`: Existenz- & Duplikatcheck vor Statusmeldung (B4, B10)
6. `DATABASE`-Ausgabe: strikt `published_at DESC` (B5)
7. **Threshold-Semantik:** Ähnlichkeitsscore pro Treffer ausgeben (B6/B13); Default für Multi-Word-Queries anpassen oder Query-Embedding verbessern (B14)

**UX/Fehlermeldungen**
8. Rohe `X API failed: 400`-Fehler durch kontextreiche Meldungen ersetzen (B7)
9. Datumsformate vereinheitlichen (ISO-8601)
10. `SHOW`-Dump: Response mit Anzahl + IDs anreichern
11. `ONLINE`: Handle statt Author-ID (B11); alte Tweets mit Hinweis statt nacktem „not found"
12. `limit=0` → explizites Verhalten dokumentieren
13. Benachrichtigung bei Massen-Sync-Abschluss wirklich liefern (3.3)

---

## Phase 7 – Semantische-Suche-Tiefentest (Serenity, ~17:10)

### 7.1 Einzelwort „quantum" (authors=serenity, threshold 0.3, full_text)
- **Ergebnis:** 10/10 Treffer tatsächlich quantum-bezogen (Photonics-vs-Quantum, MSFT-Quantum/Riber, ALRIB-Quantum-Kunde, Quantum-Dot-Laser, „quantum sector still far out").
- **Status:** 🟢 hohe Präzision, kein Drift.

### 7.2 Konzept-Query „bottlenecks that could constrain AI data center buildout" (threshold 0.35)
- **Ergebnis:** fand ihre echten Chokepoint-Calls (InP-Substrat-Shortage, HBM, AI-Supply-Chain) — **echte semantische Konzeptsuche** (Query-Phrase steht wörtlich in keinem Post).
- **Status:** 🟢 ✅ Embeddings leisten echte Konzeptsuche (kein Keyword-Fallback).
- **Aber:** 1 Falsch-Positiv — „an apple a day keeps the bottleneck away" (Witz) matcht nur wegen „bottleneck" → bestätigt **B14** (Oberflächenvokabular wird überwertet).

### 7.3 Metadaten-Qualität (beide Queries)
- **Status:** 🔴 **zwei neue Bugs:**
  - **B15:** `Date:`-Feld im Output = **Ingest/Sync-Datum**, nicht `published_at` — z. B. angezeigt `5/18/2026` vs. `published_at 2026-03-03` (bis 2,5 Monate abweichend). Sortierung/Filterung nach `Date` ist damit fehlerhaft.
  - **B16:** `tickers:[]` **leer** bei alten Posts (Mär–Jul 2026) trotz `$NVDA/$MSFT/$ALRIB/$LITE/$POET` im Text; aktuelle Posts haben gefüllte Arrays → **inconsistent Ticker-Extraktion**, schwächt alle Ticker-Tools (inkl. `discover_ticker_mentions`).

---

## Anhang: Testumgebung
- 13 Influencer in DB, 15.029 Posts; 60s-Periodic-Sync aktiv; LLM-Kategorisierung gestoppt.
- Phase 0–5: 23.8.2026, 15:36–15:45 · Phase 6: ~16:54–17:00 (nach Embedding-Fix).
- Embedding-Route: `http://host.docker.internal:4000/v1/embeddings` (Switchyard-Gateway → Ollama).
- Keine destruktiven Änderungen an der Influencer-DB (REMOVE-Tests nur auf nie-existierenden Namen; ADD-Versuche abgelehnt/blockiert).
