# WORKITEM_HARDENING_PLAN

**Auslöser:** Der Lauf `2dd854d4` (Influencer-Report 19./20.09.2026, Watchlist-Kandidaten KW 39) endete nach
**2.023.339 Tokens und 38 Modellrunden als `failed`** — ohne Ergebnis. Die Auswertung hat fünf Schwachstellen
freigelegt. Dieser Plan stellt sie ab.

**Nicht Teil dieses Plans:** die Datenlücke in `metadata.tickers` (36.562 von 55.258 Posts ohne Ticker-Feld).
Ursache ist behoben, Fill läuft.

## Stand der Umsetzung (20.09.2026, 23:15)

| WP | Stand |
|---|---|
| WP0 | ✅ erledigt — `x_first_mentions` 3.727 Ticker gegen 3.732 im Korpus (Rückstand = laufender Fill). Der finale `rebuild_x_first_mentions()` läuft automatisch am Ende des Fills |
| WP1 | ✅ Migration `022_ticker_breadth.sql` angewendet und am Fixture-Fenster abgenommen |
| WP2 | ✅ in derselben Migration (`ticker_evidence`), Post-IDs kommen an |
| WP3 | ✅ `report_tools.ts` mit beiden Tools, in `index.ts` registriert; Beschreibungen von `show_x_content`/`discover_ticker_mentions` geschärft; Lead- und CCO-Persona ergänzt; Rollenbeschreibung via `023_cco_role_description.sql` |
| WP4 | ✅ `loop.py` (Session pro Versuch), kompiliert |
| WP5 | ✅ `workitems.ts` + end-to-end getestet (negativ/positiv), Container neu gestartet |
| WP6 | ⏳ offen — hängt an einem Neustart von `dsh-native` |

**Aufgeschoben:** Neustart von `llm-gw-mcp-cco`. Der Metadata-Fill läuft **in-process** in genau diesem
Container (nur ein deno-Prozess, per `docker top` geprüft) — ein Neustart würde ihn abbrechen. Die neuen
Tools und Beschreibungen greifen erst nach dem Neustart, also **nach** dem Fill (ETA ~21:29 UTC).

---

## Was bereits existiert — und deshalb NICHT gebaut wird

Die Analyse hat ergeben, dass ein Teil meines ersten Vorschlags schon da ist. Das ändert den Plan:

| Objekt | Was es tut |
|---|---|
| `x_first_mentions` (11.641 Zeilen) | Tabelle: `(ticker, author)` → `first_mentioned_at`, `post_id`. Wird beim Ingest von `updateFirstMentions()` gepflegt |
| `get_first_mentions_v2(p_keywords, p_authors, p_start_date, p_limit)` | liest **x_first_mentions** (nicht den Korpus) und liefert bereits `post_id` mit |
| `get_new_ticker_mentions(p_authors, p_limit)` | Erstnennung on-the-fly über `metadata->'tickers'` |
| `rebuild_x_first_mentions()` | baut `x_first_mentions` aus dem Korpus neu auf |
| **MCP-Tool `discover_ticker_mentions`** | wrappt `get_first_mentions_v2` — die Erstnennung ist **im CCO bereits als Tool verfügbar** |

**Konsequenz:** Die Neuheit war nie das Problem. Der CCO hatte das Werkzeug. Der Fehler war der
**Auftrags-Schnitt**: der Lead hat `novelty-check` als *abhängigen* Task **hinter** die teure Vollextraktion
gehängt, statt die Neuheit **zuerst** zu fragen. Genau das ist der Kern von WP3.

---

## WP0 — Nach dem Fill: `x_first_mentions` neu aufbauen

**Status: erledigt** (geprüft). Der finale Rebuild läuft automatisch am Ende des Fills.

**Voraussetzung, kein Code.** `x_first_mentions` wird aus `metadata.tickers` abgeleitet. Solange die Tabelle
nicht neu aufgebaut wird, bleiben `discover_ticker_mentions` und `get_first_mentions_v2` auf dem alten,
unvollständigen Stand — der Fill würde nur den Korpus reparieren, nicht die daraus abgeleitete Tabelle.

