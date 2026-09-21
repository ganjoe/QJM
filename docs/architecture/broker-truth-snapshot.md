# Broker-Truth-Snapshot

**Status:** umgesetzt (Stufe 1 + 2), Deployment offen
**Betrifft:** `ibkr_live_daemon/main.py`, `mcp/agent-pta/tools/pta.ts`
**Diagramme:** `dsh_playground/drawio/qjm-datenfluss-vorher-ist-stand.drawio`, `dsh_playground/drawio/qjm-datenfluss-nachher-soll-stand.drawio`

---

## 1. Anforderung

> „Wenn ich wissen will, wie mein Portfolio steht, will ich als Zahl das haben,
> was mein Broker mir sagt, wenn ich mich einlogge."

Daraus folgt als Architekturprinzip:

1. **Der Broker ist die einzige Wahrheit** für Depotwerte. Kein Agent rechnet
   sich einen Depotstand selbst zusammen.
2. **Eine Zahl ohne Haltbarkeitsdatum ist wertlos.** Ein Stand, der 3 Tage alt
   ist, darf nicht identisch aussehen wie einer, der 3 Sekunden alt ist.
3. **Lesen und Ausführen sind getrennte Pfade.** Eine Frage nach dem Depot darf
   niemals einen Kanal berühren, über den Orders laufen.

---

## 2. Der Fehler (Ursache)

Die Portfolio-Anzeige lieferte **wochenlang eingefrorene Kurse** aus — im
konkreten Fall 204 EUR unter dem tatsächlichen Kontostand (13.993,62 statt
14.197,57 EUR), ohne jede Warnung.

### Die Kette

| # | Ort | Verhalten |
|---|---|---|
| 1 | `mcp/agent-pta/tools/pta.ts` (`list_active_positions`) | legte eine `REFRESH_REQUESTED`-Zeile an — **ohne das Feld `notes`** |
| 2 | DB-Schema `pta_execution_log` | `notes text,` — **kein DEFAULT**, also NULL |
| 3 | `ibkr_live_daemon/main.py` (`handle_refresh_requests`) | suchte Aufträge mit `.neq("notes", "PROCESSING")` |

PostgREST übersetzt Schritt 3 in SQL `notes <> 'PROCESSING'`. In SQL ist
`NULL <> 'PROCESSING'` gleich **NULL**, und NULL zählt in `WHERE` nicht als wahr.
**Jede** eingehende Refresh-Anfrage fiel damit durch den Filter.

Der Daemon ist der **einzige Schreiber** von `pta_ibkr_positions` und
`pta_ibkr_account_summary`. Wird er nie aufgerufen, wird nie ein neuer Snapshot
geschrieben.

Das Tool wartete anschließend 10 s auf eine Bestätigung, **brach ohne Fehler
ab**, löschte die Auftragszeile und lieferte die alten Werte aus. Ergebnis: ein
eingefrorener Stand, der wie ein frischer aussah.

### Warum Neustarts nichts halfen

Weder `ibkr-sync` noch das Gateway waren die Ursache. Der Auftrag erreichte den
Daemon nie — ein Neustart des Empfängers ändert daran nichts. Das ist der
Grund, warum „Container neu starten" hier wirkungslos blieb.

### Beweis durch Zeilenvergleich

Zwei Geschwister-Tools, identischer Mechanismus:

| Datei | Insert | Daemon-Filter | Ergebnis |
|---|---|---|---|
| `mcp/agent-pta/tools/get_quote.ts` | `notes: "PENDING"` | `handle_quotes`: `.eq("notes","PENDING")` | funktioniert |
| `mcp/agent-pta/tools/pta.ts` | *kein* `notes` → NULL | `handle_refresh_requests`: `.neq(...)` | tot |

### Nebenbefund: die Kopplung war schon einmal gefährlich

`mcp/agent-pta/tools/pta.ts` dokumentiert einen Vorfall: ohne einen Guard fiel
eine `REFRESH`-Anfrage durch den Order-Handler, wurde als `ORDER_SUBMITTED` mit
`action = "REFRESH"` geschrieben und **vom Daemon in eine BUY-Market-Order
aufgelöst**. Eine Frage nach dem Depot hat also schon einmal fast eine echte
Kauforder ausgelöst. Der damalige Fix war eine Aktions-Whitelist im Daemon
(`main.py`, `handle_orders`) — die Ursache, der gemeinsame Kanal, blieb.

---

