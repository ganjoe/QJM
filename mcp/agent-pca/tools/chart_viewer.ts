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
        "- DISPLAY_STOCK: Open/update a chart window for a stock ticker (e.g. 'NVDA', 'AAPL', 'MSFT'). Automatically loads historical OHLCV data and technical indicators (SMA 50/200, Bollinger Bands) from local Parquet storage into the desktop viewer.\n" +
        "- OPEN_WINDOW: Open or register a chart window with custom parameters.\n" +
        "- ADD_ANNOTATION: Draw support/resistance lines (`hline`), trendlines, rectangles, or buy/sell trade markers (`trade_marker`) on a specific window.\n" +
        "- REMOVE_ANNOTATION: Remove a drawing object by ID.\n" +
        "- SET_TOPBAR: Display formatted status/metric blocks in the chart topbar (e.g. Minervini Stage 2 rating, ATR, Stop-loss level, Sentiment).\n" +
        "- CLOSE_WINDOW: Close an open chart window.\n" +
        "- STATUS: Get list of open windows and viewer connection state.\n\n" +
        "WHEN TO USE: Use whenever you want to display charts, show technical setups, mark price targets, draw support/resistance levels, or show trade markers on the user's screen.",
      inputSchema: {
        action: z.enum([
          "DISPLAY_STOCK",
          "OPEN_WINDOW",
          "ADD_ANNOTATION",
          "REMOVE_ANNOTATION",
          "SET_TOPBAR",
          "CLOSE_WINDOW",
          "STATUS",
        ]).describe("The action to perform"),
        ticker: z.string().optional().describe("Stock ticker symbol (e.g. 'NVDA', 'AAPL') for DISPLAY_STOCK or OPEN_WINDOW"),
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
      },
    },
    async ({ action, ticker, timeframe, window_id, annotation, annotation_id, topbar_block, limit }: any) => {
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

        // 2. DISPLAY_STOCK: Load real Parquet data from pca-service and open in Viewer
        if (action === "DISPLAY_STOCK" || (action === "OPEN_WINDOW" && ticker)) {
          if (!ticker) {
            throw new Error("Parameter 'ticker' is required for DISPLAY_STOCK.");
          }
          const sym = ticker.toUpperCase();
          const targetWinId = window_id || `win_${sym.toLowerCase()}_${tf.toLowerCase()}`;
          const cappedLimit = Math.min(Math.max(20, limit || 300), 1000);

          log.info(`[chart_viewer] Fetching ${cappedLimit} candles for ${sym} from ${PCA_SERVICE_URL}...`);
          const pcaUrl = `${PCA_SERVICE_URL}/api/chartdata?symbol=${encodeURIComponent(sym)}&timeframe=${encodeURIComponent(tf)}&limit=${cappedLimit}&features=true`;
          const pcaRes = await fetch(pcaUrl);

          if (!pcaRes.ok) {
            const errText = (await pcaRes.text()).slice(0, 200);
            throw new Error(`PCA-Service failed (HTTP ${pcaRes.status}): ${errText}`);
          }

          const payload = await pcaRes.json();
          if (payload.status !== "ok" || !payload.data || payload.data.length === 0) {
            return {
              content: [{
                type: "text",
                text: JSON.stringify({
                  status: "warning",
                  notice: payload.notice || `Keine lokalen Chart-Daten für ${sym} vorhanden.`,
                }, null, 2),
              }],
            };
          }

          const cols: string[] = payload.columns || [];
          const rows: any[] = payload.data || [];
          const idx = (name: string) => cols.indexOf(name);
          const TI = idx("timestamp"), OI = idx("open"), HI = idx("high"), LI = idx("low"), CI = idx("close"), VI = idx("volume");

          const bars = rows.map((r: any) => ({
            t_open: r[TI],
            t_close: r[TI] + 86400,
            open: Number(r[OI]),
            high: Number(r[HI]),
            low: Number(r[LI]),
            close: Number(r[CI]),
            volume: Number(r[VI] || 0),
          }));

          // Build technical overlays from precalculated features
          const overlays: any[] = [];
          const sma50Idx = idx("ma_sma_50");
          if (sma50Idx !== -1) {
            overlays.push({
              overlay_id: "sma_50",
              type: "line",
              style: { color: "#2962FF", width: 2 },
              values: rows.filter(r => r[sma50Idx] != null).map(r => ({ t: r[TI], value: Number(r[sma50Idx]) })),
            });
          }

          const sma200Idx = idx("ma_sma_200");
          if (sma200Idx !== -1) {
            overlays.push({
              overlay_id: "sma_200",
              type: "line",
              style: { color: "#FF9800", width: 2 },
              values: rows.filter(r => r[sma200Idx] != null).map(r => ({ t: r[TI], value: Number(r[sma200Idx]) })),
            });
          }

          const bbUpperIdx = idx("bb_20_upper");
          const bbLowerIdx = idx("bb_20_lower");
          if (bbUpperIdx !== -1 && bbLowerIdx !== -1) {
            overlays.push({
              overlay_id: "bollinger_bands",
              type: "band",
              style: { color: "#26A69A", alpha: 30 },
              values: rows.filter(r => r[bbUpperIdx] != null && r[bbLowerIdx] != null).map(r => ({
                t: r[TI],
                value: Number(r[bbUpperIdx]),
                value2: Number(r[bbLowerIdx]),
              })),
            });
          }

          // Send to Chart Viewer via HTTP control API
          const cmdPayload = {
            action: "OPEN_WINDOW",
            window_id: targetWinId,
            symbol: sym,
            timeframe: { unit: tf === "1D" ? "D" : "min", multiplier: 1 },
            sync_group_id: "stocks",
            bars,
            overlays,
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
          const lastCandle = bars[bars.length - 1];

          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                status: "success",
                message: `Chart for ${sym} (${tf}) displayed on Desktop Viewer.`,
                window_id: targetWinId,
                candles_loaded: bars.length,
                last_price: lastCandle.close,
                overlays_active: overlays.map(o => o.overlay_id),
                viewer_response: viewerResult,
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