```sql
select rebuild_x_first_mentions();   -- nach Abschluss des Fills
```

**Abnahme:** Zeilenzahl und Verteilung prüfen (vor dem Fill: 11.641 Zeilen / 66 % Posts ohne Ticker):

```sql
select (select count(*) from x_first_mentions) as erstnennungen,
       (select count(*) from agent_workspace
         where artifact_type='x_post'
           and coalesce(metadata->'tickers','[]'::jsonb) <> '[]'::jsonb) as posts_mit_ticker;
```

---

## WP1 — `ticker_breadth`: Breiten-Aggregat über ein Zeitfenster (Kernstück)

**Das einzige, was wirklich fehlt.** Alle bestehenden Funktionen beantworten „wann erstmals erwähnt", keine
beantwortet „welche Ticker wurden im Fenster von wie vielen **verschiedenen** Autoren genannt".

**Migration `022_ticker_breadth.sql`** (Konvention aus 019–021: `create or replace function`, Grants für
`anon`/`service_role`, `notify pgrst`):

```sql
create or replace function ticker_breadth(
  p_start        timestamptz,
  p_end          timestamptz,
  p_min_authors  integer default 2,      -- 1 = alles, 2 = nur belastbare Mehrfach-Belege
  p_only_new     boolean default false,  -- nur Ticker, deren Erstnennung im Fenster liegt
  p_limit        integer default 50
) returns table (
  ticker              text,
  autoren             integer,   -- VERSCHIEDENE Autoren im Fenster
  nennungen           integer,
  erstmals_im_korpus  timestamptz,  -- aus x_first_mentions
  neu_im_fenster      boolean,      -- erstmals_im_korpus >= p_start
  nicht_us            boolean       -- Suffix .L/.WA/.SR/.DE/.PA
) language sql stable as $$
  with fenster as (
    select w.metadata->>'author' as autor,
           upper(t.ticker)       as ticker
      from agent_workspace w,
           jsonb_array_elements_text(w.metadata->'tickers') as t(ticker)
     where w.artifact_type = 'x_post'
       and coalesce((w.metadata->>'published_at')::timestamptz, w.created_at)
           between p_start and p_end
  ),
  agg as (
    select f.ticker, count(distinct f.autor) as autoren, count(*) as nennungen
      from fenster f group by f.ticker
  )
  select a.ticker, a.autoren, a.nennungen, fm.erstmals,
         coalesce(fm.erstmals >= p_start, false) as neu_im_fenster,
         a.ticker ~ '\\.[A-Z]{1,3}$' as nicht_us
    from agg a
    left join (select ticker, min(first_mentioned_at) as erstmals
                 from x_first_mentions group by ticker) fm on fm.ticker = a.ticker
   where a.autoren >= p_min_authors
     and (not p_only_new or coalesce(fm.erstmals >= p_start, false))
   order by a.autoren desc, a.nennungen desc
   limit p_limit;
$$;

grant execute on function ticker_breadth(timestamptz, timestamptz, integer, boolean, integer)
  to anon, service_role;
notify pgrst, 'reload schema';
```

**Entscheidungen und ihre Begründung:**

1. **Quelle `metadata.tickers`, nicht Regex über `content`.** Nach dem Fill ist das Feld die gepflegte,
   indexierbare Quelle (Präzedenzfall: die bestehenden Funktionen nutzen es genauso). Der Regex-Scan aus
   `scan_cashtags.sql` wird zum **Prüforakel** für die Abnahme, nicht zum Produktionspfad.
2. **`stable`, `security invoker`** — wie alle bestehenden RPCs (`discover_first_mentions`, `hybrid_search_workspace`).
   `agent_workspace` hat Grants für `anon`/`service_role`, ein `definer` ist nicht nötig.
3. **Neuheit aus `x_first_mentions`**, nicht neu berechnet — die Tabelle ist genau dafür da (WP0).
4. **`nicht_us` als Flag**, weil `EEE.L`, `PKN.WA`, `2222.SR` im echten Report echte Stolpersteine waren.

