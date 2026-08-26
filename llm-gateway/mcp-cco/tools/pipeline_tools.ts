import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { WorkerManager } from "../workers/worker_manager.ts";

export function registerPipelineTools(server: McpServer) {
  const manager = WorkerManager.getInstance();

  server.registerTool(
    "manage_sync_pipeline",
    {
      title: "Manage Sync Pipeline",
      description: "Controls the background sync workers (Stage 1 Ingestion, Stage 2 Metadata, Stage 3 Embeddings, YouTube) or checks backlog and throughput metrics.",
      inputSchema: {
        action: z.enum(["START", "STOP", "STATUS"]).describe("START = start all workers, STOP = stop all workers, STATUS = show pipeline and backlog stats"),
      },
    },
    async ({ action }: any) => {
      try {
        if (action === "START") {
          manager.startAll();
          return { content: [{ type: "text", text: "✅ Alle Hintergrund-Worker (Ingestion, Metadata, Embedding, YouTube) wurden gestartet." }] };
        }

        if (action === "STOP") {
          manager.stopAll();
          return { content: [{ type: "text", text: "⏹️ Abbruchsignal wurde an alle Hintergrund-Worker gesendet." }] };
        }

        if (action === "STATUS") {
          const status = await manager.getStatus();
          const lines = [
            `=== CCO Sync Pipeline Status ===`,
            ``,
            `📊 Backlog & Datenbank:`,
            `  • Stage 1 (Pending Metadata): ${status.backlog.stage_1_pending_metadata} Posts`,
            `  • Stage 2 (Pending Embedding): ${status.backlog.stage_2_pending_embedding} Posts`,
            `  • Stage 3 (Vollständig Embedded): ${status.backlog.stage_3_embedded} Posts`,
            `  • Gesamtanzahl X Posts: ${status.backlog.total_x_posts}`,
            `  • Aktive Influencer: ${status.backlog.active_influencers}`,
            ``,
            `⚙️ Worker Status:`,
            `  • X Ingestion: ${status.pipeline.x_ingestion.running ? 'LÄUFT 🟢' : 'GESTOPPT 🔴'} (Zyklen: ${status.pipeline.x_ingestion.cycle_count}, Ingested: ${status.pipeline.x_ingestion.total_ingested})`,
            `  • Metadata Worker: ${status.pipeline.metadata_worker.running ? 'LÄUFT 🟢' : 'GESTOPPT 🔴'} (Processed: ${status.pipeline.metadata_worker.total_processed}, Errors: ${status.pipeline.metadata_worker.total_errors})`,
            `  • Embedding Worker: ${status.pipeline.embedding_worker.running ? 'LÄUFT 🟢' : 'GESTOPPT 🔴'} (Embedded: ${status.pipeline.embedding_worker.total_embedded}, Errors: ${status.pipeline.embedding_worker.total_errors})`,
            `  • YouTube Worker: ${status.pipeline.youtube_worker.running ? 'LÄUFT 🟢' : 'GESTOPPT 🔴'} (Discovered: ${status.pipeline.youtube_worker.videos_discovered}, Processed: ${status.pipeline.youtube_worker.videos_processed})`,
          ];

          return { content: [{ type: "text", text: lines.join("\n") }] };
        }

        return { content: [{ type: "text", text: `Unbekannte Aktion: ${action}` }], isError: true };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );
}
