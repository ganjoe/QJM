import { supabase, log, extractMetadata, updateFirstMentions, MAX_CONCURRENT_METADATA_WORKERS } from "../tools/shared.ts";

export const metadataWorkerStats = {
  isRunning: false,
  startTime: 0,
  lastRunTime: 0,
  totalProcessed: 0,
  totalErrors: 0,
  lastError: null as string | null,
};

let metadataAbortController: AbortController | null = null;

/**
 * Process a single post for metadata & ticker extraction
 */
async function processSinglePost(post: any, signal?: AbortSignal): Promise<boolean> {
  try {
    const meta = await extractMetadata(post.content, signal);
    const existingMeta = post.metadata || {};

    const author = meta.author || existingMeta.author || "unknown";
    const rawTickers = (meta.tickers as string[]) || [];
    const tickers = Array.isArray(rawTickers) ? rawTickers : [];
    const keywords = (meta.keywords as string[]) || [];
    const topics = (meta.topics as string[]) || [];
    const publishedAt = (meta.published_at as string) || existingMeta.published_at || post.created_at;

    // Track first mentions of tickers
    await updateFirstMentions(author as string, tickers, publishedAt, post.id);

    const updatedMetadata = {
      ...existingMeta,
      author,
      tickers,
      keywords,
      topics,
      published_at: publishedAt,
      thought_type: meta.thought_type || "observation",
      llm_categorized: true,
      stage: "metadata_extracted",
    };

    const { error: updErr } = await supabase
      .from("agent_workspace")
      .update({
        status: "pending_embedding", // Stage 2 output status
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
        metadata: { ...(post.metadata || {}), metadata_error: err.message },
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
    const p = fn(item).then(() => executing.delete(p));
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