**MCP-Tool `ticker_breadth`** — neues Modul `mcp/agent-cco/tools/report_tools.ts`, in `index.ts` neben den
fünf bestehenden registriert (`registerReportTools(server)`). Handler-Muster wie `discover_ticker_mentions`:
`supabase.rpc("ticker_breadth", {...})`, Ausgabe als kompakte Tabelle. Bei ~50 Zeilen sind das **~2–4k Tokens**.

**Abnahme — korrigiert.** Mein ursprüngliches Kriterium („muss dem Regex-Scan entsprechen") war falsch.
Gemessen im Fixture-Fenster nach dem Fill:

| Quelle | Ticker | Nennungen |
|---|---|---|
| `metadata.tickers` | 422 | 1065 |
| Regex über `content` | 309 | 755 |

`metadata.tickers` ist **Obermenge**, nicht Entsprechung: die LLM-Extraktion erkennt auch Ticker ohne
Cashtag, der Regex nur `$XXX`. Das Kriterium lautet daher: **jeder Regex-Treffer muss in
`ticker_breadth` enthalten sein** (Abdeckung), nicht Gleichheit. Der Regex-Scan bleibt als
Vollständigkeitsprüfung liegen, nicht als Sollwert.

Abgenommen am echten Fenster: NVDA 10 Autoren, MU 10, VLO 8, AMD 7, META 7 — Rangfolge plausibel,
Top-Kandidat mit dem Regex-Scan deckungsgleich.

---

## WP2 — `ticker_evidence`: Belege mit Post-ID

Für den zweiten Trichter-Schritt: 2–3 Belege je **Kandidat**, nicht je Nennung.

```sql
create or replace function ticker_evidence(
  p_ticker text, p_start timestamptz, p_end timestamptz, p_limit integer default 3
) returns table (autor text, zeit timestamptz, post_id text, auszug text)
language sql stable as $$
  select w.metadata->>'author' as autor,
         coalesce((w.metadata->>'published_at')::timestamptz, w.created_at) as zeit,
         w.x_external_id as post_id,
         left(regexp_replace(w.content, '\\s+', ' ', 'g'), 240) as auszug
    from agent_workspace w
   where w.artifact_type = 'x_post'
     and w.metadata->'tickers' ? upper(p_ticker)
     and coalesce((w.metadata->>'published_at')::timestamptz, w.created_at) between p_start and p_end
   order by (w.metadata->>'public_metrics')::jsonb->>'like_count' desc nulls last
   limit p_limit;
$$;
```

Deckt zwei Monita des CCO ab: **fehlende Post-ID** („Uebermittlung der Tweet-ID/URL im show_x_content-Output")
und den Zwang, für Belege den halben Korpus zu lesen. Optionaler GIN-Index für den `?`-Operator:

```sql
create index if not exists idx_aw_x_tickers on agent_workspace using gin ((metadata->'tickers'))
  where artifact_type = 'x_post';
```

---

## WP3 — Beschreibungen und Persona (kein Code, größte Wirkung)

Der Lauf ist nicht an fehlendem Wissen gescheitert — der CCO hat `metadata.tickers` in seiner eigenen
Methodik-Note **erwähnt** und verworfen. Er ist an einem Auftrag gescheitert, der Volltext erzwang.

**a) Tool-Beschreibungen** (Muster WHEN TO USE / WHEN NOT TO USE ist im Repo etabliert):

- `show_x_content`: ergänzen um
  > **WHEN NOT TO USE:** Für Ranglisten, Zählungen oder Breitenanalysen über mehr als ~50 Posts — nutze
  > `ticker_breadth`. Jede Zeile hier enthält den vollen Post-Text; das ist der teuerste Weg (gemessen:
  > 400k Tokens pro Tageshälfte).
- `discover_ticker_mentions`: schärfen auf „läuft in EINEM Aufruf, ohne vorherige Sammlung — die Neuheit
  ist eine eigene Frage, keine Ableitung aus einer Vollextraktion".
- Neues `ticker_breadth`: „Erster Schritt jedes Watchlist-/Rangberichts. Danach `ticker_evidence` nur für
  die Top-N."

**b) Lead-Persona** (`roles/lead.cordis.yml`, Abschnitt ROLLENWAHL):

