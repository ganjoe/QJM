import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { FEATURES_SERVICE_URL, sendTelemetry, log, supabase } from "./shared.ts";

function formatProgressBar(pct: number, width: number = 20): string {
  const filled = Math.min(width, Math.max(0, Math.round((pct / 100) * width)));
  const empty = width - filled;
  return `[${"█".repeat(filled)}${"░".repeat(empty)}] ${pct.toFixed(1)}%`;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

export function registerFeatureTools(server: McpServer) {
  server.registerTool(
    "manage_feature_calculation",
    {
      title: "Manage Stock Features Calculation & Scheduling",
      description:
        "Manage technical indicator / feature calculation (MAs, RS-Rating, Minervini) for all stocks in the system. " +
        "GET_STATUS: Returns the live progress (X of Y tickers completed, elapsed time, ms/ticker, ETA/remaining time, last run stats). " +
        "TRIGGER: Trigger a full calculation run immediately (optional priority ticker). " +
        "SET_SCHEDULE: Configure cyclical automatic calculations (e.g. every 60 minutes or daily at a specific UTC time). " +
        "GET_SCHEDULE: View current cyclical schedule and next run time.",
      inputSchema: {
        action: z
          .enum(["GET_STATUS", "TRIGGER", "SET_SCHEDULE", "GET_SCHEDULE"])
          .describe("The action to perform"),
        priority: z
          .string()
          .optional()
          .describe("Optional ticker to prioritize (processed first) for TRIGGER"),
        stream_telemetry: z
          .boolean()
          .optional()
          .default(false)
          .describe("For TRIGGER: if true, streams live progress logs to telemetry"),
        schedule_enabled: z
          .boolean()
          .optional()
          .describe("For SET_SCHEDULE: enable or disable the cyclical schedule"),
        schedule_mode: z
          .enum(["INTERVAL", "DAILY"])
          .optional()
          .describe("For SET_SCHEDULE: 'INTERVAL' (every N minutes) or 'DAILY' (once a day at daily_time)"),
        interval_minutes: z
          .number()
          .optional()
          .describe("For SET_SCHEDULE (mode INTERVAL): repeat interval in minutes (e.g. 60)"),
        daily_time: z
          .string()
          .optional()
          .describe("For SET_SCHEDULE (mode DAILY): time in UTC 'HH:MM' (e.g. '22:00')"),
      },
    },
    async ({
      action,
      priority,
      stream_telemetry,
      schedule_enabled,
      schedule_mode,
      interval_minutes,
      daily_time,
    }: any) => {
      try {
        // ── GET_STATUS ────────────────────────────────────────────────
        if (action === "GET_STATUS") {
          const res = await fetch(`${FEATURES_SERVICE_URL}/features/status`);
          if (!res.ok) {
            throw new Error(`Features Service HTTP ${res.status}: ${await res.text()}`);
          }
          const data = await res.json();

          const isRunning = data.is_running;
          const stage = data.stage || "IDLE";
          const total = data.total_tickers || 0;
          const completed = data.completed_tickers || 0;
          const failed = data.failed_tickers || 0;
          const pct = data.progress_pct || 0.0;
          const elapsed = data.elapsed_seconds || 0.0;
          const avgMs = data.avg_time_per_ticker_ms || 0.0;
          const eta = data.eta_seconds || 0.0;
          const lastRun = data.last_run || {};
          const sched = data.schedule || {};

          let statusIcon = "🟢";
          let stageText = "Bereit (Leerlauf)";
          if (isRunning) {
            statusIcon = "⚙️";
            if (stage === "PASS_0_PRECOMPUTE") {
              stageText = "Pass 0: Vorberechnung marktweiter Features (RS-Rohdaten laden & berechnen)...";
            } else if (stage === "PASS_0_RANKING") {
              stageText = "Pass 0: Globale Perzentil-Rangberechnung (IBD RS Ratings & Marktbreite)...";
            } else if (stage === "PASS_1_PROCESSING") {
              stageText = "Pass 1: Parallele Feature-Berechnung aller Ticker läuft...";
            } else {
              stageText = stage;
            }
          }

          const lines: string[] = [
            `📊 **Stock Data Features Calculation Status**`,
            ``,
            `**Status:** ${statusIcon} ${stageText}`,
          ];

          if (isRunning) {
            lines.push(
              `**Fortschritt:** ${formatProgressBar(pct)}`,
              `**Erledigt:** ${completed.toLocaleString("de-DE")} von ${total.toLocaleString("de-DE")} Tickern (${pct.toFixed(1)}%)`,
              data.current_ticker ? `**Aktueller Ticker:** \`${data.current_ticker}\`` : "",
              `**Laufzeit:** ${formatDuration(elapsed)}`,
              avgMs > 0 ? `**Ø Berechnungszeit:** ${avgMs.toFixed(1)} ms pro Ticker` : "",
              eta > 0 ? `**Geschätzte Restzeit (ETA):** ~${formatDuration(eta)}` : "",
              failed > 0 ? `⚠️ **Fehler:** ${failed} Ticker fehlgeschlagen` : ""
            );
          }

          if (lastRun && lastRun.finished_at) {
            lines.push(
              ``,
              `📋 **Letzter Durchlauf:**`,
              `  - **Beendet:** ${new Date(lastRun.finished_at).toLocaleString("de-DE")}`,
              `  - **Status:** ${lastRun.status === "SUCCESS" ? "✅ Erfolgreich" : "⚠️ " + lastRun.status}`,
              `  - **Dauer:** ${formatDuration(lastRun.duration_seconds || 0)}`,
              `  - **Ticker verarbeitet:** ${(lastRun.total_tickers || 0).toLocaleString("de-DE")} (${lastRun.success_count || 0} OK, ${lastRun.failed_count || 0} Fehler)`,
              lastRun.data_points ? `  - **Datenpunkte:** ${(lastRun.data_points).toLocaleString("de-DE")}` : "",
              lastRun.avg_time_per_ticker_ms ? `  - **Ø Durchsatz:** ${lastRun.avg_time_per_ticker_ms.toFixed(1)} ms / Ticker` : ""
            );
          }

          if (sched) {
            const schedActive = sched.enabled;
            lines.push(
              ``,
              `⏰ **Automatischer Zeitplan:**`,
              `  - **Status:** ${schedActive ? "🟢 Aktiviert" : "⬛ Deaktiviert"}`,
              schedActive ? `  - **Modus:** ${sched.mode === "DAILY" ? `Täglich um ${sched.daily_time_utc} UTC` : `Alle ${sched.interval_minutes} Minuten`}` : "",
              sched.next_run_at ? `  - **Nächster Lauf:** ${new Date(sched.next_run_at).toLocaleString("de-DE")}` : ""
            );
          }

          return { content: [{ type: "text", text: lines.filter(Boolean).join("\n") }] };
        }

        // ── TRIGGER ──────────────────────────────────────────────────
        if (action === "TRIGGER") {
          let url = `${FEATURES_SERVICE_URL}/features/calculate`;
          const params = new URLSearchParams();
          if (priority) params.set("priority", priority.toUpperCase());
          if (stream_telemetry) params.set("stream", "true");
          if (params.toString()) url += `?${params.toString()}`;

          const res = await fetch(url, { method: "POST" });
          if (!res.ok) {
            if (res.status === 409) {
              return { content: [{ type: "text", text: "⚠️ Feature-Berechnung läuft bereits im Hintergrund!" }], isError: true };
            }
            const err = await res.text();
            throw new Error(`Features Service HTTP ${res.status}: ${err}`);
          }

          if (!stream_telemetry) {
            const data = await res.json();
            return {
              content: [{
                type: "text",
                text: `▶️ **Feature-Berechnung erfolgreich gestartet!**\n${priority ? `⚡ Priorisierter Ticker: \`${priority.toUpperCase()}\`\n` : ""}Status: ${data.status || "Job started"}\nNutze \`manage_feature_calculation\` mit \`action: 'GET_STATUS'\` um den Live-Fortschritt zu verfolgen.`,
              }],
            };
          }

          // Streaming telemetry response
          const reader = res.body?.getReader();
          if (!reader) {
            return { content: [{ type: "text", text: "Job gestartet, aber Stream nicht verfügbar." }] };
          }

          await sendTelemetry("▶️ [Features] Starte Feature-Berechnung...");
          const decoder = new TextDecoder();
          let buffer = "";

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";
            for (const line of lines) {
              if (line.includes("Feature processing:") || line.includes("Feature calculation finished:") || line.includes("Pass")) {
                await sendTelemetry(`⚙️ [Features] ${line.trim()}`);
              }
            }
          }

          await sendTelemetry("✅ [Features] Feature-Berechnung abgeschlossen.");
          return { content: [{ type: "text", text: "✅ Feature-Berechnung erfolgreich beendet." }] };
        }

        // ── SET_SCHEDULE ─────────────────────────────────────────────
        if (action === "SET_SCHEDULE") {
          // Fetch existing config first
          const getRes = await fetch(`${FEATURES_SERVICE_URL}/features/schedule`);
          const currentCfg = getRes.ok ? await getRes.json() : {};

          const updatePayload: Record<string, any> = { ...currentCfg };
          if (schedule_enabled !== undefined) updatePayload.enabled = schedule_enabled;
          if (schedule_mode) updatePayload.mode = schedule_mode;
          if (interval_minutes !== undefined) updatePayload.interval_minutes = interval_minutes;
          if (daily_time) updatePayload.daily_time_utc = daily_time;

          const res = await fetch(`${FEATURES_SERVICE_URL}/features/schedule`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(updatePayload),
          });

          if (!res.ok) {
            throw new Error(`Features Service HTTP ${res.status}: ${await res.text()}`);
          }

          const result = await res.json();
          const saved = result.schedule || updatePayload;

          log.info(`[PCA] Updated features calculation schedule: ${JSON.stringify(saved)}`);
          return {
            content: [{
              type: "text",
              text: [
                `⚙️ **Zyklischer Zeitplan für Feature-Berechnung aktualisiert**`,
                ``,
                `**Status:** ${saved.enabled ? "🟢 Aktiviert" : "⬛ Deaktiviert"}`,
                `**Modus:** ${saved.mode === "DAILY" ? `Täglich um ${saved.daily_time_utc} UTC` : `Alle ${saved.interval_minutes} Minuten`}`,
                saved.next_run_at ? `**Nächster Ausführungszeitpunkt:** ${new Date(saved.next_run_at).toLocaleString("de-DE")}` : "",
              ].filter(Boolean).join("\n"),
            }],
          };
        }

        // ── GET_SCHEDULE ─────────────────────────────────────────────
        if (action === "GET_SCHEDULE") {
          const res = await fetch(`${FEATURES_SERVICE_URL}/features/schedule`);
          if (!res.ok) {
            throw new Error(`Features Service HTTP ${res.status}: ${await res.text()}`);
          }
          const saved = await res.json();
          return {
            content: [{
              type: "text",
              text: [
                `⏰ **Aktueller Zeitplan für Feature-Berechnung**`,
                ``,
                `**Status:** ${saved.enabled ? "🟢 Aktiviert" : "⬛ Deaktiviert"}`,
                `**Modus:** ${saved.mode === "DAILY" ? `Täglich um ${saved.daily_time_utc} UTC` : `Alle ${saved.interval_minutes} Minuten`}`,
                saved.last_run_at ? `**Letzter Lauf:** ${new Date(saved.last_run_at).toLocaleString("de-DE")}` : "**Letzter Lauf:** Noch nie",
                saved.next_run_at ? `**Nächster Lauf:** ${new Date(saved.next_run_at).toLocaleString("de-DE")}` : "",
              ].filter(Boolean).join("\n"),
            }],
          };
        }

        return { content: [{ type: "text", text: `Unbekannte Aktion: ${action}` }], isError: true };
      } catch (err: any) {
        log.error(`[PCA] Error in manage_feature_calculation: ${err.message}`);
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );
}
