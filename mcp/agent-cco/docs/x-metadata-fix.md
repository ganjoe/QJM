# X-Post-Metadaten: Ursache, Fix und Backfill

Bezug: Fehlerbericht „X-Posts im CCO-Korpus ohne Ticker-/Keyword-Metadaten" (2026-09-16).

## 1. Ursache (bewiesen)

`system_settings.provider_config.cco = "local"`, aber die Switchyard-Route `local`
existiert seit Commit `07daea0` nicht mehr in `llm-gateway/switchyard-config/routes.toml`
(registriert: `auto, pro, reasoning, flash, fast, free, embeddings, embeddings_cpu`).

Ablauf des Defekts:

1. `extractMetadata()` sendet `model: "local"` an `/v1/chat/completions` → Route nicht gefunden.
2. Der Nicht-OK-Pfad gab den Fallback `{ topics: ["uncategorized"], type: "observation" }` zurück.
3. `metadata_worker.ts` schrieb **unbedingt** `llm_categorized: true` plus leere `tickers`/`keywords`.
4. Die Legacy-Abfrage des Workers greift nur bei `metadata->llm_categorized IS NULL` —
   die vergifteten Zeilen galten damit als „fertig" und wurden nie erneut verarbeitet.

Log-Beleg (CCO-Container, vor dem Fix):

```
[WARN] [Switchyard] Route 'local' ist nicht registriert (verfügbar: auto, embeddings, ...).
       Fallback auf 'auto'. provider_config in system_settings korrigieren ...
```

## 2. Umfang des Schadens (Stand vor Backfill)

| Metrik | Wert |
|---|---|
| `x_post` gesamt | 31.291 |
| `metadata.tickers = []` | 30.726 |
| `metadata.keywords = []` | 17.341 |
| `metadata.topics = ["uncategorized"]` | 17.196 |
| `llm_categorized = true` | 31.291 (alle — auch die leeren) |
| Kandidaten mit `$` und leeren Tickern | 5.734 |

## 3. Ursache der scheinbaren Embedding-Lücke (31.291 vs. 19.858)

Kein echter Datenverlust: `embedding IS NOT NULL` gilt für **alle** 31.291 Zeilen.
`manage_sync_pipeline` zählte „embedded" aber nur über `status = 'embedded'`.
11.429 Zeilen tragen noch den Legacy-Status `categorized` (mit vorhandenem Vektor),
daher der Unterstand. `worker_manager.ts` zählt jetzt die Vektor-Coverage
(`embedding IS NOT NULL`) und weist `stage_legacy_categorized` separat aus.

## 4. Änderungen

- `mcp/agent-cco/tools/shared.ts`
  - `extractMetadata()` markiert Fehlschläge (`_extraction_failed`) statt still einen
    leeren Erfolg vorzutäuschen.
  - `normalizeTickers()`: trimmt, entfernt `$`/`#`, UPPERCASE, dedupliziert, validiert
    über `isValidTicker` (Preise/Jahre/Mengen fliegen raus).
  - `isValidTicker()` verwirft zusätzlich Fiat-/Stablecoin-Codes (USD, USDT, USDC, EUR, …)
    gegen False Positives wie `$USD`.
  - `updateFirstMentions()` nutzt die neue RPC `upsert_first_mention` und überschreibt
    ein früheres Datum **nicht** mehr.
- `mcp/agent-cco/workers/metadata_worker.ts`
  - Normalisiert Ticker/String-Arrays, plausibilisiert `published_at` (verhindert 1970).
  - `llm_categorized` wird nur noch bei tatsächlich erfolgreicher Extraktion gesetzt;
    Fehlschläge landen als `status='metadata_failed'` + `metadata_error`.
- `mcp/agent-cco/workers/worker_manager.ts`: korrekte Embedded-Metrik (siehe §3).
- `mcp/agent-cco/tools/pipeline_tools.ts`: neue Aktionen `REENRICH_X` (bounded,
  idempotent) und `RETRY_METADATA_FAILED`; Status zeigt Vektor- vs. Legacy-Zählung.
- `mcp/agent-cco/tools/x_tools.ts`: `search_influencer_posts` respektiert jetzt den
  dokumentierten Default `artifact_type='x_post'` (vorher wurden auch YouTube-Chunks geliefert).
- `migrations/007_first_mentions_earliest.sql`
  - `upsert_first_mention(...)` — atomar, nur frühestes Datum gewinnt.
  - `rebuild_x_first_mentions()` — baut `x_first_mentions` aus `metadata.tickers` neu.
  - `x_metadata_candidates(...)` — Keyset-paginierte Kandidatenauswahl.
