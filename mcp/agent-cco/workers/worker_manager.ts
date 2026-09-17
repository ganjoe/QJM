import { supabase, log } from "../tools/shared.ts";
import { startXIngestion, stopXIngestion, xIngestionStats } from "./x_ingestion_worker.ts";
import { startMetadataWorker, stopMetadataWorker, metadataWorkerStats } from "./metadata_worker.ts";
import { startEmbeddingWorker, stopEmbeddingWorker, embeddingWorkerStats } from "./embedding_worker.ts";
import { startYtWorker, stopYtWorker, ytIngestionStats } from "./yt_ingestion_worker.ts";
import { startCompanyExtractionWorker, stopCompanyExtractionWorker, companyExtractionStats } from "./company_extraction_worker.ts";

export class WorkerManager {
  private static instance: WorkerManager;
  private isInitialized = false;

  private constructor() {}

  public static getInstance(): WorkerManager {
    if (!WorkerManager.instance) {
      WorkerManager.instance = new WorkerManager();
    }
    return WorkerManager.instance;
  }

  public startAll() {
    log.info("[WorkerManager] Starte alle Hintergrund-Worker...");
    startXIngestion();
    startMetadataWorker();
    startEmbeddingWorker();
    startYtWorker();
    startCompanyExtractionWorker();
    this.isInitialized = true;
  }

  public stopAll() {
    log.info("[WorkerManager] Stoppe alle Hintergrund-Worker...");
    stopXIngestion();
    stopMetadataWorker();
    stopEmbeddingWorker();
    stopYtWorker();
    stopCompanyExtractionWorker();
    this.isInitialized = false;
  }