> **Trichter, nicht Vollscan.** Für Breiten-, Ranking- oder Zählaufgaben über einen Korpus ist
> `ticker_breadth` der ERSTE Task — niemals `show_x_content` über den gesamten Zeitraum. Belege
> (2–3 Posts je Kandidat) sind ein ZWEITER, kleiner Task auf dem Aggregat (`context_refs`).
> Reihenfolge: erst Breite, dann Tiefe der Top-10. Nie beides in einem Item.
> Ein Item, das mehr als ~100 Posts Volltext lesen müsste, ist falsch geschnitten.

**c) Budget-Richtwerte korrigieren.** Die Persona sagt „tokens: rounds × 30000". Gemessen:

| Item-Typ | Runden | Tokens | pro Runde |
|---|---|---|---|
| Planung (initial) | 3 | 37k | ~12k |
| Bewertung (review) | 9 | 97k | ~11k |
| Vollextraktion (gescheitert) | 9–11 | 364–482k | **~40–45k** |

Richtwert auf `rounds × 35000` anheben **und** den Satz aufnehmen, dass ein Item mit `>100` Posts
Volltext falsch geschnitten ist — das ist die eigentliche Ursache der Risse, nicht die Zahl.

---

## WP4 — Engine: Session pro Versuch (der Retry-Bug)

**Befund:** Ein `task` läuft mit `session_id = workitem_id`. Beim Retry landet der zweite Versuch in
**derselben Session**, und die `budget-policy` liest das Budget aus der **ersten** User-Nachricht der Session
und zählt den **gesamten** Session-Verlauf. Ergebnis: der Retry startet über Budget, bekommt eine leere Runde
ohne Tool-Aufruf und stirbt mit „kein workitem_finish". Belege: 3 Versuche, 3 identische Gründe, 0 Token-Zuwachs,
leere Turns (`turn/start → inbox/spliced → turn/end`), ein in der DB erhöhtes Budget erreicht die Policy nie.

**Fix** in `services/secretary/secretary/loop.py`, `_dispatch`:

```python
if keeps_session:
    session_id = store.ensure_session(item["change_item_id"], claimed["role"], str(claimed["id"]))
else:
    # Zustandslose Worker bekommen eine Session PRO VERSUCH. Sonst zaehlt die
    # budget-policy beim Retry den Verbrauch des Vorversuchs gegen das alte Budget,
    # die Runde bleibt leer und das Item scheitert erneut an "kein workitem_finish".
    session_id = f"{claimed['id']}-v{claimed['attempts']}"
```

**Verworfen:** die Session-Datei in `store.requeue()` löschen. Sie zerstört die Diagnosefähigkeit (genau die
Session hat diesen Bug beweisbar gemacht) und braucht Wissen über `DSH_HOME`-Pfade im Store.

**Testfall:** Item mit kleinem Token-Budget, das reißt → `requeue` → zweiter Versuch muss mit `0` Verbrauch
starten und darf nicht mit „kein workitem_finish" enden.

---

## WP5 — Engine: genau ein Review pro Runde

**Befund:** Der Lead hat in Runde 2 acht Items angelegt — **kein Review**. `finalize_change_items()` nimmt
dann das `satisfied:false` aus Runde 1: der Lauf endet zwingend als `failed`, selbst wenn Runde 2 alles liefert.
Musste manuell ergänzt werden.

**Fix** in `mcp/agent-wiq/tools/workitems.ts`, `workitem_create`, in der bestehenden Validierungsstrecke
(vor dem Insert): plant ein `initial`- oder `review`-Item Kinder, muss **mindestens ein Item vom Typ `review`**
dabei sein.

```ts
if (!items.some((i) => i.type === "review")) {
  return fail("Runde " + round + " ohne review-Item. Jede Runde braucht genau ein review, " +
              "sonst entscheidet finalize_change_items() mit der Bewertung der VORIGEN Runde.");
}
```

Unkritisch, weil Reviews von der Skip-Kaskade ausgenommen sind — sie laufen auch, wenn alle Vorgänger
gescheitert sind. **Testfall:** `tests/rounds.sh` um einen Fall erweitern (Review plant nur Tasks → Fehler).

