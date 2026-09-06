import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { PCA_SERVICE_URL, log, supabase } from "./shared.ts";

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
        "- DISPLAY_WATCHLIST: Opens a watchlist window. Provide `list_name` to load from Supabase OR `ticker` with comma-separated symbols (e.g. 'AAPL,MSFT,NVDA').\n" +
        "- OPEN_WINDOW: Open or register a chart window with custom parameters.\n" +
        "- ADD_ANNOTATION: Draw support/resistance lines (`hline`), trendlines, rectangles, or buy/sell trade markers (`trade_marker`) on a specific window.\n" +
        "- REMOVE_ANNOTATION: Remove a drawing object by ID.\n" +
        "- SET_TOPBAR: Display formatted status/metric blocks in the chart topbar (e.g. Minervini Stage 2 rating, ATR, Stop-loss level, Sentiment).\n" +
        "- CLOSE_WINDOW: Close an open chart window.\n" +
        "- STATUS: Get list of open windows and viewer connection state.\n" +
        "- SCREENSHOT: Capture 640x480 screenshots of all open chart windows (or target window_id), save to /dsh_playground, and return capture ID and filepaths for reference or visual UI debugging.\n" +
        "- SAVE_SETUP: Save current window layout (positions, sizes, color flags) as a named setup. Requires `setup_name`. If no setup is loaded, 'default' is auto-saved on every geometry change.\n" +
        "- LOAD_SETUP: Load a saved window layout. Reuses existing windows of matching type, closes excess windows, creates missing ones. Matches monitor count exactly or picks closest variant. Requires `setup_name`.\n" +
        "- LIST_SETUPS: List all saved setups with their monitor-count variants.\n" +
        "- DELETE_SETUP: Delete a setup or a specific monitor-count variant. Requires `setup_name`; optional `monitor_count`.\n" +
        "- RENAME_SETUP: Rename all variants of a setup. Requires `setup_name` and `new_setup_name`.\n\n" +
        "WHEN TO USE: Use whenever you want to display charts, show technical setups, mark price targets, draw support/resistance levels, capture UI screenshots for inspection, show trade markers, or manage window layouts (save/load/delete/rename setups).",
      inputSchema: {
        action: z.enum([
          "DISPLAY_STOCK",
          "DISPLAY_WATCHLIST",
          "OPEN_WINDOW",
          "ADD_ANNOTATION",
          "REMOVE_ANNOTATION",
          "SET_TOPBAR",
          "CLOSE_WINDOW",
          "STATUS",
          "SCREENSHOT",
          "SAVE_SETUP",
          "LOAD_SETUP",
          "LIST_SETUPS",
          "DELETE_SETUP",
          "RENAME_SETUP",
        ]).describe("The action to perform"),

        ticker: z.string().optional().describe("Stock ticker symbol (or comma-separated symbols for DISPLAY_WATCHLIST)"),
        list_name: z.string().optional().describe("Supabase list name for DISPLAY_WATCHLIST (e.g. 'current_positions')"),
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
        limit: z.number().optional().default(2000).describe("Number of historical candles to load (Default 2000)"),
        resolution: z.enum(["standard", "hires", "640x480", "800x600"]).optional().default("standard").describe("Screenshot resolution: 'standard' (640x480) or 'hires' (800x600)"),
        hires: z.boolean().optional().describe("Shortcut to capture high-resolution 800x600 screenshots"),
        setup_name: z.string().optional().describe("Setup name for SAVE_SETUP, LOAD_SETUP, DELETE_SETUP, RENAME_SETUP"),
        new_setup_name: z.string().optional().describe("New setup name for RENAME_SETUP"),
        monitor_count: z.number().optional().describe("Optional specific monitor count variant for DELETE_SETUP"),
      },
    },
    async ({ action, ticker, list_name, preset, timeframe, window_id, annotation, annotation_id, topbar_block, limit, resolution, hires, setup_name, new_setup_name, monitor_count }: any) => {

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
          const cappedLimit = Math.min(Math.max(20, limit || 2000), 10000);
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

        // 8. DISPLAY_WATCHLIST
        if (action === "DISPLAY_WATCHLIST") {
          let symbols: string[] = [];
          const actualListName = list_name || window_id || "watchlist";

          if (list_name) {
            log.info(`[chart_viewer] Fetching watchlist '${list_name}' from Supabase...`);
            const { data, error } = await supabase
              .from("pca_watchlists")
              .select("ticker")
              .eq("list_name", list_name);

            if (error) throw new Error(`Supabase error: ${error.message}`);
            if (!data || data.length === 0) {
              throw new Error(`Watchlist '${list_name}' is empty or does not exist.`);
            }
            symbols = data.map((row: any) => row.ticker);
          } else if (ticker) {
            symbols = ticker.split(",").map((s: string) => s.trim().toUpperCase()).filter((s: string) => s.length > 0);
          } else {
            throw new Error("Either 'list_name' or 'ticker' (comma-separated) is required for DISPLAY_WATCHLIST.");
          }

          const rows = symbols.map((sym: string) => ({
            symbol: sym,
            cells: { Symbol: sym }
          }));

          const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              action: "OPEN_WATCHLIST",
              list_id: actualListName,
              display_name: actualListName,
              columns: ["Symbol"],
              rows: rows
            }),
          });

          const resData = await res.json();
          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                status: "success",
                message: `Watchlist '${actualListName}' sent to Chart Viewer with ${symbols.length} tickers.`,
                result: resData,
              }, null, 2),
            }],
          };
        }

        // 9. SAVE_SETUP — Save current window layout as a named setup
        if (action === "SAVE_SETUP") {
          if (!setup_name) throw new Error("Parameter 'setup_name' is required for SAVE_SETUP.");

          log.info(`[chart_viewer] SAVE_SETUP: '${setup_name}'`);
          const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "SAVE_SETUP", setup_name }),
          });

          const data = await res.json();
          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                status: "success",
                message: `Setup '${setup_name}' saved (${data.window_count || "?"} windows, ${data.monitor_count || "?"} monitors).`,
                result: data,
              }, null, 2),
            }],
          };
        }

        // 10. LOAD_SETUP — Load a saved window layout
        if (action === "LOAD_SETUP") {
          if (!setup_name) throw new Error("Parameter 'setup_name' is required for LOAD_SETUP.");

          log.info(`[chart_viewer] LOAD_SETUP: '${setup_name}'`);
          const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "LOAD_SETUP", setup_name }),
          });

          const data = await res.json();
          if (data.error) throw new Error(`LOAD_SETUP failed: ${data.error}`);

          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                status: "success",
                message: `Setup '${setup_name}' loaded: ${data.windows_opened || "?"} windows repositioned.`,
                mapping: data.mapping,
                result: data,
              }, null, 2),
            }],
          };
        }

        // 11. LIST_SETUPS — List all saved setups
        if (action === "LIST_SETUPS") {
          log.info(`[chart_viewer] LIST_SETUPS`);
          const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "LIST_SETUPS" }),
          });

          const data = await res.json();
          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                status: "success",
                count: data.count,
                setups: data.setups,
              }, null, 2),
            }],
          };
        }

        // 12. DELETE_SETUP — Delete a setup (or a specific monitor variant)
        if (action === "DELETE_SETUP") {
          if (!setup_name) throw new Error("Parameter 'setup_name' is required for DELETE_SETUP.");

          log.info(`[chart_viewer] DELETE_SETUP: '${setup_name}' (monitor_count: ${monitor_count ?? "ALL"})`);
          const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "DELETE_SETUP", setup_name, monitor_count }),
          });

          const data = await res.json();
          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                status: "success",
                message: `Setup '${setup_name}' deleted.`,
                result: data,
              }, null, 2),
            }],
          };
        }

        // 13. RENAME_SETUP — Rename all variants of a setup
        if (action === "RENAME_SETUP") {
          if (!setup_name) throw new Error("Parameter 'setup_name' is required for RENAME_SETUP.");
          if (!new_setup_name) throw new Error("Parameter 'new_setup_name' is required for RENAME_SETUP.");

          log.info(`[chart_viewer] RENAME_SETUP: '${setup_name}' → '${new_setup_name}'`);
          const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "RENAME_SETUP", setup_name, new_setup_name }),
          });

          const data = await res.json();
          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                status: "success",
                message: `Setup renamed from '${setup_name}' to '${new_setup_name}'.`,
                result: data,
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
