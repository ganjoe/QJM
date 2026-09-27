# X-Ingestion: Credit-Audit & Kostenbremse (24.09.2026)

Anlass: TwitterAPI.io-Guthaben war am 24.09. um 17:02 UTC aufgebraucht
(`Credits is not enough.Please recharge`, Kontostand -1.482 Credits). Der
Ingestion-Loop lief danach stündlich in 12 fehlschlagende Requests und der
Such-Cursor blieb stehen.

## Messmethode

- Server-Logs des CCO-Containers (`[X Ingestion]`) über den Container-Lebenslauf
- Interne Telemetrie (`xIngestionStats`) + `manage_sync_pipeline STATUS`
- Provider-Kontostand (`GET /oapi/my/info`, `recharge_credits`) als Gegenprobe
- Preisannahme laut Code/Doku: 100.000 Credits = $1, 15 Credits pro geliefertem Tweet

## Befund vorher (20.09. 20:57 – 24.09. 21:03)

| Quelle | Kosten | Anteil |
|---|---|---|
| Stündliche Suche (`tweet/advanced_search`) | ~$0.27/Tag = **~$8.10/Monat** | 58% |
| Timeline-Abgleich (`user/tweet_timeline`) | $1.49 pro Lauf, wöchentlich = **~$6.40/Monat** | 42% |
| **Summe** | **~$14.50/Monat ≈ 1.45 Mio. Credits** | |

Der eine große Abgleich-Lauf am 21.09. (12:21–12:36) bezog **9.961 Tweets für
1.256 neue Posts = 87% Duplikate** zum vollen Tweet-Preis (149.420 Credits /
$1.4942) – 37% des gesamten Verbrauchs im Messfenster.

Ursachen:

1. **Abgleich paginierte blind durchs ganze Fenster** (`X_RECONCILE_LIMIT = 2000`,
   7-Tage-Rückblick), obwohl die stündliche Suche 87% davon längst bezahlt hatte.
2. **Kein Budget-Guard**: kein Zyklus hatte eine Obergrenze.
3. **402 ohne Sperre**: nach dem Leerlaufen 12 Fehlversuche pro Stunde.
4. **15-Minuten-Überlappung** bei nur 60-Minuten-Intervall = ~25% Mehrvolumen
   (jeder erneut gelieferte Tweet kostet erneut 15 Credits).
5. **Telemetrie-Fehler**: die Such-Logzeile druckte die Zyklus-Summe inklusive
   Abgleich-Credits – die Verschwendung war dadurch unsichtbar.

## Umgesetzte Maßnahmen

| Maßnahme | Wirkung |
|---|---|
| Abgleich stoppt, sobald eine Seite nur bekannte Posts enthält (`X_RECONCILE_STOP_ON_KNOWN_PAGE`) | beendet das 87%-Duplikat-Paginieren |
| max. 3 Seiten pro Influencer (`X_RECONCILE_MAX_PAGES_PER_USER`) | harte Obergrenze pro Account |
| Abgleich-Fenster auf 3 Tage begrenzt (`X_RECONCILE_MAX_LOOKBACK_SEC`) | kein 7-Tage-Sweep nach Ausfällen |
| Budget-Guard `X_MAX_CREDITS_PER_CYCLE = 60000` ($0.60) | ein Zyklus kann das Guthaben nicht mehr leeren |
| 402-Sperre `X_PAYMENT_BLOCK_MS = 1800000` in `twitterApiIoFetch` | keine Fehlversuchs-Schleife gegen leere Kasse |
| Überlappung 900s → 300s | −17% Mehrvolumen pro Lauf |
| Kosten-Telemetrie pro Quelle (Suche/RT/Abgleich/Liveness) | Verschwendung ist im STATUS sichtbar |

## Messung nachher (Testlauf 24.09. 21:49–21:54, gleiche 119 Influencer)