---

## WP6 — Betrieb: `run_*` erreichen die Session nicht

**Befund:** Der WIQ-Server bietet 7 Tools (`workitem_*` + `run_submit/status/result`), in der Chat-Session
waren nur die vier `workitem_*` sichtbar. Deshalb musste der Auftrag über `secretary.cli submit` eingereicht
werden. Vermutete Ursache: veralteter Tool-Katalog im langlebigen DSH-Hostprozess, nicht im Server.

**Maßnahme:** `systemctl --user restart dsh-native.service`, danach verifizieren:

```sh
bash mcp/agent-wiq/tests/call.sh tools/list      # muss 7 Tools zeigen (Server, bereits bestätigt)
```
und in einem **neuen Chat** prüfen, ob `run_submit`/\`run_status`/\`run_result` im Katalog stehen.
Falls nicht: Ursache liegt im MCP-Client-Caching des Hosts — dann dort ansetzen, nicht am Server.

---

## Reihenfolge und Aufwand

| WP | Inhalt | Größe | Abhängig von |
|---|---|---|---|
| WP0 | `rebuild_x_first_mentions()` | Minuten | Abschluss des Fills |
| WP1 | `ticker_breadth` + Tool | ~1/2 Tag | WP0 (für die Neuheitsspalte) |
| WP2 | `ticker_evidence` + Tool | ~2 h | WP1 |
| WP3 | Beschreibungen + Persona | ~1 h, **höchste Wirkung** | WP1 (nennt das Tool) |
| WP4 | Session pro Versuch | ~1 h | — |
| WP5 | Review-Validierung | ~1 h | — |
| WP6 | Neustart + Verifikation | Minuten | — |

WP4 und WP5 sind unabhängig von der CCO-Arbeit und können sofort laufen. **WP3 ist der wichtigste Punkt**:
ohne den Auftrags-Schnitt hilft das beste Aggregat nichts.

---

## Testplan

- **Fixture:** 19./20.09.2026, Sollzahlen aus dem Regex-Scan (`dsh_playground/influencer_report_2026-09-19_20/scan_cashtags.sql`):
  294 Ticker, 718 Nennungen, VLO mit 8 Autoren. Diese Datei bleibt als Prüforakel liegen.
- **WP1:** `ticker_breadth('2026-09-19T00:00Z','2026-09-20T22:00Z', 2, false)` gegen den Regex-Scan.
- **WP2:** Belegabruf für einen bekannten Kandidaten, Post-ID muss gesetzt sein.
- **WP4/WP5:** deterministische Fälle in `services/secretary` bzw. `mcp/agent-wiq/tests/rounds.sh`.
- **Regression:** der nächste echte Report-Lauf muss unter **20k Tokens** bleiben (heute: 2,02 Mio).

---

## Risiken und offene Fragen

1. **Der Fill verändert die Sollzahlen.** Trefferzahlen nach dem Fill weichen vom Regex-Scan von *vor* dem Fill ab.
   Abnahme daher als **Vergleich zweier Quellen im selben Zustand**, nicht gegen die alten Absolutzahlen.
2. **`metadata.tickers` ist LLM-klassifiziert**, der Regex ist mechanisch. Bleibt nach dem Fill eine Lücke
   (z. B. ETFs oder Nicht-US-Suffixe, die das LLM wegfiltert), ist das eine Semantik-Entscheidung, keine Bugfix —
   dann muss WP1 sie dokumentieren, nicht der Regex überschreiben.
3. **`p_only_new` hängt an `x_first_mentions`.** Ist WP0 nicht gelaufen, liefert die Neuheitsspalte Unsinn —
   deshalb die Abhängigkeit in der Tabelle oben.
4. **Kein Testverzeichnis im CCO-Server.** Für WP1/WP2 gibt es kein `tests/call.sh`-Pendant wie bei `agent-wiq`.
   Entweder eines anlegen (empfohlen, ~20 Zeilen, analog) oder mit `curl` gegen 8788 prüfen.
