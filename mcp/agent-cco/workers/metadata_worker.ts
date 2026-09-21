import { supabase, log, extractMetadata, updateFirstMentions, normalizeTickers, MAX_CONCURRENT_METADATA_WORKERS } from "../tools/shared.ts";

export const metadataWorkerStats = {
  isRunning: false,
  startTime: 0,
  lastRunTime: 0,
  totalProcessed: 0,
  totalErrors: 0,
  totalRetried: 0,
  totalRepaired: 0,
  consecutiveFailures: 0,
  lastError: null as string | null,
};

let metadataAbortController: AbortController | null = null;

// Retry-/Backoff-Steuerung fuer fehlgeschlagene Metadaten-Extraktionen.
const MAX_METADATA_ATTEMPTS = parseInt(Deno.env.get("MAX_METADATA_ATTEMPTS") || "5");
const METADATA_RETRY_BASE_MS = parseInt(Deno.env.get("METADATA_RETRY_BASE_MS") || "30000");
const METADATA_RETRY_MAX_MS = parseInt(Deno.env.get("METADATA_RETRY_MAX_MS") || "3600000");
const METADATA_QUOTA_COOLDOWN_MS = parseInt(Deno.env.get("METADATA_QUOTA_COOLDOWN_MS") || "60000");
const METADATA_QUOTA_COOLDOWN_MAX_MS = parseInt(Deno.env.get("METADATA_QUOTA_COOLDOWN_MAX_MS") || "600000");
const METADATA_RETRY_BATCH = parseInt(Deno.env.get("METADATA_RETRY_BATCH") || "10");
const METADATA_REPAIR_BATCH = parseInt(Deno.env.get("METADATA_REPAIR_BATCH") || "3");

/** Exponentielles Backoff (Basis 30 s), gedeckelt. */
function backoffMs(attempts: number): number {
  return Math.min(METADATA_RETRY_BASE_MS * 2 ** Math.max(0, attempts - 1), METADATA_RETRY_MAX_MS);
}