## 3. Zielarchitektur

Drei Pfade, die sich **keinen Transportkanal** mehr teilen:

| Pfad | Transport | Schreibzugriff |
|---|---|---|
| **Charts** | PCA-Service liest Parquet | read-only |
| **Quotes** | `pta_execution_log` (`QUOTE_REQUESTED`) → Daemon → `reqTickers` | nur der Daemon |
| **Portfolio** | periodischer Snapshot → read-only Tabellen | nur der Daemon |
| **Orders** | `pta_execution_log` (`ORDER_SUBMITTED` / `CANCEL_REQUESTED`) | nur der Daemon |

Kern der Änderung: **Der Snapshot wird periodisch geschrieben, nicht auf
Anfrage.** Ein anfragegetriebener Snapshot ist genau so frisch wie die letzte
Anfrage — und wenn diese Anfrage unbemerkt scheitert, friert der Stand ein.
Ein periodischer Snapshot kann das strukturell nicht.

### Umsetzung Stufe 1 + 2

**`ibkr_live_daemon/main.py`**

- Neuer Snapshot-Writer `write_portfolio_snapshot(reason)`, herausgezogen aus
  `handle_refresh_requests`. Schreibt Positionen, Kontostand **und** offene
  Orders in einem Durchgang.
- **Guard:** Geschrieben wird nur, wenn mindestens ein Konto mit
  `NetLiquidation > 0` vorliegt. Ohne diesen Guard würde ein Lauf direkt nach
  dem Verbindungsaufbau alle Depotzeilen löschen und durch Nullen ersetzen.
  Im Skip-Fall bleibt der bestehende Snapshot **unverändert**.
- `sync_loop` ruft den Writer **alle `PTA_SNAPSHOT_INTERVAL_SEC` (Default 30 s)**
  auf — unabhängig von jeder Agenten-Anfrage.
- `handle_refresh_requests` filtert jetzt auf einen **expliziten Zustand**
  (`notes = 'PENDING'`) **und** den aktiven Modus. Zeilen ohne `notes` werden
  zusätzlich über einen expliziten NULL-Test eingesammelt (Toleranz für ältere
  Aufrufer).
- `IB_MARKET_DATA_TYPE` ist per Umgebungsvariable konfigurierbar (Default 4).
  **Wichtig:** Das betrifft ausschließlich Marktdaten-Anfragen (`get_quote`).
  Die Depotbewertung kommt aus den Account-Updates des Brokers und ist davon
  unabhängig — der NAV ist auch mit Typ 4 nicht verzögert.

**`mcp/agent-pta/tools/pta.ts`**

- **Reiner Lesezugriff im Regelbetrieb:** Der Handler prüft zuerst das Alter des
  vorhandenen Snapshots. Ist er frischer als 90 s (Normalfall: der Daemon
  schreibt alle 30 s), wird **kein** Refresh angefordert und
  `pta_execution_log` überhaupt nicht berührt. Nur bei einem veralteten Stand
  wird eine Anfrage geschrieben — das deckt einen Daemon ab, der noch nicht
  periodisch schreibt oder gerade nicht verbunden ist. Der Pfad heilt sich
  damit selbst und bleibt in beiden Ausbaustufen korrekt.
- Der Refresh-Auftrag setzt **`notes: "PENDING"` und `mode: activeMode`**
  explizit. Damit kann ein Refresh aus PAPER nie als LIVE-Auftrag erscheinen.
- Eine ausbleibende Bestätigung wird **nicht mehr verschwiegen**: sie wird
  geloggt und in der Antwort ausgewiesen.
- **Frische-Kontrakt:** Jede Antwort beginnt mit
  `=== SNAPSHOT: <Zeitpunkt> (Alter: <X>s) — ✅ frisch | ⚠️ VERALTET ===`.
  Ab 90 s gilt der Stand als veraltet und die Antwort sagt explizit, dass der
  Broker beim Login abweichende Werte zeigen kann.
- Die Tool-Beschreibung weist den aufrufenden Agenten an, veraltete Werte
  **nicht** als aktuellen Depotstand auszugeben.

---

## 4. Deployment

### Empfohlener Weg: ein Skript

```bash
cd /home/daniel/QJM
bash scripts/deploy_broker_snapshot.sh
```

Das Skript erzwingt die Reihenfolge (Tool → Daemon → Migration), spielt die
Migration idempotent ein und prüft die Abnahme automatisch:

