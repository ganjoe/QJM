import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { PCA_SERVICE_URL, log } from "./shared.ts";

export const CHART_VIEWER_API_URL = Deno.env.get("CHART_VIEWER_API_URL") || "http://host.docker.internal:8766";

export function registerChartViewerTools(server: McpServer) {

  // ── manage_chart_viewer ──────────────────────────────────────────
  server.registerTool(
    "manage_chart_viewer",
    {
      title: "Control Desktop Chart Viewer",
      description: "Controls the TC2000-style native desktop chart viewer running on the user's screen.\n\n" +
        "ACTIONS:\n" +
        "- DISPLAY_STOCK: Open/update a chart window for a stock ticker (e.g. 'NVDA', 'AAPL', 'MSFT'). Automatically loads historical OHLCV data, precalculated or on-the-fly indicators, and topbar metrics from Supabase registry using presets ('default', 'trend_template', 'momentum', 'clean').\n" +
        "- OPEN_WINDOW: Open or register a chart window with custom parameters.\n" +
        "- ADD_ANNOTATION: Draw support/resistance lines (`hline`), trendlines, rectangles, or buy/sell trade markers (`trade_marker`) on a specific window.\n" +
        "- REMOVE_ANNOTATION: Remove a drawing object by ID.\n" +
        "- SET_TOPBAR: Display formatted status/metric blocks in the chart topbar (e.g. Minervini Stage 2 rating, ATR, Stop-loss level, Sentiment).\n" +
        "- CLOSE_WINDOW: Close an open chart window.\n" +
        "- STATUS: Get list of open windows and viewer connection state.\n" +
        "- SCREENSHOT: Capture 640x480 screenshots of all open chart windows (or target window_id), save to /dsh_playground, and return capture ID and filepaths for reference or visual UI debugging.\n\n" +
        "WHEN TO USE: Use whenever you want to display charts, show technical setups, mark price targets, draw support/resistance levels, capture UI screenshots for inspection, or show trade markers on the user's screen.",
      inputSchema: {
        action: z.enum([
          "DISPLAY_STOCK",
          "OPEN_WINDOW",
          "ADD_ANNOTATION",
          "REMOVE_ANNOTATION",
          "SET_TOPBAR",
          "CLOSE_WINDOW",
          "STATUS",
          "SCREENSHOT",
        ]).describe("The action to perform"),

        ticker: z.string().optional().describe("Stock ticker symbol (e.g. 'NVDA', 'AAPL') for DISPLAY_STOCK or OPEN_WINDOW"),
        preset: z.string().optional().default("default").describe("Indicator preset name: e.g. 'default', 'trend_template', 'momentum', 'clean', or custom user preset like 'qmaggi' created via manage_chart_presets"),
        timeframe: z.string().optional().default("1D").describe("Candle timeframe (e.g. '1D', '5min')"),
        window_id: z.string().optional().describe("Target chart window ID (defaults to 'win_{ticker}_1d')"),
        annotation: z.object({
          id: z.string().optional().describe("Unique annotation ID (e.g. 'support_1', 'stop_loss')"),
          type: z.enum(["hline", "trendline", "rect", "text", "trade_marker"]).describe("Annotation type"),
          price: z.number().optional().describe("Price level for hline or trade_marker"),
          color: z.string().optional().describe("Hex color code (e.g. '#00E676' green, '#FF5252' red, '#FF9800' orange)"),
          label: z.string().optional().describe("Text label or annotation note"),
          action: z.enum(["BUY", "SELL"]).optional().describe("Trade marker action ('BUY' or 'SELL')"),
          anchors: z.array(z.object({
            price: z.number().optional(),
            t: z.number().optional(),
            x_px: z.number().optional(),
            y_px: z.number().optional(),
            mode: z.enum(["data", "pixel"]).optional(),
          })).optional().describe("Custom anchor points"),
        }).optional().describe("Annotation definition for ADD_ANNOTATION"),
        annotation_id: z.string().optional().describe("Annotation ID for REMOVE_ANNOTATION"),
        topbar_block: z.object({
          block_id: z.string().optional().describe("Unique block ID"),
          row: z.number().optional().default(0),
          col: z.number().optional().default(0),
          content: z.string().describe("Status content or metric text"),
          ttl_ms: z.number().optional().describe("Optional time-to-live in milliseconds"),
        }).optional().describe("Topbar block definition for SET_TOPBAR"),
        limit: z.number().optional().default(300).describe("Number of historical candles to load (Default 300)"),
        resolution: z.enum(["standard", "hires", "640x480", "800x600"]).optional().default("standard").describe("Screenshot resolution: 'standard' (640x480) or 'hires' (800x600)"),
        hires: z.boolean().optional().describe("Shortcut to capture high-resolution 800x600 screenshots"),
      },
    },
    async ({ action, ticker, preset, timeframe, window_id, annotation, annotation_id, topbar_block, limit, resolution, hires }: any) => {

      try {
        const tf = timeframe || "1D";

        // 1. STATUS
        if (action === "STATUS") {
          const res = await fetch(`${CHART_VIEWER_API_URL}/api/status`);
          if (!res.ok) {
            throw new Error(`Chart Viewer Server not reachable: HTTP ${res.status}`);
          }
          const statusData = await res.json();
          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                status: "success",
                chart_viewer: statusData,
              }, null, 2),
            }],
          };
        }

        // 2. DISPLAY_STOCK: Delegate full orchestration (bars, preset indicators, topbar) to Chart Viewer Server
        if (action === "DISPLAY_STOCK" || (action === "OPEN_WINDOW" && ticker)) {
          if (!ticker) {
            throw new Error("Parameter 'ticker' is required for DISPLAY_STOCK.");
          }
          const sym = ticker.toUpperCase();
          const targetWinId = window_id || `win_${sym.toLowerCase()}_${tf.toLowerCase()}`;
          const cappedLimit = Math.min(Math.max(20, limit || 1500), 2000);
          const selectedPreset = preset || "default";

          log.info(`[chart_viewer] DISPLAY_STOCK: ${sym} with preset '${selectedPreset}'...`);
          const cmdPayload = {
            action: "DISPLAY_STOCK",
            symbol: sym,
            preset: selectedPreset,
            timeframe_str: tf,
            limit: cappedLimit,
            window_id: targetWinId,
          };

          const viewerRes = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(cmdPayload),
          });

          if (!viewerRes.ok) {
            const errText = await viewerRes.text();
            throw new Error(`Chart Viewer Server rejected command: ${errText}`);
          }

          const viewerResult = await viewerRes.json();
          if (viewerResult.error) {
            throw new Error(`DISPLAY_STOCK failed on Chart Server: ${viewerResult.error}`);
          }

          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                status: "success",
                message: `Chart for ${sym} (${tf}) with preset '${selectedPreset}' displayed on Desktop Viewer.`,
                window_id: viewerResult.window_id || targetWinId,
                bars: viewerResult.bars,
                overlays: viewerResult.overlays,
                preset: selectedPreset,
              }, null, 2),
            }],
          };
        }

        // 3. ADD_ANNOTATION
        if (action === "ADD_ANNOTATION") {
          if (!window_id) throw new Error("Parameter 'window_id' is required for ADD_ANNOTATION.");
          if (!annotation) throw new Error("Parameter 'annotation' is required for ADD_ANNOTATION.");

          const annId = annotation.id || `ann_${Date.now()}`;
          const annType = annotation.type;
          let anchors = annotation.anchors || [];

          if (anchors.length === 0 && annotation.price !== undefined) {
            anchors = [{ price: annotation.price, mode: "data" }];
          }

          const style: any = {
            color: annotation.color || "#00E676",
            width: 2,
          };
          if (annotation.label) style.text = annotation.label;
          if (annotation.action) style.action = annotation.action;

          const cmdPayload = {
            action: "ADD_ANNOTATION",
            window_id,
            annotation: {
              id: annId,
              type: annType,
              anchors,
              style,
              persistent: true,
            },
          };

          const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(cmdPayload),
          });

          const data = await res.json();
          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                status: "success",
                message: `Annotation '${annId}' (${annType}) added to window '${window_id}'.`,
                result: data,
              }, null, 2),
            }],
          };
        }

        // 4. REMOVE_ANNOTATION
        if (action === "REMOVE_ANNOTATION") {
          if (!window_id || !annotation_id) {
            throw new Error("Parameters 'window_id' and 'annotation_id' are required.");
          }
          const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              action: "REMOVE_ANNOTATION",
              window_id,
              annotation_id,
            }),
          });
          const data = await res.json();
          return {
            content: [{
              type: "text",
              text: JSON.stringify({ status: "success", result: data }, null, 2),
            }],
          };
        }

        // 5. SET_TOPBAR
        if (action === "SET_TOPBAR") {
          if (!window_id || !topbar_block) {
            throw new Error("Parameters 'window_id' and 'topbar_block' are required.");
          }
          const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              action: "SET_TOPBAR",
              window_id,
              block_id: topbar_block.block_id || "status",
              position: { row: topbar_block.row ?? 0, col: topbar_block.col ?? 0 },
              content: topbar_block.content,
              ttl_ms: topbar_block.ttl_ms,
            }),
          });
          const data = await res.json();
          return {
            content: [{
              type: "text",
              text: JSON.stringify({ status: "success", result: data }, null, 2),
            }],
          };
        }

        // 6. CLOSE_WINDOW
        if (action === "CLOSE_WINDOW") {
          if (!window_id) throw new Error("Parameter 'window_id' is required.");
          const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "CLOSE_WINDOW", window_id }),
          });
          const data = await res.json();
          return {
            content: [{
              type: "text",
              text: JSON.stringify({ status: "success", result: data }, null, 2),
            }],
          };
        }

        // 7. SCREENSHOT
        if (action === "SCREENSHOT") {
          log.info(`[chart_viewer] SCREENSHOT requested (window_id: ${window_id || "ALL"})...`);
          const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              action: "SCREENSHOT",
              window_id: window_id || undefined,
              resolution,
              hires,
            }),
          });


          if (!res.ok) {
            const errText = await res.text();
            throw new Error(`Chart Viewer Server rejected screenshot: ${errText}`);
          }

          const data = await res.json();
          if (data.error) {
            throw new Error(`Screenshot failed: ${data.error}`);
          }

          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                status: "success",
                capture_id: data.capture_id,
                count: data.count,
                output_dir: data.output_dir,
                files: data.files,
              }, null, 2),
            }],
          };
        }

        throw new Error(`Unhandled action: ${action}`);


      } catch (err: any) {
        log.error(`[manage_chart_viewer] Error: ${err.message}`);
        return {
          content: [{
            type: "text",
            text: JSON.stringify({
              status: "error",
              error: err.message,
            }, null, 2),
          }],
          isError: true,
        };
      }
    }
  );
}