- `mcp/agent-cco/scripts/backfill_x_metadata.ts` — idempotenter CLI-Backfill.
- `llm-gateway/docker-compose.yml` — `scripts/` nach `/app/scripts` gemountet.
- `system_settings.provider_config`: `cco` und `pta` von `local` auf `flash` korrigiert.

## 5. Backfill ausführen

```bash
# Trockenlauf (nur zählen)
docker exec llm-gw-mcp-cco deno run --allow-net --allow-env --allow-read \
  /app/scripts/backfill_x_metadata.ts --dry-run --limit=50

# Datumsbereich, nur Cashtag-Posts, x_first_mentions danach neu aufbauen
docker exec llm-gw-mcp-cco deno run --allow-net --allow-env --allow-read \
  /app/scripts/backfill_x_metadata.ts \
  --from=2026-09-01 --to=2026-09-17 --limit=0 --concurrency=4 --rebuild-first-mentions
```

Parameter: `--from`, `--to`, `--limit` (0 = unbegrenzt), `--page-size`, `--concurrency`,
`--all-content` (auch Posts ohne `$`), `--dry-run`, `--rebuild-first-mentions`.

Über MCP alternativ: `manage_sync_pipeline(action="REENRICH_X", start_date=..., end_date=..., limit=...)`.

## 6. Antworten auf die offenen Fragen des Berichts

1. **Gibt es ein Ticker-Feld für x_post?** Ja — `agent_workspace.metadata.tickers`;
   es wurde nur nie befüllt, weil die Extraktion an der toten Route scheiterte.
2. **Wird die LLM-Kategorisierung ausgeführt?** Vor dem Fix: nein (HTTP-Fehler → Fallback);
   das Flag wurde trotzdem gesetzt. Jetzt: ja, und das Flag ist an den Erfolg gekoppelt.
3. **Re-Enrichment-Trigger?** Jetzt ja: `REENRICH_X` (MCP) bzw. `backfill_x_metadata.ts`.
4. **Embedding-Lücke?** Keine Lücke, Zähl-Scope (§3).
5. **-32001-Timeouts?** Eigenes Thema; die Embedding-Last konkurrierte um die iGPU/CPU.
   Der neue Prioritätsparameter `priority: x_search | x_post | yt` entlastet interaktive
   Suchen. Bei anhaltenden Timeouts die Timeout-Konfiguration des Gateway prüfen.
6. **-provider failed- (91)?** Resolver-Thema (mehrdeutige/nicht-US-Namen), unabhängig
   von diesem Fix; kein Bestandteil dieses Tickets.

## 7. Ergebnis des ersten Backfill-Laufs

Bounded-Lauf über 200 Cashtag-Posts (`--limit=200 --concurrency=4 --rebuild-first-mentions`),
193 s Laufzeit, 200/200 erfolgreich:

- 128 der 200 Posts erhielten valide Ticker (die übrigen 72 enthielten kein echtes Symbol).
- `x_first_mentions` neu aufgebaut: 675 Einträge, 0 mit Datum < 2000 (vorher gab es einen
  1970-Ausreißer), 164/164 Mehrfach-Paare mit korrektem frühesten Datum.
- `discover_ticker_mentions(start_date=2026-09-16)` liefert wieder Treffer.
- Alle 9 Beispielposts aus dem Bericht sind angereichert und normalisiert (z. B.
  `$OKTA` → `["OKTA"]`, `$FND,$QXO,$DHI` → `["FND","QXO","DHI"]`, Topics statt `uncategorized`).
- Verbleibender Cashtag-Backlog nach dem Lauf: **5.606** Posts (idempotent nachholbar).

## 8. Restarbeiten

- Voller Backfill aller ~30.7k Posts bzw. der 5.734 Cashtag-Posts ist vorbereitet,
  aber noch nicht komplett gelaufen (LLM-Laufzeit). Der Lauf ist idempotent und
  kann jederzeit wiederholt werden.
- `provider_config` der übrigen Agenten (`cda`, `pca`, `system`, `ea`, `all`) steht
  weiterhin auf `local`; CDA/PCA nutzen `provider_config` derzeit nicht, sollte aber
  bei Gelegenheit bereinigt werden.
- PCA-Nebenbefund (403 auf `pca_scan_watchlist_meta`, RLS/Service-Role) separat.
