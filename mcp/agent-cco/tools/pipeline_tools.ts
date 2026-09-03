import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { WorkerManager } from "../workers/worker_manager.ts";
import { supabase } from "./shared.ts";

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
            `=== CCO Sync & Embedding Pipeline Status ===`,
            ``,
            `📊 X-Posts (agent_workspace):`,
            `  • In Bearbeitung (Metadaten): ${status.backlog.x_posts.stage_1_pending_metadata} Posts`,
            `  • Warteschlange Vektorisierung: ${status.backlog.x_posts.stage_2_pending_embedding} Posts`,
            `  • Fertig gevektort (embedded): ${status.backlog.x_posts.stage_3_embedded} Posts`,
            `  • Gesamtanzahl X Posts: ${status.backlog.x_posts.total}`,
            `  • Aktive Influencer: ${status.backlog.x_posts.active_influencers}`,
            ``,
            `📺 YouTube Pipeline (yt_videos & agent_workspace):`,
            `  • Aktive Kanäle: ${status.backlog.youtube.active_channels}`,
            `  • Transkripte in Warteschlange (downloaded): ${status.backlog.youtube.transcripts_in_queue} Videos`,
            `  • Fertig gevektorte Videos (embedded): ${status.backlog.youtube.videos_embedded} Videos`,
            `  • Fertige Vektor-Chunks (yt_chunk): ${status.backlog.youtube.total_chunks_in_workspace} Chunks`,
            `  • Discovery ausstehend: ${status.backlog.youtube.pending_discovery} Videos`,
            `  • Fehlerhafte Videos: ${status.backlog.youtube.failed_videos} Videos`,
            ``,
            `⚙️ Worker Durchsatz & Status:`,
            `  • X Ingestion: ${status.pipeline.x_ingestion.running ? 'LÄUFT 🟢' : 'GESTOPPT 🔴'} (Zyklen: ${status.pipeline.x_ingestion.cycle_count}, Ingested: ${status.pipeline.x_ingestion.total_ingested})`,
            `  • YouTube Worker: ${status.pipeline.youtube_worker.running ? 'LÄUFT 🟢' : 'GESTOPPT 🔴'} (Discovered: ${status.pipeline.youtube_worker.videos_discovered}, Downloaded: ${status.pipeline.youtube_worker.transcripts_downloaded || 0}, Processed: ${status.pipeline.youtube_worker.videos_processed}, Chunks: ${status.pipeline.youtube_worker.chunks_processed || 0})`,
            `  • Company & Ticker Extractor: ${status.pipeline.company_extraction?.running ? 'LÄUFT 🟢' : 'GESTOPPT 🔴'} (Videos: ${status.pipeline.company_extraction?.videos_scanned || 0}, Firmen: ${status.pipeline.company_extraction?.companies_extracted || 0}, Ticker gelöst: ${status.pipeline.company_extraction?.tickers_resolved || 0}, Failed: ${status.pipeline.company_extraction?.tickers_failed || 0})`,
            ``,
            `⚡ YouTube Throughput & Speed (Live):`,
            `  • Geschwindigkeit: ${status.pipeline.youtube_worker.speed?.chunks_per_sec || 0} Chunks/s (~${status.pipeline.youtube_worker.speed?.tokens_per_sec || 0} Tokens/s)`,
            `  • Latenz: ${status.pipeline.youtube_worker.speed?.avg_chunk_latency_ms || 0} ms pro Chunk`,
            `  • Letzter Batch: ${status.pipeline.youtube_worker.speed?.last_batch_chunks || 0} Chunks in ${((status.pipeline.youtube_worker.speed?.last_batch_duration_ms || 0) / 1000).toFixed(2)}s`,
            `  • Status: Downloads aktiv: ${status.pipeline.youtube_worker.speed?.active_downloads || 0} | GPU-Inferenz aktiv: ${status.pipeline.youtube_worker.speed?.active_embeddings || 0}`,
            `  • Zuletzt eingebettet: "${status.pipeline.youtube_worker.speed?.last_processed_title || "N/A"}" (${status.pipeline.youtube_worker.speed?.last_processed_channel || ""})`,
          ];

          let gpuSection = "";
          try {
            const dashboardUrl = Deno.env.get("DASHBOARD_URL") || "http://localhost:9000";
            let res: Response | null = null;
            try {
              res = await fetch(`${dashboardUrl}/api/system/metrics`);
            } catch (_e) {
              try {
                res = await fetch("http://dashboard:9000/api/system/metrics");
              } catch (_e2) {
                res = await fetch("http://host.docker.internal:9000/api/system/metrics");
              }
            }
            if (res && res.ok) {
              const d = await res.json();
              const vramUsedGb = d.vram_used_mb ? (d.vram_used_mb / 1024).toFixed(2) : "N/A";
              const vramTotalGb = d.vram_total_mb ? (d.vram_total_mb / 1024).toFixed(2) : "N/A";
              const vramFreeGb = (d.vram_total_mb && d.vram_used_mb) ? ((d.vram_total_mb - d.vram_used_mb) / 1024).toFixed(2) : "N/A";
              gpuSection = [
                ``,
                `🎮 GPU & Hardware Performance (${d.gpu_name || "AMD Radeon dGPU 32GB"}):`,
                `  • GPU Auslastung: ${d.gpu_util ?? 0} %`,
                `  • VRAM: ${vramUsedGb} GB / ${vramTotalGb} GB (${d.vram_percent ?? 0} %) | Frei: ${vramFreeGb} GB`,
                `  • Power: ${d.gpu_power ?? 0} W / ${d.gpu_power_cap ?? 300} W`,
                `  • Temperatur: ${d.gpu_temp ?? 0}°C (Hotspot: ${d.gpu_temp_hotspot ?? 0}°C, Mem: ${d.gpu_temp_mem ?? 0}°C)`,
                `  • Takt: ${d.gpu_clock_mhz ?? 0} MHz | Lüfter: ${d.gpu_fan_rpm ?? 0} RPM`,
              ].join("\n");
            }
          } catch (_err) {}

          const fullOutput = lines.join("\n") + (gpuSection ? gpuSection : "");
          return { content: [{ type: "text", text: fullOutput }] };
        }

        if (action === "START") {
          manager.startAll();
          return { content: [{ type: "text", text: "✅ Alle Hintergrund-Worker wurden gestartet (X Ingestion, Metadata Worker, Embedding Worker, YouTube Worker, Company Extractor)." }] };
        }

        if (action === "STOP") {
          manager.stopAll();
          return { content: [{ type: "text", text: "🛑 Alle Hintergrund-Worker wurden gestoppt." }] };
        }

        if (action === "RETRY_FAILED") {
          const { count } = await supabase.from("yt_videos").update({
            status: "pending",
            error_msg: null,
          }, { count: "exact" }).eq("status", "failed");

          return { content: [{ type: "text", text: `🔄 ${count || 0} fehlgeschlagene YouTube-Videos wurden auf 'pending' zurückgesetzt.` }] };
        }

        return { content: [{ type: "text", text: `Unbekannte Aktion: ${action}` }], isError: true };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  server.registerTool(
    "get_system_metrics",
    {
      title: "Get System & GPU Performance Metrics",
      description: "Returns real-time GPU hardware metrics (VRAM usage, GPU utilization, power draw in Watts, temperature, clocks for AMD dGPU) and system CPU/RAM metrics.",
      inputSchema: {},
    },
    async () => {
      try {
        const dashboardUrl = Deno.env.get("DASHBOARD_URL") || "http://localhost:9000";
        let res: Response | null = null;
        try {
          res = await fetch(`${dashboardUrl}/api/system/metrics`);
        } catch (_e) {
          try {
            res = await fetch("http://dashboard:9000/api/system/metrics");
          } catch (_e2) {
            res = await fetch("http://host.docker.internal:9000/api/system/metrics");
          }
        }
        if (!res || !res.ok) {
          throw new Error(`Dashboard metrics API nicht erreichbar oder HTTP ${res?.status}`);
        }
        const data = await res.json();
        const vramUsedGb = data.vram_used_mb ? (data.vram_used_mb / 1024).toFixed(2) : "N/A";
        const vramTotalGb = data.vram_total_mb ? (data.vram_total_mb / 1024).toFixed(2) : "N/A";
        const vramFreeGb = (data.vram_total_mb && data.vram_used_mb) ? ((data.vram_total_mb - data.vram_used_mb) / 1024).toFixed(2) : "N/A";

        const lines = [
          `=== AMD GPU & System Performance Metrics ===`,
          `🖥️ GPU Modell: ${data.gpu_name || "AMD Radeon (dGPU 32GB)"}`,
          `⚡ GPU Auslastung: ${data.gpu_util ?? "N/A"} %`,
          `💾 VRAM Belegung: ${vramUsedGb} GB / ${vramTotalGb} GB (${data.vram_percent ?? "N/A"} %) | Frei: ${vramFreeGb} GB`,
          `🔥 Leistungsaufnahme: ${data.gpu_power ?? "N/A"} W / ${data.gpu_power_cap ?? "300"} W`,
          `🌡️ Temperatur: Edge ${data.gpu_temp ?? "N/A"}°C | Hotspot ${data.gpu_temp_hotspot ?? "N/A"}°C | Mem ${data.gpu_temp_mem ?? "N/A"}°C`,
          `⏱️ GPU Takt: ${data.gpu_clock_mhz ?? "N/A"} MHz | Lüfter: ${data.gpu_fan_rpm ?? "N/A"} RPM (${data.gpu_fan_percent ?? "N/A"} %)`,
          ``,
          `💻 System: CPU: ${data.cpu_percent ?? "N/A"} % (${data.cpu_count ?? "N/A"} Cores) | RAM: ${data.ram_used_gb?.toFixed(1) ?? "N/A"} GB / ${data.ram_total_gb?.toFixed(1) ?? "N/A"} GB (${data.ram_percent ?? "N/A"} %)`,
        ];
        return {
          content: [{ type: "text", text: lines.join("\n") }],
        };
      } catch (err: any) {
        return {
          content: [{ type: "text", text: `Fehler beim Abrufen der System-Metriken: ${err.message}` }],
          isError: true,
        };
      }
    }
  );
}