| Kennzahl | vorher (21.09.) | nachher (24.09.) |
|---|---|---|
| Tweets geladen | 9.961 | **2.597** (−74%) |
| Kosten | $1.4942 | **$0.3896** (−74%) |
| Laufzeit | 15:04 min | 4:06 min |
| Verhältnis geladen/neu | 7,9 : 1 | 29 : 1 (struktureller Boden) |

Gegenprobe Provider-Kontostand: 989.593 → 950.218 Credits = **39.375 Credits**
in 5,5 Minuten (Abgleich 38.955 + Suche 225). Die interne Schätzung stimmt
damit auf <0,5% – die Telemetrie ist jetzt belastbar.

Der Abgleich kostet strukturell mindestens ~20 Tweets × 119 Influencer
(2.380 Tweets ≈ 35.700 Credits) pro Lauf, weil von jedem Timeline-Kopf
mindestens eine Seite geprüft wird. Bei wöchentlichem Lauf: **~$1.70/Monat**
statt $6.40.

## Erwartete neue Kostenlage

- Suche: ~$7.50/Monat (Inhalt bestimmt die Kosten, nicht die Abfragefrequenz)
- Abgleich: ~$1.70/Monat
- **Summe ~$9.20/Monat statt ~$14.50/Monat → −37%**

## Wichtig für weitere Einsparungen

Die Abrechnung erfolgt **pro geliefertem Tweet**. Ein größeres Suchintervall
(`X_SEARCH_INTERVAL_SEC`) spart daher fast nichts – es kommen dieselben Posts
an, nur gebündelt. Wirklich wirksam sind nur:

1. **Retweets ausschließen** – im Testlauf 1.275 von 8.760 Credits (14,6%).
   Dafür die zweite Query pro Chunk (`filter:nativeretweets`) deaktivieren.
2. **Influencer-Liste verschlanken** (`x_users`, derzeit 119 aktive Accounts
   ≈ 1.400 Posts/Tag). Jeder entfernte Account spart direkt proportional.
3. Überlappung weiter senken (`X_SEARCH_OVERLAP_SEC`, 300s = 8% Aufschlag;
   darunter steigt das Risiko, Rand-Posts dem Abgleich zu überlassen).

## Inventar: Wer lädt automatisch Tweets? (geprüft 24.09.2026)

Nur **`llm-gw-mcp-cco`** nutzt den TwitterAPI.io-Key bzw. die offiziellen
X-Credentials (alle laufenden Container geprüft). Innerhalb des Containers gibt es
genau **einen** periodischen Loop (`runXIngestionLoop`, Start beim Container-Boot),
in dem zwei Mechanismen aktiv sind:

| # | Mechanismus | Auslöser | Rhythmus | Kosten | Status |
|---|---|---|---|---|---|
| 1 | Stündliche Suche `tweet/advanced_search` | Ingestion-Loop | `X_SEARCH_INTERVAL_SEC` = 3600s | ~$0.27/Tag | **aktiv** |
| 2 | Timeline-Abgleich `user/tweet_timeline` | `last_reconcile` > 7 Tage **oder** Suchlücke > 24h | ~wöchentlich | $0.39/Lauf (vorher $1.49) | **aktiv** |
| 3 | Liveness-Check `user/batch_info_by_ids` + Timeline-Sweep | nur bei `X_INGESTION_MODE=timeline` | 21600s | 119 × 18 Credits + Seiten | inaktiv (Mode = search) |
| 4 | Initial-Backfill (200 Posts/Account) | `manage_influencers` ADD | einmalig je Account | 3.000 Credits/Account | automatisch bei Add |
| 5 | `user/info` (Handle auflösen) | Cache-Miss in `x_users` | selten | 18 Credits | automatisch |
| 6 | `sync_influencer_posts` | Tool-Aufruf | manuell | 200 Posts ≈ $0.03 | manuell |
| 7 | `search_x_ticker` | Tool-Aufruf | manuell | 15 Credits/Tweet, Fenster wird automatisch geweitet | manuell |
| 8 | `show_x_content` ONLINE | Tool-Aufruf | manuell | 1 Tweet | manuell |
| 9 | `sync_x_bookmarks` | Tool-Aufruf | manuell | offizielle X API (eigene Quote), nicht TwitterAPI.io | manuell |
| 10 | `scripts/find_alpha_influencer.ts` (Followings-Crawl) | Skript | manuell | Followings-Preis | manuell |