  public async getStatus(): Promise<Record<string, any>> {
    // Query DB backlogs
    const [
      { count: pendingMetadataCount },
      { count: pendingEmbeddingCount },
      { count: embeddedXCount },
      { count: totalPosts },
      { count: legacyCategorizedCount },
      { count: activeInfluencers },
      { count: activeYtChannels },
      { count: ytPendingVideos },
      { count: ytDownloadedTranscripts },
      { count: ytEmbeddedVideos },
      { count: ytEmbeddedChunks },
      { count: ytFailedVideos },
      { data: ingestionStateRow },
    ] = await Promise.all([
      supabase.from("agent_workspace").select("*", { count: "exact", head: true }).eq("status", "pending_metadata"),
      supabase.from("agent_workspace").select("*", { count: "exact", head: true }).or("status.eq.pending_embedding,status.eq.pending"),
      // Vektor-Coverage unabhängig vom Status-Label: embedding IS NOT NULL.
      supabase.from("agent_workspace").select("*", { count: "exact", head: true }).eq("artifact_type", "x_post").not("embedding", "is", null),
      supabase.from("agent_workspace").select("*", { count: "exact", head: true }).eq("artifact_type", "x_post"),
      // Legacy-Zeilen mit vorhandenem Vektor, aber altem Status 'categorized' (erklärt 31k vs 20k).
      supabase.from("agent_workspace").select("*", { count: "exact", head: true }).eq("artifact_type", "x_post").eq("status", "categorized"),
      supabase.from("x_users").select("*", { count: "exact", head: true }).eq("is_active", true),
      supabase.from("yt_channels").select("*", { count: "exact", head: true }).eq("is_active", true),
      supabase.from("yt_videos").select("*", { count: "exact", head: true }).eq("status", "pending"),
      supabase.from("yt_videos").select("*", { count: "exact", head: true }).eq("status", "downloaded"),
      supabase.from("yt_videos").select("*", { count: "exact", head: true }).eq("status", "embedded"),
      supabase.from("agent_workspace").select("*", { count: "exact", head: true }).eq("artifact_type", "yt_chunk"),
      supabase.from("yt_videos").select("*", { count: "exact", head: true }).eq("status", "failed"),
      supabase.from("system_settings").select("value").eq("key", "x_ingestion_state").single(),
    ]);

    return {
      pipeline: {
        x_ingestion: {
          running: xIngestionStats.isRunning,
          cycle_count: xIngestionStats.cycleCount,
          total_ingested: xIngestionStats.totalPostsIngested,
          last_run: xIngestionStats.lastRunTime ? new Date(xIngestionStats.lastRunTime).toISOString() : null,
          last_error: xIngestionStats.lastError,
          uptime_ms: xIngestionStats.startTime ? Date.now() - xIngestionStats.startTime : 0,
          cost: {
            last_cycle_tweets_fetched: xIngestionStats.lastCycleTweetsFetched,
            last_cycle_users_checked: xIngestionStats.lastCycleUsersChecked,
            last_cycle_users_synced: xIngestionStats.lastCycleUsersSynced,
            last_cycle_credits: xIngestionStats.lastCycleCredits,
            total_tweets_fetched: xIngestionStats.totalTweetsFetched,
            total_credits: xIngestionStats.totalCredits,
            last_search_saved: xIngestionStats.lastSearchSaved,
            last_search_failed: xIngestionStats.lastSearchFailed,
            last_search_since: xIngestionStats.lastSearchSince
              ? new Date(xIngestionStats.lastSearchSince * 1000).toISOString()
              : null,
            last_reconcile_saved: xIngestionStats.lastReconcileSaved,
            last_reconcile_at: xIngestionStats.lastReconcileAt
              ? new Date(xIngestionStats.lastReconcileAt).toISOString()
              : null,
          },
          persisted_state: ingestionStateRow?.value
            ? {
              search_since: Number((ingestionStateRow.value as any).search_since) || 0,
              search_since_iso: Number((ingestionStateRow.value as any).search_since)
                ? new Date(Number((ingestionStateRow.value as any).search_since) * 1000).toISOString()
                : null,
              last_reconcile: Number((ingestionStateRow.value as any).last_reconcile) || 0,
              last_reconcile_iso: Number((ingestionStateRow.value as any).last_reconcile)
                ? new Date(Number((ingestionStateRow.value as any).last_reconcile)).toISOString()
                : null,
            }
            : null,
        },
        metadata_worker: {
          running: metadataWorkerStats.isRunning,
          total_processed: metadataWorkerStats.totalProcessed,
          total_errors: metadataWorkerStats.totalErrors,
          last_run: metadataWorkerStats.lastRunTime ? new Date(metadataWorkerStats.lastRunTime).toISOString() : null,
          last_error: metadataWorkerStats.lastError,
        },
        embedding_worker: {
          running: embeddingWorkerStats.isRunning,
          total_embedded: embeddingWorkerStats.totalEmbedded,
          total_errors: embeddingWorkerStats.totalErrors,
          last_run: embeddingWorkerStats.lastRunTime ? new Date(embeddingWorkerStats.lastRunTime).toISOString() : null,
          last_error: embeddingWorkerStats.lastError,
        },
        youtube_worker: {
          running: ytIngestionStats.isRunning,
          videos_discovered: ytIngestionStats.totalVideosDiscovered,
          transcripts_downloaded: ytIngestionStats.totalTranscriptsDownloaded,
          videos_processed: ytIngestionStats.totalVideosProcessed,
          chunks_processed: ytIngestionStats.totalChunksProcessed,
          speed: {
            chunks_per_sec: ytIngestionStats.currentChunksPerSec,
            tokens_per_sec: ytIngestionStats.currentTokensPerSec,
            avg_chunk_latency_ms: ytIngestionStats.avgChunkLatencyMs,
            last_batch_chunks: ytIngestionStats.lastBatchChunks,
            last_batch_duration_ms: ytIngestionStats.lastBatchDurationMs,
            active_downloads: ytIngestionStats.activeDownloadsRunning,
            active_embeddings: ytIngestionStats.activeEmbeddingsRunning,
            last_processed_title: ytIngestionStats.lastProcessedTitle,
            last_processed_channel: ytIngestionStats.lastProcessedChannel,
          },
          last_run: ytIngestionStats.lastRunTime ? new Date(ytIngestionStats.lastRunTime).toISOString() : null,
          last_error: ytIngestionStats.lastError,
        },
        company_extraction: {
          running: companyExtractionStats.isRunning,
          videos_scanned: companyExtractionStats.totalVideosScanned,
          companies_extracted: companyExtractionStats.totalCompaniesExtracted,
          tickers_resolved: companyExtractionStats.totalResolvedTickers,
          tickers_failed: companyExtractionStats.totalFailedTickers,
          last_run: companyExtractionStats.lastRunTime ? new Date(companyExtractionStats.lastRunTime).toISOString() : null,
          last_error: companyExtractionStats.lastError,
        },
      },
      backlog: {
        x_posts: {
          stage_1_pending_metadata: pendingMetadataCount || 0,
          stage_2_pending_embedding: pendingEmbeddingCount || 0,
          // Vektor-Coverage (embedding IS NOT NULL), nicht nur Status='embedded'.
          stage_3_embedded: embeddedXCount || 0,
          // Legacy-Zeilen, die bereits einen Vektor haben, aber noch Status 'categorized' tragen.
          stage_legacy_categorized: legacyCategorizedCount || 0,
          total: totalPosts || 0,
          active_influencers: activeInfluencers || 0,
        },
        youtube: {
          active_channels: activeYtChannels || 0,
          pending_discovery: ytPendingVideos || 0,
          transcripts_in_queue: ytDownloadedTranscripts || 0,
          videos_embedded: ytEmbeddedVideos || 0,
          total_chunks_in_workspace: ytEmbeddedChunks || 0,
          failed_videos: ytFailedVideos || 0,
        },
      },
    };
  }
}
