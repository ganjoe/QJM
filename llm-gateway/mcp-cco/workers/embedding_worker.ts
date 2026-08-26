import { supabase, log, getEmbeddingsBatch, EMBEDDING_BATCH_SIZE } from "../tools/shared.ts";

export const embeddingWorkerStats = {
  isRunning: false,
  startTime: 0,
  lastRunTime: 0,
  totalEmbedded: 0,
  totalErrors: 0,
  lastError: null as string | null,
};

let embeddingAbortController: AbortController | null = null;

/**
 * Process a batch of pending embedding records
 */
async function processEmbeddingBatch(batchSize: number = EMBEDDING_BATCH_SIZE): Promise<number> {
  // Fetch pending embedding records (both new 'pending_embedding' and legacy 'pending')
  const { data: posts, error } = await supabase
    .from("agent_workspace")
    .select("id, content, metadata")
    .or("status.eq.pending_embedding,status.eq.pending")
    .order("created_at", { ascending: false })
    .limit(batchSize);

  if (error) throw error;
  if (!posts || posts.length === 0) return 0;

  // Build texts for embedding
  const textsToEmbed = posts.map((p: any) => {
    const meta = p.metadata || {};
    const author = meta.author || meta.screen_name || "unknown";
    const tickers = meta.tickers && meta.tickers.length > 0 ? `Tickers: ${meta.tickers.join(", ")}` : "";
    const keywords = meta.keywords && meta.keywords.length > 0 ? `Keywords: ${meta.keywords.join(", ")}` : "";
    return `Author: ${author}\n${tickers}\n${keywords}\nContent:\n${p.content}`.trim();
  });

  // Batch embedding call via Switchyard
  const embeddings = await getEmbeddingsBatch(textsToEmbed);

  // Update records in DB
  for (let i = 0; i < posts.length; i++) {
    const post = posts[i];
    const embedding = embeddings[i];

    const { error: updErr } = await supabase
      .from("agent_workspace")
      .update({
        status: "embedded",
        embedding: embedding,
        metadata: {
          ...(post.metadata || {}),
          embedded_at: new Date().toISOString(),
          stage: "embedded",
        },
      })
      .eq("id", post.id);

    if (updErr) {
      log.error(`[Embedding Worker] Failed to update post ${post.id}: ${updErr.message}`);
      embeddingWorkerStats.totalErrors++;
    } else {
      embeddingWorkerStats.totalEmbedded++;
    }
  }

  return posts.length;
}

/**
 * Stage 3 Loop: Batch Vectorizer
 */
export async function runEmbeddingLoop() {
  embeddingWorkerStats.isRunning = true;
  embeddingWorkerStats.startTime = Date.now();
  log.info(`[Embedding Worker] Gestartet (Batch-Size: ${EMBEDDING_BATCH_SIZE})`);

  while (embeddingAbortController && !embeddingAbortController.signal.aborted) {
    try {
      embeddingWorkerStats.lastRunTime = Date.now();

      const processed = await processEmbeddingBatch(EMBEDDING_BATCH_SIZE);

      if (processed === 0) {
        // No work to do, sleep 5 seconds
        let waited = 0;
        while (waited < 5000 && embeddingAbortController && !embeddingAbortController.signal.aborted) {
          await new Promise((r) => setTimeout(r, 1000));
          waited += 1000;
        }
      } else {
        // If there were items, immediately check for the next batch with a small pause
        await new Promise((r) => setTimeout(r, 200));
      }
    } catch (err: any) {
      if (err.name === "AbortError") break;
      embeddingWorkerStats.lastError = err.message;
      log.error(`[Embedding Worker Loop Error]: ${err.message}`);
      await new Promise((r) => setTimeout(r, 10000));
    }
  }

  embeddingWorkerStats.isRunning = false;
  log.info("[Embedding Worker] Beendet.");
}

export function startEmbeddingWorker() {
  if (embeddingWorkerStats.isRunning) return;
  embeddingAbortController = new AbortController();
  runEmbeddingLoop().catch((err) => log.error(`Embedding Worker fatal error: ${err.message}`));
}

export function stopEmbeddingWorker() {
  if (embeddingAbortController) {
    embeddingAbortController.abort();
    embeddingAbortController = null;
  }
}