Zusätzlich zu beachten:

- **Jeder Container-Restart startet sofort einen Zyklus** – am 20.09. sechsmal in
  einer Stunde (je ~$0.01–0.02).
- **Bulk-Add ist der teuerste Einmal-Effekt**: 101 `influencer_added`-Events
  (letzte am 19.09.); ein Batch von 30 Accounts zieht automatisch
  30 × 200 × 15 = **90.000 Credits ($0.90)** im Hintergrund. Steuerbar über
  `X_INITIAL_BACKFILL_LIMIT` und `X_INITIAL_SYNC_CONCURRENCY`.
- Keine aktiven WIQ-Zeitpläne, keine Cron-/Systemd-Timer, die X-Tools aufrufen
  (geprüft). Manuelle Tools kosten nur, wenn ein Agent sie aufruft.

## Bewertung: Was ist verzichtbar? (Datenstand 24.09.2026, 7-Tage-Fenster)

| Kandidat | Datenlage | Urteil |
|---|---|---|
| **Retweet-Query** der Suche | 14,6% der Suchkosten; Ticker-Trefferquote 17,9% vs. 49,3% bei Originalen; nur 6,1% der First Mentions | **verzichtbar** – ein RT ist nicht die Aussage des Influencers, First Mentions werden sogar sauberer |
| **`sync_x_bookmarks`** | 0 Zeilen in `agent_workspace` (artifact_type `x_bookmark`) – nie benutzt | toter Pfad (kostet nichts, aber verwirrt) |
| **Vielposter ohne Ticker-Yield** (@staunovo, @zerohedge, @anasalhajji) | 2.329 Posts = **27% der Kosten**, aber nur 173 = **4,3% des Ticker-Signals**; @anasalhajji ist zu 75% Retweets | inhaltliche Entscheidung – nur wenn der Korpus nach Ticker-Yield bewertet wird |
| **Abgleich-Intervall** | $0.39/Lauf, Lauf kostet strukturell ~2.380 Tweets (1 Seite × 119 Accounts) | 14-Tage-Takt halbiert auf ~$0.85/Monat, wenn die Suche stabil läuft |

Nicht verzichtbar: die stündliche Suche (Kernmechanismus; Preis = Inhalt), der
Abgleich als Versicherung (fand echte Lücken, z.B. ganze Threads), `user/info`,
Liveness-Check (inaktiv), sowie die manuellen Tools.

## Betriebsmodus: ein Lauf pro Tag um 22:00 (ab 24.09.2026)

- **Zeitplan:** `X_DAILY_RUN_AT=22:00` + `X_DAILY_RUN_TZ=Europe/Berlin`.
  Es läuft genau ein Zyklus pro Tag; **verpasste Slots werden nachgeholt**
  (Container war aus, Neustart nach 22:00 → Lauf startet sofort).
- **Manuell:** `manage_sync_pipeline SYNC_NOW` weckt den Loop innerhalb von
  Sekunden; der Tagesslot bleibt davon unberührt.
- **Kein stiller Datenverlust (Lücken-Schließer):** Jeder Suchlauf meldet, ob er
  sein Fenster *vollständig* gelesen hat. Trifft das nicht zu (Fehler, Seitenlimit,
  Budget-Stopp), läuft automatisch ein Timeline-Abgleich genau über dieses Fenster.
  **Der Such-Cursor wird erst fortgeschrieben, wenn das Fenster abgedeckt ist** –
  sonst bleibt er stehen und der nächste Lauf holt es erneut.
