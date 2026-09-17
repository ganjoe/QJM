/**
 * Idempotenter Metadaten-Backfill für X-Posts (agent_workspace, artifact_type='x_post').
 *
 * Verarbeitet ausschließlich Posts, deren metadata.tickers leer ist, über denselben
 * Code-Pfad wie der Metadata-Worker (processSinglePost) und normalisiert/validiert
 * die Ticker. Der Lauf ist idempotent: verarbeitete Posts fallen aus der
 * Kandidatenmenge, ein zweiter Lauf verarbeitet nur noch die verbleibenden.
 * Optional wird x_first_mentions aus den korrigierten Tickern neu aufgebaut.
 *
 * Beispiele:
 *   # letzte 30 Tage, nur Posts mit $, max 500
 *   deno run --allow-net --allow-env --allow-read /app/scripts/backfill_x_metadata.ts \
 *     --from=2026-08-17 --to=2026-09-17 --limit=500 --rebuild-first-mentions
 *
 *   # Trockenlauf (nur zählen, keine Schreibzugriffe)
 *   deno run ... --dry-run --all-content
 *
 * Parameter:
 *   --from=ISO               untere created_at-Grenze
 *   --to=ISO                 obere created_at-Grenze
 *   --limit=N                0 = unbegrenzt (Default 0)
 *   --page-size=N            Kandidaten pro DB-Abruf (Default 200, max 500)
 *   --concurrency=N          parallele LLM-Extraktionen (Default 4)
 *   --all-content            auch Posts ohne '$' verarbeiten (Default: nur Cashtag-Posts)
 *   --dry-run                nichts schreiben, nur Kandidaten zählen
 *   --rebuild-first-mentions am Ende x_first_mentions neu aufbauen
 *   --no-reembed             vorhandenes Embedding behalten (keine lokale CPU-Last durch Ollama)
 */
import { supabase, log } from "../tools/shared.ts";
import { processSinglePost } from "../workers/metadata_worker.ts";

function flag(name: string): string | undefined {
  const hit = Deno.args.find((a) => a === `--${name}` || a.startsWith(`--${name}=`));
  if (!hit) return undefined;
  const eq = hit.indexOf("=");
  return eq === -1 ? "true" : hit.slice(eq + 1);
}
function num(name: string, def: number): number {
  const v = Number(flag(name));
  return Number.isFinite(v) ? v : def;
}

const from = flag("from") ?? null;
const to = flag("to") ?? null;
const limit = Math.max(0, num("limit", 0));
const pageSize = Math.max(1, Math.min(num("page-size", 200), 500));
const concurrency = Math.max(1, num("concurrency", 4));
const onlyCashtag = flag("all-content") !== "true";
const dryRun = flag("dry-run") === "true";
const rebuild = flag("rebuild-first-mentions") === "true";
// CPU-schonend: vorhandenes Embedding behalten (kein lokales Re-Embedding).
const noReembed = flag("no-reembed") === "true";

let cursorAt: string | null = null;
let cursorId: string | null = null;
let processed = 0, ok = 0, fail = 0, pages = 0;
const started = Date.now();

log.info(
  `[Backfill] from=${from ?? "∞"} to=${to ?? "∞"} limit=${limit || "∞"} page=${pageSize} concurrency=${concurrency} cashtagOnly=${onlyCashtag} dryRun=${dryRun}`,
);

while (true) {
  const remaining = limit > 0 ? limit - processed : pageSize;
  if (remaining <= 0) break;
  const take = Math.min(pageSize, remaining);

  const { data, error } = await supabase.rpc("x_metadata_candidates", {
    p_from: from,
    p_to: to,
    p_only_cashtag: onlyCashtag,
    p_limit: take,
    p_cursor_at: cursorAt,
    p_cursor_id: cursorId,
  });
  if (error) throw new Error(`x_metadata_candidates: ${error.message}`);
  const batch: any[] = data || [];
  if (batch.length === 0) break;

  pages++;
  const last = batch[batch.length - 1];
  cursorAt = last.created_at;
  cursorId = last.id;

  if (dryRun) {
    processed += batch.length;
    log.info(`[Backfill] (dry-run) Seite ${pages}: +${batch.length} Kandidaten, total=${processed}`);
    if (batch.length < take) break;
    continue;
  }

  for (let i = 0; i < batch.length; i += concurrency) {
    const slice = batch.slice(i, i + concurrency);
    const results = await Promise.all(slice.map((p) => processSinglePost(p, undefined, { reembed: !noReembed })));
    ok += results.filter(Boolean).length;
    fail += results.filter((r) => !r).length;
    processed += slice.length;
  }
  const secs = ((Date.now() - started) / 1000).toFixed(0);
  log.info(`[Backfill] Seite ${pages}: processed=${processed} ok=${ok} fail=${fail} cursor=${cursorAt} elapsed=${secs}s`);
  if (batch.length < take) break;
}

if (rebuild && !dryRun) {
  const { data, error } = await supabase.rpc("rebuild_x_first_mentions");
  if (error) log.error(`[Backfill] rebuild_x_first_mentions fehlgeschlagen: ${error.message}`);
  else log.info(`[Backfill] x_first_mentions neu aufgebaut: ${data} Einträge`);
}

log.info(
  `[Backfill] fertig: processed=${processed} ok=${ok} fail=${fail} pages=${pages} in ${((Date.now() - started) / 1000).toFixed(1)}s`,
);
