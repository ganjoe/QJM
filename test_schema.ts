import { z } from "npm:zod";
import zodToJsonSchema from "npm:zod-to-json-schema";

const schema = z.object({
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
  limit: z.number().optional().default(300).describe("Number of historical candles to load (Default 300)"),
  resolution: z.enum(["standard", "hires", "640x480", "800x600"]).optional().default("standard").describe("Screenshot resolution: 'standard' (640x480) or 'hires' (800x600)"),
  hires: z.boolean().optional().describe("Shortcut to capture high-resolution 800x600 screenshots"),
});

console.log(JSON.stringify(zodToJsonSchema(schema, "mySchema"), null, 2));
