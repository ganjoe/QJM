import { supabase, log, extractMetadata, updateFirstMentions, normalizeTickers, MAX_CONCURRENT_METADATA_WORKERS } from "../tools/shared.ts";

export const metadataWorkerStats = {
  isRunning: false,
  startTime: 0,
  lastRunTime: 0,
  totalProcessed: 0,
  totalErrors: 0,
  lastError: null as string | null,
};

let metadataAbortController: AbortController | null = null;

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
 * Process a single post for metadata & ticker extraction
 */
export async function processSinglePost(post: any, signal?: AbortSignal, opts: { reembed?: boolean } = {}): Promise<boolean> {
  try {
    const meta = await extractMetadata(post.content, signal);
    const existingMeta: Record<string, any> = { ...(post.metadata || {}) };
    delete existingMeta.metadata_error;

    // Fehlgeschlagene LLM-Extraktion darf NICHT als kategorisiert gelten.
    if ((meta as any)._extraction_failed === true) {
      const msg = String((meta as any)._extraction_error || "extraction failed");
      log.warn(`[Metadata Worker] Extraktion für Post ${post.id} fehlgeschlagen (${msg}); status=metadata_failed.`);
      metadataWorkerStats.totalErrors++;
      await supabase
        .from("agent_workspace")
        .update({
          status: "metadata_failed",
          metadata: { ...existingMeta, llm_categorized: false, metadata_error: msg, stage: "metadata_failed" },
        })
        .eq("id", post.id);
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
    const skipReembed = opts.reembed === false && post.has_embedding === true;

    const updatedMetadata = {
      ...existingMeta,
      author,
      tickers,
      keywords,
      topics,
      published_at: publishedAt,
      thought_type: meta.thought_type || meta.type || existingMeta.thought_type || "observation",
      llm_categorized: true,
      stage: skipReembed ? "embedded" : "metadata_extracted",
    };

    const { error: updErr } = await supabase
      .from("agent_workspace")
      .update({
        status: skipReembed ? "embedded" : "pending_embedding", // Stage 2 output status
        metadata: updatedMetadata,
      })
      .eq("id", post.id);

    if (updErr) throw updErr;

    metadataWorkerStats.totalProcessed++;
    return true;
  } catch (err: any) {
    if (err.name === "AbortError" || signal?.aborted) throw err;
    log.error(`[Metadata Worker] Failed for post ${post.id}: ${err.message}`);
    metadataWorkerStats.totalErrors++;

    // Mark as failed or retryable
    await supabase
      .from("agent_workspace")
      .update({
        status: "metadata_failed",
        metadata: { ...(post.metadata || {}), llm_categorized: false, metadata_error: err.message, stage: "metadata_failed" },
      })
      .eq("id", post.id);

    return false;
  }
}

/**
 * Helper to run async tasks with concurrency limit
 */
async function runWithConcurrencyLimit<T>(items: T[], limit: number, fn: (item: T) => Promise<any>): Promise<void> {
  const executing = new Set<Promise<any>>();
  for (const item of items) {
    const p: Promise<any> = fn(item).then(() => {
      executing.delete(p);
    });
    executing.add(p);
    if (executing.size >= limit) {
      await Promise.race(executing);
    }
  }
  await Promise.all(executing);
}

/**
 * Stage 2 Loop: Parallel LLM Extraction
 */
export async function runMetadataLoop() {
  metadataWorkerStats.isRunning = true;
  metadataWorkerStats.startTime = Date.now();
  log.info(`[Metadata Worker] Gestartet (Concurrency: ${MAX_CONCURRENT_METADATA_WORKERS})`);

  while (metadataAbortController && !metadataAbortController.signal.aborted) {
    try {
      metadataWorkerStats.lastRunTime = Date.now();

      // Fetch pending items
      const { data: posts, error } = await supabase
        .from("agent_workspace")
        .select("id, content, created_at, metadata")
        .eq("status", "pending_metadata")
        .order("created_at", { ascending: false })
        .limit(20);

      if (error) throw error;

      if (!posts || posts.length === 0) {
        // Also check if there are legacy posts without llm_categorized
        const { data: legacyPosts } = await supabase
          .from("agent_workspace")
          .select("id, content, created_at, metadata")
          .eq("artifact_type", "x_post")
          .is("metadata->llm_categorized", null)
          .order("created_at", { ascending: false })
          .limit(10);

        if (!legacyPosts || legacyPosts.length === 0) {
          // No work to do, sleep 5 seconds
          let waited = 0;
          while (waited < 5000 && metadataAbortController && !metadataAbortController.signal.aborted) {
            await new Promise((r) => setTimeout(r, 1000));
            waited += 1000;
          }
          continue;
        }

        // Process legacy posts
        await runWithConcurrencyLimit(
          legacyPosts,
          MAX_CONCURRENT_METADATA_WORKERS,
          (p) => processSinglePost(p, metadataAbortController?.signal)
        );
        continue;
      }

      // Process current batch in parallel
      await runWithConcurrencyLimit(
        posts,
        MAX_CONCURRENT_METADATA_WORKERS,
        (p) => processSinglePost(p, metadataAbortController?.signal)
      );

      // Yield briefly between batches
      await new Promise((r) => setTimeout(r, 100));
    } catch (err: any) {
      if (err.name === "AbortError") break;
      metadataWorkerStats.lastError = err.message;
      log.error(`[Metadata Worker Loop Error]: ${err.message}`);
      await new Promise((r) => setTimeout(r, 10000));
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