function sleepAbortable(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

/** published_at nur übernehmen, wenn es ein plausibles Datum (>= 2000) ist. */
function sanePublishedAt(candidate: unknown, fallback: string): string {
  const c = typeof candidate === "string" ? candidate : "";
  const t = Date.parse(c);
  if (!c || Number.isNaN(t) || t < Date.parse("2000-01-01T00:00:00Z")) return fallback;
  return c;
}

/** Normalisiert String-Arrays (keywords/topics): trimmen, leere entfernen, deduplizieren. */
function toStringArray(raw: unknown): string[] {
  if (Array.isArray(raw)) {
    return Array.from(new Set(raw.map((x) => String(x).trim()).filter(Boolean)));
  }
  if (typeof raw === "string") {
    return Array.from(new Set(raw.split(",").map((x) => x.trim()).filter(Boolean)));
  }
  return [];
}

/**
 * Markiert einen Post als fehlgeschlagen. Solange ein Retry moeglich ist, wird ein
 * Backoff-Zeitpunkt (metadata_retry_at) und ein Versuchszaehler gesetzt; der Loop
 * greift die Zeile spaeter wieder auf. Nach MAX_METADATA_ATTEMPTS endgueltig.
 */
async function markMetadataFailed(post: any, msg: string, retryable: boolean): Promise<{ attempts: number; retryAt: string | null; status: string }> {
  const existingMeta: Record<string, any> = { ...(post.metadata || {}) };
  delete existingMeta.metadata_error;
  const attempts = (Number(existingMeta.metadata_attempts) || 0) + 1;
  const canRetry = retryable && attempts < MAX_METADATA_ATTEMPTS;
  const retryAt = canRetry ? new Date(Date.now() + backoffMs(attempts)).toISOString() : null;
  // Endgueltig aufgegebene Zeilen bekommen einen eigenen Status, damit der Retry-Sweep
  // sie nicht endlos erneut auswaehlt (sonst wuerde metadata_retry_at nie greifen).
  const finalStatus = canRetry ? "metadata_failed" : "metadata_failed_permanent";

  const failedMeta: Record<string, any> = {
    ...existingMeta,
    llm_categorized: false,
    metadata_error: msg,
    metadata_attempts: attempts,
    stage: finalStatus,
  };
  if (retryAt) failedMeta.metadata_retry_at = retryAt;
  else delete failedMeta.metadata_retry_at;

  await supabase
    .from("agent_workspace")
    .update({ status: finalStatus, metadata: failedMeta })
    .eq("id", post.id);

  return { attempts, retryAt, status: finalStatus };
}

/**
 * Process a single post for metadata & ticker extraction.
 * opts.reembed=false: vorhandenen Vektor behalten (Metadata-only-Reparatur/Retry),
 * sofern die Zeile bereits embedded_at hat.
 */
export async function processSinglePost(post: any, signal?: AbortSignal, opts: { reembed?: boolean } = {}): Promise<boolean> {
  try {
    const meta = await extractMetadata(post.content, signal);
    const existingMeta: Record<string, any> = { ...(post.metadata || {}) };
    delete existingMeta.metadata_error;

    // Fehlgeschlagene LLM-Extraktion darf NICHT als kategorisiert gelten.
    if ((meta as any)._extraction_failed === true) {
      const msg = String((meta as any)._extraction_error || "extraction failed");
      const retryable = (meta as any)._retryable !== false;
      const { attempts, retryAt } = await markMetadataFailed(post, msg, retryable);
      metadataWorkerStats.totalErrors++;
      metadataWorkerStats.lastError = msg;
      log.warn(
        `[Metadata Worker] Extraktion für Post ${post.id} fehlgeschlagen (${msg}); ` +
        `Versuch ${attempts}/${MAX_METADATA_ATTEMPTS}${retryAt ? `, Retry ab ${retryAt}` : ", endgültig aufgeben"}. `,
      );
      return false;
    }

    // Gespeicherte Ingestion-Werte sind autoritativ: Das LLM sieht nur den Post-Text
    // und darf den echten Handle / das Publikationsdatum nicht überschreiben.
    const author = existingMeta.author || (meta.author as string) || "unknown";
    const tickers = normalizeTickers(meta.tickers);
    const keywords = toStringArray(meta.keywords);
    const topics = toStringArray(meta.topics);
    if (topics.length === 0) topics.push("uncategorized");
    const publishedAt = existingMeta.published_at || sanePublishedAt(meta.published_at, post.created_at);

    // Erst-Erwähnungen nur mit tatsächlich extrahierten, validen Tickern pflegen.
    await updateFirstMentions(author, tickers, publishedAt, post.id);

    // CPU-schonend: vorhandenen Vektor behalten, statt neu zu embedden (--no-reembed).
    // Der Inhalt selbst ändert sich bei Metadata-Reparaturen nicht.
    const hasExistingEmbedding = post.has_embedding === true || post.embedded_at != null;
    const skipReembed = opts.reembed === false && hasExistingEmbedding;

    const updatedMetadata: Record<string, any> = {
      ...existingMeta,
      author,
      tickers,
      keywords,
      topics,
      published_at: publishedAt,
      thought_type: meta.thought_type || meta.type || existingMeta.thought_type || "observation",
      llm_categorized: true,
      metadata_repaired: true,
      stage: skipReembed ? "embedded" : "metadata_extracted",
    };
    delete updatedMetadata.metadata_error;
    delete updatedMetadata.metadata_retry_at;
    delete updatedMetadata.metadata_attempts;

    const { error: updErr } = await supabase
      .from("agent_workspace")
      .update({
        status: skipReembed ? "embedded" : "pending_embedding", // Stage 2 output status
        metadata: updatedMetadata,
      })
      .eq("id", post.id);

    if (updErr) throw updErr;

    metadataWorkerStats.totalProcessed++;
    metadataWorkerStats.consecutiveFailures = 0;
    return true;
  } catch (err: any) {
    if (err.name === "AbortError" || signal?.aborted) throw err;
    log.error(`[Metadata Worker] Failed for post ${post.id}: ${err.message}`);
    metadataWorkerStats.totalErrors++;
    metadataWorkerStats.lastError = err.message;
    // DB-/Netzfehler: retryfaehig einstufen, damit die Zeile nicht verloren geht.
    try {
      await markMetadataFailed(post, err.message, true);
    } catch (e2: any) {
      log.error(`[Metadata Worker] Konnte Fehlerstatus für ${post.id} nicht schreiben: ${e2.message}`);
    }
    return false;
  }
}

/**
 * Fuehrt Aufgaben mit begrenzter Parallelitaet aus und liefert das Ergebnis je Item.
 */
async function runWithConcurrencyLimit<T>(
  items: T[],
  limit: number,
  fn: (item: T) => Promise<boolean>,
): Promise<boolean[]> {
  const results: boolean[] = new Array(items.length).fill(false);
  let cursor = 0;
  const workerCount = Math.max(1, Math.min(limit, items.length));
  const workers = Array.from({ length: workerCount }, async () => {
    while (true) {
      const i = cursor++;
      if (i >= items.length) return;
      try {
        results[i] = await fn(items[i]);
      } catch (e: any) {
        if (e?.name === "AbortError") return;
        results[i] = false;
      }
    }
  });
  await Promise.all(workers);
  return results;
}

async function fetchPendingPosts(): Promise<any[]> {
  const { data, error } = await supabase
    .from("agent_workspace")
    .select("id, content, created_at, metadata, embedded_at")
    .eq("status", "pending_metadata")
    .order("created_at", { ascending: false })
    .limit(20);
  if (error) throw error;
  return data || [];
}

/** Nie kategorisierte Posts (Legacy): llm_categorized fehlt komplett. */
async function fetchLegacyPosts(): Promise<any[]> {
  const { data } = await supabase
    .from("agent_workspace")
    .select("id, content, created_at, metadata, embedded_at")
    .eq("artifact_type", "x_post")
    .is("metadata->llm_categorized", null)
    .order("created_at", { ascending: false })
    .limit(10);
  return data || [];
}

/**
 * Fehlgeschlagene Posts, deren Backoff abgelaufen ist. Die Auswahl ist bewusst
 * breit (ohne attempts-Filter in SQL) und wird in JS exakt nachgefiltert.
 */
async function fetchRetryableFailed(): Promise<any[]> {
  const nowIso = new Date().toISOString();
  const { data } = await supabase
    .from("agent_workspace")
    .select("id, content, created_at, metadata, embedded_at")
    .eq("artifact_type", "x_post")
    .eq("status", "metadata_failed")
    .or(`metadata->>metadata_retry_at.is.null,metadata->>metadata_retry_at.lte.${nowIso}`)
    .order("created_at", { ascending: false })
    .limit(METADATA_RETRY_BATCH * 4);
  const now = Date.now();
  return (data || []).filter((p: any) => {
    const m = p.metadata || {};
    const attempts = Number(m.metadata_attempts) || 0;
    if (attempts >= MAX_METADATA_ATTEMPTS) return false;
    const retryAt = m.metadata_retry_at;
    if (retryAt && Date.parse(retryAt) > now) return false;
    return true;
  }).slice(0, METADATA_RETRY_BATCH);
}

/**
 * Vergiftete/kaputte Altzeilen: kategorisiert, aber ohne Ticker und noch nie repariert.
 * Die Auswahl passiert serverseitig (PostgREST-JSONB-Filter) und NICHT per Client-Filter
 * nach einem Overfetch — sonst wuerden die alten Zeilen hinter den neuen verhungern.
 * Das Flag metadata_repaired verhindert Endlos-Reprocessing.
 */
async function fetchRepairCandidates(): Promise<any[]> {
  const { data } = await supabase
    .from("agent_workspace")
    .select("id, content, created_at, metadata, embedded_at")
    .eq("artifact_type", "x_post")
    .eq("metadata->>llm_categorized", "true")
    .eq("metadata->tickers", "[]")
    .or("metadata->>metadata_repaired.is.null,metadata->>metadata_repaired.neq.true")
    .order("created_at", { ascending: true })
    .limit(METADATA_REPAIR_BATCH);
  return data || [];
}

/**
 * Stage 2 Loop: Parallel LLM Extraction mit drei Arbeitsklassen:
 *  1. pending_metadata (Neuware)
 *  2. Legacy ohne llm_categorized
 *  3. Retry fehlgeschlagener Zeilen (Backoff) und Reparatur vergifteter Altzeilen
 */
export async function runMetadataLoop() {
  metadataWorkerStats.isRunning = true;
  metadataWorkerStats.startTime = Date.now();
  log.info(
    `[Metadata Worker] Gestartet (Concurrency: ${MAX_CONCURRENT_METADATA_WORKERS}, ` +
    `Retry-Batch: ${METADATA_RETRY_BATCH}, Repair-Batch: ${METADATA_REPAIR_BATCH})`,
  );

  while (metadataAbortController && !metadataAbortController.signal.aborted) {
    try {
      metadataWorkerStats.lastRunTime = Date.now();
      const signal = metadataAbortController?.signal;

      let results: boolean[] = [];
      let mode = "idle";

      const posts = await fetchPendingPosts();
      if (posts.length > 0) {
        mode = "pending";
        results = await runWithConcurrencyLimit(posts, MAX_CONCURRENT_METADATA_WORKERS, (p) => processSinglePost(p, signal));
      } else {
        const legacy = await fetchLegacyPosts();
        if (legacy.length > 0) {
          mode = "legacy";
          results = await runWithConcurrencyLimit(legacy, MAX_CONCURRENT_METADATA_WORKERS, (p) => processSinglePost(p, signal, { reembed: false }));
        } else {
          const retry = await fetchRetryableFailed();
          if (retry.length > 0) {
            mode = "retry";
            metadataWorkerStats.totalRetried += retry.length;
            results = await runWithConcurrencyLimit(retry, MAX_CONCURRENT_METADATA_WORKERS, (p) => processSinglePost(p, signal, { reembed: false }));
          } else {
            const repair = await fetchRepairCandidates();
            if (repair.length > 0) {
              mode = "repair";
              metadataWorkerStats.totalRepaired += repair.length;
              results = await runWithConcurrencyLimit(repair, MAX_CONCURRENT_METADATA_WORKERS, (p) => processSinglePost(p, signal, { reembed: false }));
            }
          }
        }
      }

      if (results.length > 0) {
        const ok = results.filter(Boolean).length;
        const fail = results.length - ok;
        if (ok === 0 && fail > 0) {
          // Circuit Breaker: kein einziger Erfolg -> LLM/Quota wahrscheinlich nicht verfuegbar.
          metadataWorkerStats.consecutiveFailures++;
          const wait = Math.min(METADATA_QUOTA_COOLDOWN_MS * metadataWorkerStats.consecutiveFailures, METADATA_QUOTA_COOLDOWN_MAX_MS);
          log.warn(
            `[Metadata Worker] ${fail} Fehler, 0 Erfolge (Modus ${mode}) — Circuit Breaker: Pause ${Math.round(wait / 1000)}s ` +
            `(Fehlerserie ${metadataWorkerStats.consecutiveFailures}).`,
          );
          await sleepAbortable(wait, signal);
          continue;
        }
        // Kurz yielden zwischen Batches
        await sleepAbortable(100, signal);
      } else {
        // Keine Arbeit: 5 s schlafen (abbrechbar).
        await sleepAbortable(5000, signal);
      }
    } catch (err: any) {
      if (err.name === "AbortError") break;
      metadataWorkerStats.lastError = err.message;
      log.error(`[Metadata Worker Loop Error]: ${err.message}`);
      await sleepAbortable(10000, metadataAbortController?.signal);
    }
  }

  metadataWorkerStats.isRunning = false;
  log.info("[Metadata Worker] Beendet.");
}

export function startMetadataWorker() {
  if (metadataWorkerStats.isRunning) return;
  metadataAbortController = new AbortController();
  runMetadataLoop().catch((err) => log.error(`Metadata Worker fatal error: ${err.message}`));
}

export function stopMetadataWorker() {
  if (metadataAbortController) {
    metadataAbortController.abort();
    metadataAbortController = null;
  }
}
