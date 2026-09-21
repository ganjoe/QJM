import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { STOCK_DATA_NODE_URL, log } from "./shared.ts";

function fmtTs(epoch: number | null | undefined): string {
  if (!epoch) return "-";
  return new Date(epoch * 1000).toISOString().replace("T", " ").slice(0, 19) + "Z";
}

export function registerMassiveTools(server: McpServer) {
  server.registerTool(
    "manage_massive",
    {
      title: "Manage Massive.com Integration",
      description:
        "Steuert die Massive.com-Integration (ex-Polygon.io) im stock-data-node.\n\n" +
        "WICHTIGSTE STELLSCHRAUBE - Downloadfrequenz:\n" +
        "- snapshot_every_seconds: wie oft der Full-Market-Snapshot geholt wird (1 Call = ganzer Markt). Default 60.\n" +
        "- flush_interval_seconds: wie oft die Live-Bars in die Parquet-Charts geschrieben werden. Default 300.\n" +
        "- plan: basic | starter | developer | advanced.\n\n" +
        "ACTIONS:\n" +
        "- STATUS: Zustand (enabled, dry_run, Frequenzen, letzte Laeufe, Backfill).\n" +
        "- GET_CONFIG: aktuelle Konfiguration.\n" +
        "- SET_CONFIG: Felder setzen (persistiert in config/massive.json, wirkt sofort).\n" +
        "- SET_FREQUENCY: Kurzform fuer snapshot_every_seconds und/oder flush_interval_seconds.\n" +
        "- ENABLE / DISABLE / SET_DRY_RUN: schnelle Schalter.\n" +
        "- UNIVERSE_REFRESH / DAILY_REFRESH / SNAPSHOT / FLUSH: manuell anstossen.\n" +
        "- BACKFILL: Grouped-Daily-Backfill starten (years oder from_date/to_date).\n" +
        "- QUEUE_CLEAR: Download-Queue leeren.\n\n" +
        "Beispiele:\n" +
        "- manage_massive { action: 'SET_FREQUENCY', snapshot_every_seconds: 120 }\n" +
        "- manage_massive { action: 'STATUS' }",
      inputSchema: {
        action: z.enum([
          "STATUS", "GET_CONFIG", "SET_CONFIG", "SET_FREQUENCY", "ENABLE", "DISABLE", "SET_DRY_RUN",
          "UNIVERSE_REFRESH", "DAILY_REFRESH", "SNAPSHOT", "FLUSH", "BACKFILL", "QUEUE_CLEAR",
        ]).describe("Auszufuehrende Aktion"),
        snapshot_every_seconds: z.number().int().min(5).max(86400).optional()
          .describe("Downloadfrequenz: Snapshot-Intervall in Sekunden (Default 60)"),
        flush_interval_seconds: z.number().int().min(30).max(86400).optional()
          .describe("Parquet-Flush-Intervall in Sekunden (Default 300)"),
        enabled: z.boolean().optional().describe("Massive-Service an/aus"),
        dry_run: z.boolean().optional().describe("true = nur loggen, keine echten Calls"),
        plan: z.enum(["basic", "starter", "developer", "advanced"]).optional(),
        include_otc: z.boolean().optional().describe("OTC-Ticker mitladen"),
        max_concurrency: z.number().int().min(1).max(32).optional().describe("Parallele Massive-Requests"),
        seam_log: z.enum(["summary", "all", "off"]).optional().describe("Seam-Logging-Modus"),
        years: z.number().int().min(1).max(20).optional().describe("Backfill-Zeitraum in Jahren"),
        from_date: z.string().optional().describe("Backfill Start YYYY-MM-DD"),
        to_date: z.string().optional().describe("Backfill Ende YYYY-MM-DD"),
        max_sessions: z.number().int().min(1).optional().describe("Backfill maximal N Sessions (Test)"),
      },
    },
    async (args: any) => {
      const { action } = args;
      const base = STOCK_DATA_NODE_URL;

      const call = async (path: string, options: RequestInit = {}) => {
        const res = await fetch(base + path, { ...options, signal: AbortSignal.timeout(60000) });
        const text = await res.text();
        if (!res.ok) throw new Error("HTTP " + res.status + ": " + text);
        return text ? JSON.parse(text) : {};
      };
      const postJson = (path: string, body: any) =>
        call(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

      try {
        switch (action) {
          case "STATUS": {
            const d = await call("/massive/status");
            const lines = [
              "\u{1F6F0}\u{FE0F} **Massive Status**",
              "- enabled: " + d.enabled + " | dry_run: " + d.dry_run + " | plan: " + d.plan + " | has_key: " + d.has_key,
              "- Downloadfrequenz: Snapshot alle " + d.snapshot_every_seconds + "s | Flush alle " + d.flush_interval_seconds + "s",
              "- live_tickers: " + d.live_tickers + " | flush_count: " + d.flush_count + " | limiter_paused: " + d.limiter_paused,
              "- letzte Laeufe: snapshot=" + fmtTs(d.last_snapshot) + " | daily=" + fmtTs(d.last_daily) + " | universe=" + fmtTs(d.last_universe) + " | flush=" + fmtTs(d.last_flush),
              "- backfill: " + JSON.stringify(d.backfill),
              "- status: " + d.status + (d.last_error ? " | last_error: " + d.last_error : ""),
            ];
            return { content: [{ type: "text", text: lines.join("\n") }] };
          }

          case "GET_CONFIG": {
            const d = await call("/massive/config");
            return { content: [{ type: "text", text: "Massive-Konfiguration:\n" + JSON.stringify(d, null, 2) }] };
          }

          case "SET_CONFIG":
          case "SET_FREQUENCY":
          case "ENABLE":
          case "DISABLE":
          case "SET_DRY_RUN": {
            const payload: Record<string, any> = {};
            const keys = ["snapshot_every_seconds", "flush_interval_seconds", "plan", "include_otc", "max_concurrency", "seam_log", "enabled", "dry_run"];
            for (const k of keys) {
              if (args[k] !== undefined) payload[k] = args[k];
            }
            if (action === "ENABLE") payload.enabled = true;
            if (action === "DISABLE") payload.enabled = false;
            if (action === "SET_FREQUENCY" && payload.snapshot_every_seconds === undefined && payload.flush_interval_seconds === undefined) {
              return { content: [{ type: "text", text: "SET_FREQUENCY braucht snapshot_every_seconds und/oder flush_interval_seconds." }], isError: true };
            }
            if (Object.keys(payload).length === 0) {
              return { content: [{ type: "text", text: "Keine Felder angegeben." }], isError: true };
            }
            const d = await postJson("/massive/config", payload);
            return { content: [{ type: "text", text: "Massive-Konfiguration gesetzt (persistiert):\n" + JSON.stringify(d, null, 2) }] };
          }

          case "UNIVERSE_REFRESH": {
            const d = await call("/massive/universe/refresh", { method: "POST" });
            return { content: [{ type: "text", text: "Universum-Refresh:\n" + JSON.stringify(d, null, 2) }] };
          }

          case "DAILY_REFRESH": {
            const d = await call("/massive/daily/refresh", { method: "POST" });
            return { content: [{ type: "text", text: "Daily-Refresh: " + JSON.stringify(d) }] };
          }

          case "SNAPSHOT": {
            const d = await call("/massive/snapshot", { method: "POST" });
            return { content: [{ type: "text", text: "Snapshot: " + JSON.stringify(d) }] };
          }

          case "FLUSH": {
            const d = await call("/massive/flush", { method: "POST" });
            return { content: [{ type: "text", text: "Flush: " + JSON.stringify(d) }] };
          }

          case "BACKFILL": {
            const d = await postJson("/massive/backfill", {
              years: args.years,
              from_date: args.from_date,
              to_date: args.to_date,
              max_sessions: args.max_sessions,
            });
            return { content: [{ type: "text", text: "Backfill gestartet (Hintergrund): " + JSON.stringify(d) }] };
          }

          case "QUEUE_CLEAR": {
            const d = await call("/massive/queue/clear", { method: "POST" });
            return { content: [{ type: "text", text: "Queue geleert: " + JSON.stringify(d) }] };
          }

          default:
            return { content: [{ type: "text", text: "Unbekannte Action: " + action }], isError: true };
        }
      } catch (err: any) {
        log.error("manage_massive failed: " + err.message);
        return { content: [{ type: "text", text: "Fehler: " + err.message }], isError: true };
      }
    }
  );
}
