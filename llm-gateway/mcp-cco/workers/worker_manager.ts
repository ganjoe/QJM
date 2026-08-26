import { supabase, log } from "../tools/shared.ts";
import { startXIngestion, stopXIngestion, xIngestionStats } from "./x_ingestion_worker.ts";
import { startMetadataWorker, stopMetadataWorker, metadataWorkerStats } from "./metadata_worker.ts";
import { startEmbeddingWorker, stopEmbeddingWorker, embeddingWorkerStats } from "./embedding_worker.ts";
import { startYtWorker, stopYtWorker, ytIngestionStats } from "./yt_ingestion_worker.ts";

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
    this.isInitialized = true;
  }

  public stopAll() {
    log.info("[WorkerManager] Stoppe alle Hintergrund-Worker...");
    stopXIngestion();
    stopMetadataWorker();
    stopEmbeddingWorker();
    stopYtWorker();
    this.isInitialized = false;
  }

  public async getStatus(): Promise<Record<string, any>> {
    // Query DB backlogs
    const [
      { count: pendingMetadataCount },
      { count: pendingEmbeddingCount },
      { count: embeddedCount },
      { count: totalPosts },
      { count: activeInfluencers },
    ] = await Promise.all([
      supabase.from("agent_workspace").select("*", { count: "exact", head: true }).eq("status", "pending_metadata"),
      supabase.from("agent_workspace").select("*", { count: "exact", head: true }).or("status.eq.pending_embedding,status.eq.pending"),
      supabase.from("agent_workspace").select("*", { count: "exact", head: true }).eq("status", "embedded"),
      supabase.from("agent_workspace").select("*", { count: "exact", head: true }).eq("artifact_type", "x_post"),
      supabase.from("x_users").select("*", { count: "exact", head: true }).eq("is_active", true),
    ]);

    return {
      pipeline: {
        x_ingestion: {
          running: xIngestionStats.isRunning,
          cycle_count: xIngestionStats.cycleCount,
          total_ingested: xIngestionStats.totalPostsIngested,
          last_run: xIngestionStats.lastRunTime ? new Date(xIngestionStats.lastRunTime).toISOString() : null,
          last_error: xIngestionStats.lastError,
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
          videos_processed: ytIngestionStats.totalProcessed,
          last_run: ytIngestionStats.lastRunTime ? new Date(ytIngestionStats.lastRunTime).toISOString() : null,
          last_error: ytIngestionStats.lastError,
        },
      },
      backlog: {
        stage_1_pending_metadata: pendingMetadataCount || 0,
        stage_2_pending_embedding: pendingEmbeddingCount || 0,
        stage_3_embedded: embeddedCount || 0,
        total_x_posts: totalPosts || 0,
        active_influencers: activeInfluencers || 0,
      },
    };
  }
}