- Constraint `pta_refresh_requires_notes` vorhanden
- Snapshot-Alter unter der Grenze (wartet bis zu 3 min auf den ersten
  periodischen Snapshot — der Live-Reconnect braucht ggf. 2FA im Gateway)
- Kontostand und Depotzeilen aus dem Snapshot
- hängende `REFRESH_REQUESTED`-Aufträge
- die letzten `Snapshot (...)`-Zeilen aus dem Daemon-Log

Exit-Code `0` = bestanden. Danach bleibt nur der manuelle Vergleich der
ausgegebenen NAV mit dem Wert, den der Broker beim Login zeigt.

### Manueller Weg (gleichwertig)

Zwei Container, zwei verschiedene Wege — der Unterschied ist wichtig:

| Container | Quelle | Nötig |
|---|---|---|
| `llm-gw-mcp-pta` | bindet `tools/` per Volume ein (`llm-gateway/docker-compose.yml`) | **nur Neustart** |
| `ibkr-sync` | Image-Build (`ibkr_live_daemon/Dockerfile`, `COPY main.py .`) | **Rebuild** |

```bash
# 1. MCP-Tool (Volume-Mount -> ein Neustart genügt).
#    Bringt bereits KORREKTE Zahlen: das neue Tool schreibt notes='PENDING'
#    und passt damit auch durch den Filter der alten Daemon-Version.
docker restart llm-gw-mcp-pta

# 2. Daemon (gebackenes Image -> Rebuild).
#    Bringt die dauerhafte Frische: Snapshot alle 30 s.
cd /home/daniel/QJM/ibkr_live_daemon
docker compose up -d --build

# 3. Migration (erst NACH Schritt 1 — siehe unten).
cd /home/daniel/QJM
docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  < migrations/025_pta_refresh_requires_notes.sql

# 4. Log des Daemons beobachten
docker logs -f ibkr-sync
```

### Migration `migrations/025_pta_refresh_requires_notes.sql`

Erzwingt per CHECK-Constraint, dass ein `REFRESH_REQUESTED`-Auftrag einen
expliziten `notes`-Zustand tragen **muss**. Damit kann der Fehler dieser
Untersuchung nicht wieder entstehen: ein künftiger Aufrufer scheitert laut beim
INSERT, statt still wirkungslos zu bleiben.

**Reihenfolge ist zwingend:** erst Schritt 1 (MCP-Neustart), **dann** Schritt 3
(Migration). Die alte Tool-Version schreibt `REFRESH_REQUESTED` ohne `notes` —
läuft sie nach der Migration weiter, schlägt ihr INSERT fehl und
`list_active_positions` bricht ab. Die Migration ist idempotent und kann
gefahrlos erneut ausgeführt werden.

Erwartete Log-Zeile nach dem Start (alle 30 s):

```
Snapshot (periodic) geschrieben: 14 Position(en), 0 offene Order(s), NAV 14197.57 EUR.
```

---

## 5. Abnahmetest

1. **NAV stimmt mit dem Broker überein.** `list_active_positions` muss den Wert
   zeigen, den der Broker beim Login anzeigt (Toleranz < 0,2 %, Rest ist
   FX-Rundung und Ticker ohne Live-Feed).
2. **Kopfzeile ist frisch.** `Alter` muss deutlich unter 90 s liegen,
   Status `✅ frisch`.
3. **Kein Schreibzugriff mehr beim Lesen.** Bei einem Stand jünger als 90 s
   schreibt `list_active_positions` **keine** Zeile nach `pta_execution_log`.
   Prüfbar über `docker logs llm-gw-mcp-pta` (Log-Zeile `Stand ist Xs alt — kein
   Refresh nötig, reiner Lesezugriff`) und darüber, dass kein
   `REFRESH_REQUESTED` in der Tabelle erscheint.
4. **Periodik greift.** Zwei Aufrufe im Abstand von > 30 s müssen
   unterschiedliche `updated_at`-Zeitstempel zeigen.
5. **Verhalten im Fehlerfall.** `ibkr-sync` stoppen, dann
   `list_active_positions` aufrufen: die Antwort muss `⚠️ VERALTET` bzw.
   `nicht bestätigt` ausweisen — **nicht** wortlos alte Zahlen liefern.

---

## 6. Offene Punkte (Stufe 3)

Nicht in dieser Änderung enthalten, bewusst:

1. **Eigener Kanal für Lese-Anfragen.** `get_quote` nutzt weiterhin
   `pta_execution_log` als Transport. Funktioniert, teilt sich aber weiterhin
   eine Tabelle mit dem Order-Pfad. Sauber wäre eine eigene Request-Tabelle
   ohne Bezug zur Execution.
2. **Preis unmittelbar vor der Order.** `place_trade` fragt derzeit **keinen**
   Kurs ab; der Daemon platziert die Order im 2-Sekunden-Takt. Ein
   Pre-Trade-Abgleich gegen `ib.reqTickers` (inkl. Warnung bei zu großer
   Abweichung vom Limit) fehlt.
3. **`reqMarketDataType` klären.** Ob Typ 4 (Delayed-Frozen) gewollt ist oder
   die Konten Echtzeit-Berechtigungen haben, ist offen. Über
   `IB_MARKET_DATA_TYPE` umstellbar, ohne Code zu ändern.
4. **Ledger gegen Broker abgleichen.** `pta_active_positions` ist eine View über
   `pta_execution_log`, aggregiert **pro `trade_id`**. Dadurch erscheinen VLO
   und HL doppelt, und TRMD/PBF stehen als offene Trades im Ledger, obwohl der
   Broker keine Position führt. Das ist eine zweite, unabhängige Abweichung
   zwischen „was das System sagt" und „was der Broker sagt".
5. **Stops.** Ein Großteil der offenen Positionen läuft ohne Stop-Loss
   (`SL: NONE`).

---

## 7. Review-Befunde (statischer Review, umgesetzt)

Die Änderung wurde einem statischen Review unterzogen (keine Ausführung
möglich). Beide Dateien wurden als syntaktisch korrekt bestätigt. Die
inhaltlichen Befunde sind eingearbeitet:

| Befund | Behebung |
|---|---|
| **Datenverlust:** `pta_ibkr_positions` wurde gelöscht, *bevor* die Folgeabfragen liefen. Eine Exception dort ließ die Depotzeilen ohne Ersatz verschwinden — bei 30-s-Takt 120× häufiger als zuvor. | Es wird zuerst alles aufgebaut (`inserts`, `order_inserts`), der `DELETE` erfolgt unmittelbar vor dem `INSERT`. |
| Gleiche Klasse bei den offenen Orders. | Dito; der `DELETE` liegt jetzt außerhalb von `if trades:`, damit auch eine leere Orderliste korrekt geschrieben wird. |
| **Race nach dem Verbindungsaufbau:** `ib.portfolio()` ist kurz leer, während `accountValues()` schon gefüllt ist → „flaches Konto" und „noch nicht geladen" waren ununterscheidbar; alle Depotzeilen wären gelöscht worden. | Karenzzeit `PTA_EMPTY_POSITIONS_CONFIRM_SEC` (Default 60 s): ein leeres Portfolio bei gleichzeitig vorhandenen Depotzeilen wird erst nach dieser Zeit als „wirklich flach" akzeptiert. |
| **Stiller FX-Fallback:** fehlte ein Kurs, wurde der Marktwert 1:1 als EUR geschrieben und der Snapshot verfälscht. | Fehlt ein Kurs für eine tatsächlich vorkommende Währung, wird **nicht** geschrieben (`FX_RATE_MISSING`). |
| **Irreführender Fehlerzustand:** jeder Fehlschlag wurde als `ERROR: NO_ACCOUNT_METRICS` gemeldet; das Tool kannte diesen Zustand nicht und meldete stattdessen „nicht abgeholt". | `write_portfolio_snapshot` liefert einen Grund-Code (`OK`, `NO_ACCOUNT_METRICS`, `FX_RATE_MISSING`, `EMPTY_POSITIONS_UNCONFIRMED`, `EXCEPTION`); das Tool bricht den Poll bei `ERROR:` ab und meldet den Grund getrennt. |
| **Verzögerte Heilung:** `_last_snapshot_at` wurde auch bei Fehlschlag gesetzt → 30 s Totzeit. | Nur bei Erfolg; im Fehlerfall Retry nach 5 s. |
| **NULL-Spalte zerstörte die ganze Antwort:** `accData.total_cash_balance.toFixed(2)` wirft bei NULL einen TypeError. | `Number(v ?? 0).toFixed(2)`. |
| **Beliebige Zeile statt jüngster Stand:** `limit(1).maybeSingle()` ohne Sortierung. | `.order("updated_at", { ascending: false })` in beiden Abfragen. |
| **Audit-Trail:** die Auftragszeile wurde auch ohne Bestätigung gelöscht. | Löschung nur bei Erfolg; im Fehlerfall bleibt die Zeile als Nachweis stehen. |