- **402:** Bei leerem Guthaben bleibt der Tagesslot offen; nach dem Aufladen läuft
  er automatisch nach (kein verlorener Tag).
- **Budget:** `X_MAX_CREDITS_PER_CYCLE=80000` gilt pro Phase (Suche bzw.
  Lücken-Schließer), damit ein am Budget gescheiterter Suchlauf den Gap-Closer
  nicht mitblockiert.

### Verifiziert (24.09.2026)

| Test | Ergebnis |
|---|---|
| Manueller Lauf (`SYNC_NOW`) | „Lauf startet (manuell angefordert)" → 56 Tweets / 7 neue Posts / $0.0084, Fenster vollständig |
| Suche künstlich am Budget abgebrochen | „⚠️ Fenster NICHT vollständig" → Gap-Closer startet automatisch → scheitert ebenfalls am Test-Budget → „⚠️ Lücke NICHT geschlossen – Cursor bleibt stehen" (Cursor blieb nachweislich auf dem alten Wert) |

### Erwartete Kosten

| Posten | pro Tag | pro Monat |
|---|---|---|
| Suche (24h-Fenster, 1h Overlap) | ~24.000 Cr / $0.24 | ~$7.20 |
| Timeline-Abgleich (wöchentlich, $0.39/Lauf) | – | ~$1.70 |
| Lücken-Schließer | nur bei Bedarf (~$0.36) | – |
| **Summe** | | **~$9/Monat** |

Hinweis: Der Tagesrhythmus selbst spart fast nichts – die Suche wird pro
geliefertem Tweet abgerechnet. Der Gewinn liegt in der Abdeckungs-Garantie und
im Wegfall der stündlichen Überlappungs-Duplikate. Wer auch die letzten
Suchindex-Lücken täglich statt wöchentlich schließen will, setzt
`X_RECONCILE_INTERVAL_SEC=86400` (+ ~$9/Monat).

## Favoriten + 3-Tier-Suche (ab 25.09.2026)

### Geld-Gate `x_users.favorite`
Nur Accounts mit `favorite = true` laufen im Regel-Sync (tägliche Suche **und**
Timeline-Abgleich). Alles andere bleibt in der Datenbank, ist durchsuchbar und
wird bei Bedarf gezielt nachgeladen.

- Startbelegung laut Vorgabe: Favorit = **weniger als 20 Posts/Tag**
  (Schnitt über die vollständig abgedeckten Tage) → **100 von 119** Accounts.
- Ausgeschlossen (≥20/Tag): @1chartmaster, @algoxfloww, @anasalhajji, @cfromhertz,
  @economyvefinans, @elliottforecast, @hedgeye, @investverified, @nobullshytrader,
  @novarealinvest, @schwabnetwork, @specialsitsnews, @staunovo, @thenewmoney_app,
  @thevalueist, @tickeron, @_tp888, @uscorpfilings, @zerohedge
- Neue Influencer starten mit `favorite = false` (Freigabe per
  `manage_influencers UPDATE … favorite=true`).

**Wirkung:** Die Favoriten tragen nur **34,6 % des Volumens** (Ticker-Korpus:
12.750 Posts/7 Tage gesamt, davon 4.408 aus Favoriten). Die Suche kostet damit
statt ~$9 nur noch **~$3.50/Monat**; der Abgleich fällt von 119 auf 100 Besuche
pro Lauf (~$1.30/Monat).

### Beschreibung + OpenBrain
Jeder Influencer hat eine Beschreibung, die als **OpenBrain-Eintrag** liegt
(Embedding + Keywords) und in `x_users` verlinkt ist:

