import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { WorkerManager } from "../workers/worker_manager.ts";
import { supabase, isTwitterApiIoAvailable, X_BEARER_TOKEN, X_CLIENT_ID, TWITTER_API_IO_KEY, X_DISCOVERY_INTERVAL_SEC, X_LIVENESS_CHECK, X_INGESTION_MODE, X_SEARCH_INTERVAL_SEC, X_RECONCILE_INTERVAL_SEC } from "./shared.ts";
import { processSinglePost } from "../workers/metadata_worker.ts";

export function registerPipelineTools(server: McpServer) {
  const manager = WorkerManager.getInstance();

  server.registerTool(
    "manage_sync_pipeline",
    {
      title: "Manage Sync Pipeline",
      description: "Controls the background sync workers (Stage 1 Ingestion, Stage 2 Metadata, Stage 3 Embeddings, YouTube) or checks backlog and throughput metrics.",
      inputSchema: {
        action: z.enum(["START", "STOP", "STATUS", "REENRICH_X", "REPAIR_X_METADATA", "RETRY_METADATA_FAILED"]).describe("START/STOP = worker control, STATUS = stats, REENRICH_X = idempotenter Metadaten-Backfill für X-Posts (Datumsbereich), REPAIR_X_METADATA = vergiftete Altzeilen (unkategorisiert/ohne Keywords) reparieren, RETRY_METADATA_FAILED = fehlgeschlagene X-Posts erneut einreihen"),
        start_date: z.string().optional().describe("REENRICH_X: nur Posts ab diesem created_at (ISO, z.B. 2026-09-01)"),
        end_date: z.string().optional().describe("REENRICH_X: nur Posts bis zu diesem created_at (ISO)"),
        only_with_cashtags: z.boolean().optional().default(true).describe("REENRICH_X: nur Posts mit '$' im Text (Default: true)"),
        limit: z.number().optional().default(100).describe("REENRICH_X: max. Posts pro Aufruf (1-1000)"),
      },
    },
    async ({ action, start_date, end_date, only_with_cashtags, limit }: any) => {
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
            `🔑 API-Provider:`,
            `  • TwitterAPI.io (fremde Profile): ${isTwitterApiIoAvailable() ? '🟢 aktiv' : '🔴 NICHT KONFIGURIERT (TWITTER_API_IO_KEY fehlt)'}`,
            `  • Offizielle X API (Bookmarks/OAuth): ${X_CLIENT_ID ? '🟢 konfiguriert' : '🔴 nicht konfiguriert (X_CLIENT_ID fehlt)'}`,
            ``,
            `📊 X-Posts (agent_workspace):`,
            `  • In Bearbeitung (Metadaten): ${status.backlog.x_posts.stage_1_pending_metadata} Posts`,
            `  • Warteschlange Vektorisierung: ${status.backlog.x_posts.stage_2_pending_embedding} Posts`,
            `  • Vektor vorhanden (embedding IS NOT NULL): ${status.backlog.x_posts.stage_3_embedded} Posts`,
            `  •  davon Legacy-Status 'categorized': ${status.backlog.x_posts.stage_legacy_categorized ?? 0} Posts`,
            `  • Gesamtanzahl X Posts: ${status.backlog.x_posts.total}`,
            `  • Ticker-Coverage: ${status.backlog.x_posts.with_tickers ?? 0} mit Tickern | ${status.backlog.x_posts.empty_tickers ?? 0} ohne Ticker | ${status.backlog.x_posts.metadata_failed ?? 0} fehlgeschlagen (Retry) | ${status.backlog.x_posts.metadata_failed_permanent ?? 0} endgültig`,
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
            `    └ Modus: ${X_INGESTION_MODE === 'timeline' ? `Timeline (${X_DISCOVERY_INTERVAL_SEC}s, Liveness ${X_LIVENESS_CHECK ? 'an' : 'aus'})` : `Suche (${X_SEARCH_INTERVAL_SEC}s) + Timeline-Abgleich alle ${Math.round(X_RECONCILE_INTERVAL_SEC / 3600)}h`}`,
            `    └ Letzter Zyklus: ${status.pipeline.x_ingestion.cost?.last_search_saved ?? status.pipeline.x_ingestion.cost?.last_cycle_users_synced ?? 0} neue Posts, ${status.pipeline.x_ingestion.cost?.last_cycle_tweets_fetched ?? 0} Tweets geladen, ~${status.pipeline.x_ingestion.cost?.last_cycle_credits ?? 0} Credits (~$${(((status.pipeline.x_ingestion.cost?.last_cycle_credits ?? 0) / 100000)).toFixed(4)})`,
            `    └ Suchfenster ab: ${status.pipeline.x_ingestion.persisted_state?.search_since_iso || status.pipeline.x_ingestion.cost?.last_search_since || 'n/a'} | Suche-Fehler: ${status.pipeline.x_ingestion.cost?.last_search_failed ?? 0}`,
            `    └ Letzter Timeline-Abgleich: ${status.pipeline.x_ingestion.persisted_state?.last_reconcile_iso || 'noch keiner'}`,
            `    └ Nächster Abgleich fällig: ${status.pipeline.x_ingestion.persisted_state?.last_reconcile ? new Date(status.pipeline.x_ingestion.persisted_state.last_reconcile + X_RECONCILE_INTERVAL_SEC * 1000).toLocaleString('de-DE') : 'sofort (Erstlauf)'}`,
            `    └ Kumuliert seit Container-Start: ${status.pipeline.x_ingestion.cost?.total_tweets_fetched ?? 0} Tweets, ~${status.pipeline.x_ingestion.cost?.total_credits ?? 0} Credits (~$${(((status.pipeline.x_ingestion.cost?.total_credits ?? 0) / 100000)).toFixed(4)})`,
            `    └ Hochrechnung: ~$${(((status.pipeline.x_ingestion.cost?.last_cycle_credits ?? 0) / 100000) * (86400 / (X_INGESTION_MODE === 'timeline' ? X_DISCOVERY_INTERVAL_SEC : X_SEARCH_INTERVAL_SEC))).toFixed(2)}/Tag (letzter Zyklus) | ~$${((status.pipeline.x_ingestion.uptime_ms ?? 0) > 600000 ? ((status.pipeline.x_ingestion.cost?.total_credits ?? 0) / 100000) / ((status.pipeline.x_ingestion.uptime_ms ?? 1) / 86400000) : 0).toFixed(2)}/Tag (Schnitt seit Start)`,
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

        if (action === "RETRY_METADATA_FAILED") {
          const { count } = await supabase
            .from("agent_workspace")
            .update({ status: "pending_metadata" }, { count: "exact" })
            .eq("artifact_type", "x_post")
            .in("status", ["metadata_failed", "metadata_failed_permanent"]);
          return { content: [{ type: "text", text: `🔄 ${count || 0} X-Posts (status='metadata_failed') wurden auf 'pending_metadata' zurückgesetzt.` }] };
        }

        if (action === "REENRICH_X") {
          const batchLimit = Math.max(1, Math.min(Number(limit) || 100, 1000));
          let q = supabase
            .from("agent_workspace")
            .select("id, content, created_at, metadata, embedded_at")
            .eq("artifact_type", "x_post")
            // Serverseitig nur Zeilen ohne Ticker (NULL oder leeres Array).
            .or("metadata->tickers.is.null,metadata->tickers.eq.[]")
            .order("created_at", { ascending: false })
            .limit(batchLimit);
          if (start_date) q = q.gte("created_at", start_date);
          if (end_date) q = q.lte("created_at", end_date);
          if (only_with_cashtags !== false) q = q.like("content", "%$%");

          const { data, error } = await q;
          if (error) throw error;
          const candidates = data || [];

          if (candidates.length === 0) {
            return { content: [{ type: "text", text: `ℹ️ Keine Kandidaten mit leeren Tickern im Bereich ${start_date || "∞"}..${end_date || "∞"} (cashtag-only=${only_with_cashtags !== false}).` }] };
          }

          const concurrency = 4;
          let ok = 0;
          let fail = 0;
          for (let i = 0; i < candidates.length; i += concurrency) {
            const slice = candidates.slice(i, i + concurrency);
            const results = await Promise.all(slice.map((p: any) => processSinglePost(p, undefined, { reembed: false })));
            ok += results.filter(Boolean).length;
            fail += results.filter((r) => !r).length;
          }

          return { content: [{ type: "text", text: `♻️ X-Metadaten-Backfill: ${ok} erfolgreich, ${fail} fehlgeschlagen (Kandidaten: ${candidates.length}, Bereich ${start_date || "∞"}..${end_date || "∞"}, cashtag-only=${only_with_cashtags !== false}). Idempotent — erneuter Aufruf verarbeitet nur noch leere Ticker.` }] };
        }

        if (action === "REPAIR_X_METADATA") {
          const batchLimit = Math.max(1, Math.min(Number(limit) || 100, 1000));
          // Serverseitige Auswahl (JSONB-Filter), damit alte vergiftete Zeilen nicht
          // hinter neuen, bereits reparierten Zeilen verhungern.
          const { data, error } = await supabase
            .from("agent_workspace")
            .select("id, content, created_at, metadata, embedded_at")
            .eq("artifact_type", "x_post")
            .eq("metadata->>llm_categorized", "true")
            .eq("metadata->tickers", "[]")
            .or("metadata->>metadata_repaired.is.null,metadata->>metadata_repaired.neq.true")
            .order("created_at", { ascending: true })
            .limit(batchLimit);
          if (error) throw error;

          const candidates = data || [];

          if (candidates.length === 0) {
            return { content: [{ type: "text", text: `ℹ️ Keine vergifteten Kandidaten (unkategorisiert / ohne Keywords) gefunden.` }] };
          }

          const concurrency = 4;
          let ok = 0;
          let fail = 0;
          for (let i = 0; i < candidates.length; i += concurrency) {
            const slice = candidates.slice(i, i + concurrency);
            const results = await Promise.all(slice.map((p: any) => processSinglePost(p, undefined, { reembed: false })));
            ok += results.filter(Boolean).length;
            fail += results.filter((r) => !r).length;
          }

          return { content: [{ type: "text", text: `♻️ Metadaten-Reparatur: ${ok} erfolgreich, ${fail} fehlgeschlagen (Kandidaten: ${candidates.length}). Jede Zeile wird nur einmal repariert.` }] };
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