Der Snapshot wird damit **ganzheitlich** vorbereitet: Positionen, Kontostand und
offene Orders werden vollständig aufgebaut, **bevor** irgendeine der drei
Tabellen angefasst wird. Ein Fehler beim Aufbau lässt den bestehenden Stand
unberührt.

### Zusätzlich behoben: `portfolio_analytics` war eine zweite Leckstelle

Bei der Nachprüfung zeigte sich, dass `list_active_positions` **nicht der einzige**
Pfad war, über den veraltete oder falsche Depotwerte nach außen gingen.
`portfolio_analytics` lieferte dieselbe eingefrorene NAV (13.993,62 EUR) — und
hatte zwei zusätzliche Defekte:

| Befund | Beleg | Behebung |
|---|---|---|
| Die View `pta_live_risk` hat **weder eine `mode`-Spalte noch einen Filter** — sie liest `pta_ibkr_account_summary` über alle Modi. | View-Definition: `SELECT account, net_liquidation, … FROM public.pta_ibkr_account_summary a;` | Der Live-Risk-Block liest jetzt **direkt** aus `pta_ibkr_account_summary` mit `.eq("mode", activeMode)` und `.order("updated_at", …)`. |
| Gelesen wurde mit `.limit(1).maybeSingle()`. Bei zwei Zeilen (live **und** paper) liefert das eine **beliebige** Zeile — im LIVE-Modus konnte so der PAPER-Kontostand (1.035.132 EUR) als „Portfolio NAV" erscheinen. | `supabase.from("pta_live_risk").select("*").limit(1).maybeSingle()` | Ebenso behoben: modus-gescoped und nach `updated_at` sortiert. |
| Kein Frische-Hinweis: die View liefert `updated_at`, das Tool zeigte es nie. | — | Der Live-Risk-Block trägt jetzt denselben `=== SNAPSHOT: … ===`-Header; ein fehlender Zeitstempel gilt als veraltet, nie als frisch. |
| Die View `pta_portfolio_summary` ist **fest auf `mode = 'live'`** verdrahtet. Im PAPER-Modus meldete das Tool also LIVE-Kennzahlen als Papierwerte. | View-Definition: `WHERE (pta_trade_history.mode = 'live') …` und `WHERE (mode = 'live')` | **Nicht** per Migration geändert (historische Aggregat-Kennzahlen, eigener Auftrag). Stattdessen kennzeichnet das Tool den Block jetzt explizit als `(Ledger: mode = 'live')` und warnt im PAPER-Modus, dass es LIVE-Werte sind. |

Alle drei Korrekturen liegen im Tool und greifen mit dem MCP-Neustart aus
Phase 1 — dafür ist **keine** zusätzliche Migration nötig.

### Bekannte, nicht behobene Risiken (außerhalb dieses Auftrags)

Diese wurden im Review gefunden und betreffen **nicht** den Portfolio-Pfad, sind
aber für den Live-Betrieb relevant. Bewusst nicht angefasst, weil sie den
Order-Pfad ändern und ungetestet niemandem helfen:

1. **COMBO kann still zur Stock-Order werden.** `parse_contract` fängt breit
   (`except: pass`) und schluckt auch die `ValueError` aus der Combo-Leg-
   Qualifizierung — ein nicht qualifizierbarer COMBO degradiert dann zu einer
   normalen Stock-Order auf dasselbe Symbol.
2. **Doppelte Order möglich.** Fliegt zwischen `ib.placeOrder` und dem
   `broker_order_id`-Update eine Exception, bleibt die Spalte NULL und die
   Order wird im nächsten Loop-Durchlauf **erneut** platziert.
3. **`handle_quotes` ohne Mode-Filter** (anders als `handle_cancels`): ein
   Kurs kann in der Zeile des anderen Modus landen.
4. **UPDATE-Zweig** liest `pta_ibkr_positions` ohne Mode-Filter → im PAPER-Modus
   kann die Richtung aus einer LIVE-Zeile abgeleitet werden.
5. **Zeitstempel-Parsing** im Tool: ein `updated_at` ohne Zeitzonen-Offset würde
   als Lokalzeit gelesen und das Alter zu hoch ausfallen. Aktuell unkritisch,
   weil der Daemon `+00:00` schreibt.