| Spalte | Inhalt |
|---|---|
| `description` | `openbrain://<uuid>` – der Link auf den Eintrag |
| `brain_ref` | die UUID (maschinenlesbar) |
| `brain_content` | lokale Kopie des Textes (Basis für `x_users.embedding`) |

- **Standardmäßig automatisch:** `manage_influencers ADD` erzeugt den Eintrag.
  Ohne eigenen Text wird aus den gespeicherten Posts ein Themenprofil gebaut
  (Top-Ticker, Top-Keywords, LLM-Kurzprofil: "gut bei den Themen …").
- **Per MCP anpassbar:** `manage_influencers UPDATE` mit `description` (eigener
  Text) oder `refresh_description: true` (Profil neu aus den Posts erzeugen).
  Der alte Eintrag wird dabei soft-deleted, damit nichts dupliziert.
- Zusätzlich nutzbar: `match_x_users(query_embedding, …)` findet Influencer
  thematisch – die Grundlage für gezieltes Nachladen.

### Drei Tiers der Influencer-Suche (`search_influencer_posts`)

| Tier | Quelle | Kosten | Wann |
|---|---|---|---|
| **1** | offline, **nur ⭐ Favoriten** | 0 Credits | Routinefragen |
| **2** (Default) | DB **aller** Getrackten; lädt fehlende Posts **gezielt live** nach – aber nur bei dünner Datenlage (< 3 gespeicherte Posts im Fenster), max. 3 Accounts × 60 Posts pro Aufruf | 15 Credits/Post, gedeckelt | wenn Tier 1 die Frage nicht beantworten kann oder die Datenlage dünn ist |
| **3** | zusätzlich **freie Live-Suche auf X** (Thema/Ticker), auch außerhalb der überwachten Accounts | 15 Credits/Post, gedeckelt | Discovery / neue Themen |

Die Antwort nennt immer den Tier, die Begründung und die entstandenen Credits.

### Interaktives Tagesbudget
Alle Tool-Abrufe (Tier 2/3, `search_x_ticker`, `show_x_content ONLINE`,
manuelle Syncs) laufen gegen `X_INTERACTIVE_DAILY_CREDITS` (Default 50.000 = $0.50/Tag).
Der Ingestion-Worker ist davon ausgenommen (eigenes Phasen-Budget). Damit kann kein
Agent versehentlich das Guthaben leerräumen – sichtbar im STATUS.

### Verifiziert (25.09.2026)

| Test | Ergebnis |
|---|---|
| Profil-Erzeugung `UPDATE refresh_description` für @kobeissiletter | OpenBrain-Eintrag mit Themenprofil + Ticker/Keywords, Embeddings auf beiden Seiten |
| Tier 1 mit Nicht-Favorit @staunovo | sauber abgelehnt: "keine Favoriten-Daten, Tier 2 würde nachladen" |
| Tier 2 mit @jessefelder (still seit 2024) | dünne Datenlage erkannt → 19 Posts live nachgeladen (285 Credits) → Treffer |
| `SYNC_NOW` nach der Umstellung | 5 statt 6 Chunks, 450 Credits für 1,6 h Fenster |

### Neue Kostenlage

| Posten | vorher | nachher |
|---|---|---|
| Suche (34,6 % Volumen) | ~$9.00 | **~$3.50** |
| Timeline-Abgleich (100 statt 119 Konten) | ~$1.70 | **~$1.30** |
| Interaktive Abrufe | unbegrenzt | **≤ $0.50/Tag** (Budget) |
| **Summe** | ~$10.70 | **~$4.90/Monat** |

## Betrieb

Neue Variablen stehen in `llm-gateway/docker-compose.yml` (Service `mcp-cco`).
`manage_sync_pipeline STATUS` zeigt jetzt:

- `Kosten letzter Zyklus nach Quelle`
- `Letzte Suche / Letzter Abgleich` mit Tweets und Credits
- `Kostenbremse` (Budget, Anzahl Stopps)
- `Guthaben` (402-Sperre bis …)
