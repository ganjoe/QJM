import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { PCA_SERVICE_URL, log, supabase, POSTGREST_UNIVERSE_LIMIT } from "./shared.ts";

export const CHART_VIEWER_API_URL = Deno.env.get("CHART_VIEWER_API_URL") || "http://host.docker.internal:8766";

// ─────────────────────────────────────────────────────────────────────────────
// DISPLAY_SERIES helpers — long/short pair spreads, computed in RAM only.
// No Parquet writes, no persistence: bars are built here and pushed straight
// into the viewer via the OPEN_WINDOW command (viewer accepts raw bars).
// ─────────────────────────────────────────────────────────────────────────────

const SPREAD_MAX_SYMBOLS = 60;    // hard cap: 60 symbols => 1770 pairs
const SPREAD_WINDOW_CAP = 24;     // max chart windows pushed per call
const SPREAD_FETCH_CONCURRENCY = 8;

interface LegBar { t: number; o: number; h: number; l: number; c: number; v: number; }

function round4(x: number): number {
  return Math.round(x * 10000) / 10000;
}

/** Parse a timeframe string like '1D', '5min', '1h' into unit/multiplier/seconds. */
function parseTimeframeSpec(tf: string): { unit: string; multiplier: number; seconds: number } {
  const raw = (tf || "1D").trim();
  const m = /^(\d+)?\s*([a-zA-Z]+)$/.exec(raw);
  const multiplier = m && m[1] ? parseInt(m[1], 10) : 1;
  const token = (m && m[2] ? m[2] : "D").toLowerCase();
  if (["s", "sec", "secs"].includes(token)) return { unit: "s", multiplier, seconds: multiplier };
  if (["min", "mins", "m"].includes(token)) return { unit: "min", multiplier, seconds: multiplier * 60 };
  if (["h", "hour", "hours"].includes(token)) return { unit: "h", multiplier, seconds: multiplier * 3600 };
  if (["w", "week", "weeks"].includes(token)) return { unit: "W", multiplier, seconds: multiplier * 604800 };
  if (["mo", "month", "months"].includes(token)) return { unit: "M", multiplier, seconds: multiplier * 2592000 };
  return { unit: "D", multiplier, seconds: multiplier * 86400 };
}

function sanitizeWindowId(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

/** Fetch OHLCV bars for one leg directly from the PCA service (RAM only). */
async function fetchLegBars(symbol: string, tf: string, limit: number): Promise<LegBar[]> {
  const url = `${PCA_SERVICE_URL}/api/chartdata?symbol=${encodeURIComponent(symbol)}` +
    `&timeframe=${encodeURIComponent(tf)}&limit=${limit}&features=false`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`PCA HTTP ${res.status}`);
  const json = await res.json();
  if (!json || json.status !== "ok" || !Array.isArray(json.data) || json.data.length === 0) {
    throw new Error(`no chart data (${json?.notice || "empty"})`);
  }
  const cols: string[] = json.columns || [];
  const idx: Record<string, number> = {};
  cols.forEach((c: string, i: number) => { idx[c] = i; });

  const bars: LegBar[] = [];
  for (const row of json.data) {
    const t = Number(row[idx["timestamp"]]);
    const c = Number(row[idx["close"]]);
    if (!Number.isFinite(t) || !Number.isFinite(c)) continue;
    bars.push({
      t,
      o: Number(row[idx["open"]]),
      h: Number(row[idx["high"]]),
      l: Number(row[idx["low"]]),
      c,
      v: Number(row[idx["volume"]] || 0),
    });
  }
  bars.sort((x, y) => x.t - y.t);
  return bars;
}

/**
 * Inner-join two legs on the bar grid (no look-ahead).
 * Exact timestamps are tried first; when a leg stores its bars on a shifted
 * anchor (e.g. 00:00 UTC vs. 04:00 UTC for daily data), the join falls back to
 * UTC-day buckets so those symbols still align instead of dropping out.
 */
function alignPair(a: LegBar[], b: LegBar[], tfSeconds: number): Array<{ a: LegBar; b: LegBar }> {
  const build = (keyFn: (t: number) => number) => {
    const bMap = new Map<number, LegBar>();
    for (const bar of b) bMap.set(keyFn(bar.t), bar);
    const out: Array<{ a: LegBar; b: LegBar }> = [];
    for (const barA of a) {
      const barB = bMap.get(keyFn(barA.t));
      if (barB) out.push({ a: barA, b: barB });
    }
    return out;
  };

  const exact = build((t) => t);
  const minLen = Math.min(a.length, b.length);
  if (tfSeconds < 86400 || exact.length >= minLen * 0.9) return exact;

  const dayBuckets = build((t) => Math.floor(t / 86400));
  return dayBuckets.length > exact.length ? dayBuckets : exact;
}

/**
 * Build the spread as a synthetic OHLC bar series (first aligned bar = 0).
 * high/low use the real leg extremes: long-leg high vs short-leg low is the
 * favourable intraday extreme of the spread, long-low vs short-high the adverse
 * one — so high >= max(o,c) and low <= min(o,c) always hold (valid viewer bars).
 */
function spreadFromAligned(
  aligned: Array<{ a: LegBar; b: LegBar }>,
  mode: string,
  barSeconds: number,
): { bars: any[]; closes: number[]; timestamps: number[] } | null {
  if (aligned.length < 2) return null;
  const a0 = aligned[0].a.c;
  const b0 = aligned[0].b.c;
  if (!Number.isFinite(a0) || a0 === 0 || !Number.isFinite(b0) || b0 === 0) return null;
  const ratio0 = a0 / b0;

  const val = (longV: number, shortV: number): number => {
    if (!Number.isFinite(longV) || !Number.isFinite(shortV)) return NaN;
    if (mode === "abs") return longV - shortV;
    if (mode === "ratio") return shortV === 0 ? NaN : ((longV / shortV) / ratio0 - 1) * 100;
    return (longV / a0 - shortV / b0) * 100;
  };

  const bars: any[] = [];
  const closes: number[] = [];
  const timestamps: number[] = [];
  for (const pair of aligned) {
    const o = val(pair.a.o, pair.b.o);
    const c = val(pair.a.c, pair.b.c);
    const favourable = val(pair.a.h, pair.b.l);
    const adverse = val(pair.a.l, pair.b.h);
    if (![o, c, favourable, adverse].every((v) => Number.isFinite(v))) continue;
    bars.push({
      t_open: pair.a.t,
      t_close: pair.a.t + barSeconds,
      open: round4(o),
      high: round4(Math.max(o, c, favourable, adverse)),
      low: round4(Math.min(o, c, favourable, adverse)),
      close: round4(c),
      volume: Math.min(pair.a.v, pair.b.v),
    });
    closes.push(c);
    timestamps.push(pair.a.t);
  }
  if (bars.length < 2) return null;
  return { bars, closes, timestamps };
}

/** Total performance, max drawdown and a crude quality ratio of a spread curve. */
function spreadMetrics(closes: number[]): { total: number; maxDD: number; quality: number } {
  const total = closes[closes.length - 1];
  let peak = closes[0];
  let maxDD = 0;
  for (const v of closes) {
    if (v > peak) peak = v;
    const dd = peak - v;
    if (dd > maxDD) maxDD = dd;
  }
  const quality = maxDD > 0 ? total / maxDD : (total > 0 ? 99 : 0);
  return { total, maxDD, quality };
}

/** Local indicator math on the synthetic spread series (RAM only, no round-trips). */
function smaSeries(values: number[], period: number): Array<number | null> {
  const out: Array<number | null> = new Array(values.length).fill(null);
  if (period <= 0) return out;
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function emaSeries(values: number[], period: number): Array<number | null> {
  const out: Array<number | null> = new Array(values.length).fill(null);
  if (period <= 0 || values.length === 0) return out;
  const k = 2 / (period + 1);
  let prev = values[0];
  out[0] = prev;
  for (let i = 1; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

function bollingerSeries(values: number[], period: number, stdDev: number) {
  const avg = smaSeries(values, period);
  const upper: Array<number | null> = new Array(values.length).fill(null);
  const lower: Array<number | null> = new Array(values.length).fill(null);
  for (let i = 0; i < values.length; i++) {
    const mean = avg[i];
    if (mean === null) continue;
    let acc = 0;
    for (let j = i - period + 1; j <= i; j++) acc += (values[j] - mean) * (values[j] - mean);
    const sd = Math.sqrt(acc / period);
    upper[i] = mean + sd * stdDev;
    lower[i] = mean - sd * stdDev;
  }
  return { avg, upper, lower };
}

/** Daily spread range in percentage points — ADR proxy (ADR% is undefined for signed spreads). */
function rangeSeries(bars: any[]): number[] {
  return bars.map((b) => Number(b.high) - Number(b.low));
}

function toPoints(values: Array<number | null>, timestamps: number[]): Array<{ t: number; value: number }> {
  const pts: Array<{ t: number; value: number }> = [];
  for (let i = 0; i < values.length && i < timestamps.length; i++) {
    const v = values[i];
    if (v === null || v === undefined || !Number.isFinite(v)) continue;
    pts.push({ t: timestamps[i], value: round4(v as number) });
  }
  return pts;
}

/** Simple SMA/Bollinger overlays on the spread (fallback when no preset is given). */
function buildSpreadOverlays(kind: string, periods: number[], bars: any[], timestamps: number[]): any[] {
  if (!kind || kind === "none") return [];
  const closes = bars.map((b) => Number(b.close));
  const overlays: any[] = [];
  const colors = ["#2962FF", "#FF6D00", "#00BFA5", "#D500F9"];

  if (kind === "bb") {
    for (const p of periods) {
      const { upper, lower } = bollingerSeries(closes, p, 2);
      const bandPts: any[] = [];
      for (let i = 0; i < closes.length && i < timestamps.length; i++) {
        if (upper[i] === null || lower[i] === null) continue;
        bandPts.push({ t: timestamps[i], value: round4(upper[i] as number), value2: round4(lower[i] as number) });
      }
      if (bandPts.length > 0) {
        overlays.push({
          overlay_id: `bb_${p}`,
          type: "band",
          style: { color: "#26A69A", alpha: 25 },
          values: bandPts,
          pane: "main",
        });
      }
    }
    return overlays;
  }

  let idx = 0;
  for (const p of periods) {
    const pts = toPoints(smaSeries(closes, p), timestamps);
    if (pts.length === 0) continue;
    overlays.push({
      overlay_id: `spread_sma_${p}`,
      type: "line",
      style: { color: colors[idx % colors.length], width: 2 },
      values: pts,
      pane: "main",
      origin: "bottom",
    });
    idx++;
  }
  return overlays;
}

/** Fetch a preset definition from PCA (same source the viewer orchestrator uses). */
async function fetchPresetData(name: string): Promise<any> {
  const res = await fetch(`${PCA_SERVICE_URL}/api/presets/${encodeURIComponent(name)}`);
  if (!res.ok) throw new Error(`Preset '${name}' nicht gefunden (HTTP ${res.status})`);
  return await res.json();
}

/**
 * Translate viewer-preset members into overlays on the spread series.
 * SMA/EMA/Bollinger run on the spread closes, ADR members on the synthetic
 * spread range. Parquet-only features (RS ratings, breadth …) have no spread
 * equivalent and are reported back instead of being silently dropped.
 */
function buildPresetOverlays(presetData: any, bars: any[], timestamps: number[]) {
  const closes = bars.map((b) => Number(b.close));
  const ranges = rangeSeries(bars);
  const overlays: any[] = [];
  const unavailable: string[] = [];
  const adrInfo: { adr1?: number; adr20?: number } = {};

  for (const ind of presetData?.indicators || []) {
    const col = String(ind.column || ind.canonical_id || "").toLowerCase().trim();
    const style = { ...(ind.style || {}) };
    const pane = String(ind.pane || "main").trim().toLowerCase() || "main";
    const styleType = String(style.type || "line");

    const maMatch = /^(?:ma_)?(sma|ema)_(\d+)$/.exec(col);
    if (maMatch) {
      const period = parseInt(maMatch[2], 10);
      const series = maMatch[1] === "ema" ? emaSeries(closes, period) : smaSeries(closes, period);
      const pts = toPoints(series, timestamps);
      if (pts.length === 0) {
        unavailable.push(`${col} (Periode ${period} > ${bars.length} Bars)`);
        continue;
      }
      overlays.push({ overlay_id: col, type: "line", style, values: pts, pane, origin: "bottom" });
      continue;
    }

    const bbMatch = /^bb_(\d+)(?:_(upper|avg|lower))?$/.exec(col);
    if (bbMatch) {
      const period = parseInt(bbMatch[1], 10);
      const part = bbMatch[2] || "avg";
      const bands = bollingerSeries(closes, period, Number(presetData?.std_dev) || 2);
      if (part === "upper") {
        const bandPts: any[] = [];
        for (let i = 0; i < closes.length && i < timestamps.length; i++) {
          if (bands.upper[i] === null || bands.lower[i] === null) continue;
          bandPts.push({ t: timestamps[i], value: round4(bands.upper[i] as number), value2: round4(bands.lower[i] as number) });
        }
        if (bandPts.length > 0) {
          overlays.push({
            overlay_id: `bb_${period}`,
            type: "band",
            style: { color: style.color || "#26A69A", alpha: style.alpha ?? 25 },
            values: bandPts,
            pane,
          });
          continue;
        }
      }
      const pts = toPoints(part === "lower" ? bands.lower : bands.avg, timestamps);
      if (pts.length > 0) {
        overlays.push({
          overlay_id: col,
          type: styleType === "histogram" ? "line" : styleType,
          style,
          values: pts,
          pane,
          origin: "bottom",
        });
        continue;
      }
      unavailable.push(col);
      continue;
    }

    const adrPctMatch = /^adr_(\d+)_pct$/.exec(col);
    if (adrPctMatch) {
      const period = parseInt(adrPctMatch[1], 10);
      const series = period <= 1 ? ranges : smaSeries(ranges, period);
      const pts = toPoints(series, timestamps);
      if (pts.length === 0) {
        unavailable.push(col);
        continue;
      }
      overlays.push({
        overlay_id: col,
        type: styleType === "histogram" ? "histogram" : "line",
        style,
        values: pts,
        pane,
        origin: "bottom",
      });
      if (period <= 1) adrInfo.adr1 = pts[pts.length - 1].value;
      continue;
    }

    const adrSmaMatch = /^adr_(\d+)_sma$/.exec(col);
    if (adrSmaMatch) {
      const period = parseInt(adrSmaMatch[1], 10);
      const pts = toPoints(smaSeries(ranges, period), timestamps);
      if (pts.length === 0) {
        unavailable.push(col);
        continue;
      }
      overlays.push({ overlay_id: col, type: "line", style, values: pts, pane, origin: "bottom" });
      adrInfo.adr20 = pts[pts.length - 1].value;
      continue;
    }

    unavailable.push(col);
  }

  return { overlays, unavailable, adrInfo };
}

function spreadSummary(entry: any) {
  return {
    pair: `${entry.long} / ${entry.short}`,
    long: entry.long,
    short: entry.short,
    spread_pct: Number(entry.total.toFixed(3)),
    max_drawdown_pct: Number(entry.maxDD.toFixed(3)),
    quality: Number(entry.quality.toFixed(3)),
    long_return_pct: Number(entry.retLong.toFixed(2)),
    short_return_pct: Number(entry.retShort.toFixed(2)),
    bars: entry.barsCount,
  };
}


export function registerChartViewerTools(server: McpServer) {

  // ── manage_chart_viewer ──────────────────────────────────────────
  server.registerTool(
    "manage_chart_viewer",
    {
      title: "Control Desktop Chart Viewer",
      description: "Controls the TC2000-style native desktop chart viewer running on the user's screen.\n\n" +
        "ACTIONS:\n" +
        "- DISPLAY_STOCK: Open/update a chart window for a stock ticker (e.g. 'NVDA', 'AAPL', 'MSFT'). Automatically loads historical OHLCV data, precalculated or on-the-fly indicators, and topbar metrics from Supabase registry using presets ('default', 'trend_template', 'momentum', 'clean').\n" +
        "- DISPLAY_WATCHLIST: Opens a watchlist window. Provide `list_name` to load from Supabase OR `ticker` with comma-separated symbols (e.g. 'AAPL,MSFT,NVDA'). The tickers are read fresh from Supabase on EVERY call, so calling it again with the same `list_name` simply refreshes the existing window (window_id = list_id).\n" +
        "- `list_name: 'scan_latest'` öffnet die Auto-Watchlist des Universal Scanners: sie enthält nach jedem `run_universal_scanner`-Lauf das vollständige Treffer-Ergebnis (Replace). Für den aktuellsten Stand zuerst scannen, dann DISPLAY_WATCHLIST aufrufen. Eine leere Liste (Scan ohne Treffer) kann nicht angezeigt werden und führt zu einem Fehler - vorher `manage_watchlist` (LOAD) prüfen.\n" +
        "- DISPLAY_SERIES: Berechnet Long-Short-Spread-Zeitreihen (Paare) on the fly im MCP-Prozess — ausschließlich im RAM, kein Parquet, keine Persistenz — und pusht sie als eigenständige Chart-Fenster in den Viewer. Einzelpaar: `ticker` + `ticker_b`. Universum: `list_name` (Supabase-Watchlist) oder `ticker` als Komma-Liste; es werden alle C(n,2)-Paare berechnet, nach `min_spread_pct` gefiltert und die besten `top_n` als getilte Fenster geöffnet. `dry_run: true` liefert nur das Ranking, ohne Fenster zu öffnen. Spread-Modi: 'pct' (normierte Differenz in Prozentpunkten), 'ratio' (normiertes Kursverhältnis), 'abs' (rohe Differenz).\n" +
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
          "DISPLAY_SERIES",
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

        // ── DISPLAY_SERIES (Long-Short-Paar-Spreads, RAM-only) ──
        ticker_b: z.string().optional().describe("DISPLAY_SERIES: zweites Leg für den Einzelpaar-Modus (Short-Seite)"),
        pairs: z.array(z.string()).optional().describe("DISPLAY_SERIES: explizite Paarliste im Format 'LONG/SHORT' (z. B. ['XOP/SOXX','IBB/SOXX']). Überschreibt Ranking/Filter und behält die Reihenfolge — damit lassen sich bestehende Spread-Fenster gezielt refreshen"),
        mode: z.enum(["pct", "ratio", "abs"]).optional().default("pct").describe("DISPLAY_SERIES: Spread-Definition — pct = 100*(A/A0 - B/B0), ratio = normiertes Kursverhältnis, abs = rohe Differenz"),
        limit_bars: z.number().optional().default(66).describe("DISPLAY_SERIES: Anzahl Bars im Spread-Fenster (Default 66 ≈ 3 Monate Tagesbasis)"),
        min_spread_pct: z.number().optional().default(30).describe("DISPLAY_SERIES: nur Paare mit |Spread-Performance| >= X Prozentpunkte"),
        top_n: z.number().optional().default(12).describe("DISPLAY_SERIES: Anzahl der geöffneten Chart-Fenster (max 24 pro Aufruf)"),
        rank_by: z.enum(["total", "quality"]).optional().default("total").describe("DISPLAY_SERIES: Ranking — total = Spread-Performance, quality = total/maxDD"),
        overlay: z.enum(["none", "sma", "bb"]).optional().default("sma").describe("DISPLAY_SERIES: Overlay auf dem Spread-Chart (SMA oder Bollinger)"),
        overlay_periods: z.array(z.number()).optional().describe("DISPLAY_SERIES: Perioden für das Spread-Overlay (Default [20])"),
        grid_cols: z.number().optional().default(3).describe("DISPLAY_SERIES: Anzahl Spalten für das Fenster-Tiling"),
        cell_w: z.number().optional().default(700).describe("DISPLAY_SERIES: Fensterbreite in px"),
        cell_h: z.number().optional().default(460).describe("DISPLAY_SERIES: Fensterhöhe in px"),
        origin_x: z.number().optional().default(40).describe("DISPLAY_SERIES: Start-X des Tiling-Rasters"),
        origin_y: z.number().optional().default(40).describe("DISPLAY_SERIES: Start-Y des Tiling-Rasters"),
        dry_run: z.boolean().optional().default(false).describe("DISPLAY_SERIES: nur berechnen und ranken, keine Fenster öffnen"),
        topbar: z.boolean().optional().default(true).describe("DISPLAY_SERIES: Spread-Kennzahlen als Topbar-Block setzen"),

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
    async ({ action, ticker, ticker_b, pairs, list_name, preset, timeframe, window_id, annotation, annotation_id, topbar_block, limit, limit_bars, mode, min_spread_pct, top_n, rank_by, overlay, overlay_periods, grid_cols, cell_w, cell_h, origin_x, origin_y, dry_run, topbar, resolution, hires, setup_name, new_setup_name, monitor_count }: any) => {

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

          if (!res.ok) {
            const errText = await res.text();
            throw new Error(`Chart Viewer Server rejected ADD_ANNOTATION: HTTP ${res.status} ${errText}`);
          }

          const data = await res.json();
          if (data.error) {
            throw new Error(`ADD_ANNOTATION failed on Chart Server: ${data.error}`);
          }

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
          if (!res.ok) {
            const errText = await res.text();
            throw new Error(`Chart Viewer Server rejected REMOVE_ANNOTATION: HTTP ${res.status} ${errText}`);
          }
          const data = await res.json();
          if (data.error) {
            throw new Error(`REMOVE_ANNOTATION failed on Chart Server: ${data.error}`);
          }
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
          if (!res.ok) {
            const errText = await res.text();
            throw new Error(`Chart Viewer Server rejected SET_TOPBAR: HTTP ${res.status} ${errText}`);
          }
          const data = await res.json();
          if (data.error) {
            throw new Error(`SET_TOPBAR failed on Chart Server: ${data.error}`);
          }
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
            const lower = list_name.trim().toLowerCase();
            if (lower === "all" || lower === "all.txt" || lower === "master" || lower === "universe") {
              log.info(`[chart_viewer] Fetching master universe from cda_master_universe...`);
              const { data, error } = await supabase
                .from("cda_master_universe")
                .select("ticker")
                .eq("has_parquet", true)
                .order("ticker")
                .limit(POSTGREST_UNIVERSE_LIMIT);

              if (error) throw new Error(`Supabase error: ${error.message}`);
              if (!data || data.length === 0) {
                throw new Error("Master universe in 'cda_master_universe' is empty.");
              }
              symbols = data.map((row: any) => row.ticker);
            } else {
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
            }
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

        // 14. DISPLAY_SERIES — on-the-fly long/short pair spreads (RAM only, no Parquet)
        if (action === "DISPLAY_SERIES") {
          const tfSpec = parseTimeframeSpec(tf);
          const spreadMode = String(mode || "pct").toLowerCase();
          if (!["pct", "ratio", "abs"].includes(spreadMode)) {
            throw new Error(`DISPLAY_SERIES: unsupported mode '${mode}'. Use pct | ratio | abs.`);
          }
          const barsLimit = Math.min(Math.max(10, Number(limit_bars) || 66), 2000);
          const minSpread = Number.isFinite(Number(min_spread_pct)) ? Number(min_spread_pct) : 30;
          const maxWindows = Math.min(Math.max(1, Number(top_n) || 12), SPREAD_WINDOW_CAP);
          const rankBy = String(rank_by || "total").toLowerCase();
          const overlayKind = String(overlay || "sma").toLowerCase();
          const overlayPeriods = (Array.isArray(overlay_periods) && overlay_periods.length > 0 ? overlay_periods : [20])
            .map((p: any) => Number(p))
            .filter((p: number) => Number.isFinite(p) && p > 0);
          const gridCols = Math.max(1, Number(grid_cols) || 3);
          const cellW = Math.max(320, Number(cell_w) || 700);
          const cellH = Math.max(240, Number(cell_h) || 460);
          const originX = Number.isFinite(Number(origin_x)) ? Number(origin_x) : 40;
          const originY = Number.isFinite(Number(origin_y)) ? Number(origin_y) : 40;

          // ── 1. Resolve the pair universe ──
          let rawSymbols: string[] = [];
          let universeLabel = "";
          const pairSpecs: Array<{ long: string; short: string }> = [];
          if (Array.isArray(pairs) && pairs.length > 0) {
            for (const raw of pairs) {
              const txt = String(raw).trim().toUpperCase();
              const parts = txt.includes("/") ? txt.split("/") : txt.split("-");
              const long = (parts[0] || "").trim();
              const short = (parts[1] || "").trim();
              if (long && short) pairSpecs.push({ long, short });
            }
            if (pairSpecs.length === 0) {
              throw new Error("DISPLAY_SERIES: 'pairs' konnte nicht geparst werden — Format 'LONG/SHORT' erwartet.");
            }
            rawSymbols = pairSpecs.flatMap((p) => [p.long, p.short]);
            universeLabel = `${pairSpecs.length} explizite Paare`;
          } else if (ticker && ticker_b) {
            rawSymbols = [ticker, ticker_b];
            universeLabel = `Einzelpaar ${String(ticker).toUpperCase()}/${String(ticker_b).toUpperCase()}`;
          } else if (ticker) {
            rawSymbols = String(ticker).split(",");
            universeLabel = "Ticker-Liste";
          } else {
            const actualList = (list_name || "etf_leaderboard").trim();
            const lower = actualList.toLowerCase();
            if (["all", "all.txt", "master", "universe"].includes(lower)) {
              const { data, error } = await supabase
                .from("cda_master_universe")
                .select("ticker")
                .eq("has_parquet", true)
                .order("ticker")
                .limit(POSTGREST_UNIVERSE_LIMIT);
              if (error) throw new Error(`Supabase error: ${error.message}`);
              rawSymbols = (data || []).map((row: any) => row.ticker);
            } else {
              const { data, error } = await supabase
                .from("pca_watchlists")
                .select("ticker")
                .eq("list_name", actualList);
              if (error) throw new Error(`Supabase error: ${error.message}`);
              if (!data || data.length === 0) throw new Error(`Watchlist '${actualList}' ist leer oder existiert nicht.`);
              rawSymbols = data.map((row: any) => row.ticker);
            }
            universeLabel = `Watchlist '${actualList}'`;
          }

          const symbols = [...new Set(
            rawSymbols.map((s) => String(s).trim().toUpperCase()).filter((s) => s.length > 0 && !s.startsWith("$")),
          )];
          if (symbols.length < 2) throw new Error("DISPLAY_SERIES: mindestens 2 Symbole erforderlich.");
          if (symbols.length > SPREAD_MAX_SYMBOLS) {
            throw new Error(
              `DISPLAY_SERIES: ${symbols.length} Symbole sind zu viele (max ${SPREAD_MAX_SYMBOLS} => ` +
              `${(SPREAD_MAX_SYMBOLS * (SPREAD_MAX_SYMBOLS - 1)) / 2} Paare). Bitte Universum einschränken.`,
            );
          }

          log.info(`[chart_viewer] DISPLAY_SERIES: ${symbols.length} Symbole aus ${universeLabel} | mode=${spreadMode} | ${barsLimit} Bars`);

          // ── 2. Fetch both legs into RAM ──
          const legMap = new Map<string, LegBar[]>();
          const skipped: Array<{ symbol: string; reason: string }> = [];
          for (let i = 0; i < symbols.length; i += SPREAD_FETCH_CONCURRENCY) {
            const chunk = symbols.slice(i, i + SPREAD_FETCH_CONCURRENCY);
            const results = await Promise.all(chunk.map(async (sym) => {
              try {
                const bars = await fetchLegBars(sym, tf, barsLimit);
                return { sym, bars, error: "" };
              } catch (e: any) {
                return { sym, bars: [] as LegBar[], error: e?.message || "fetch failed" };
              }
            }));
            for (const r of results) {
              if (r.error || r.bars.length < 3) skipped.push({ symbol: r.sym, reason: r.error || "zu wenige Bars" });
              else legMap.set(r.sym, r.bars);
            }
          }

          const available = symbols.filter((s) => legMap.has(s));
          if (available.length < 2) {
            throw new Error(
              `DISPLAY_SERIES: keine zwei Symbole mit Chartdaten. Übersprungen: ` +
              skipped.map((s) => `${s.symbol} (${s.reason})`).join(", "),
            );
          }

          // ── 3. Build the spread for every requested pair ──
          const presetName = (preset && !["default", "clean", ""].includes(String(preset).toLowerCase()))
            ? String(preset)
            : "";
          let presetData: any = null;
          if (presetName) {
            presetData = await fetchPresetData(presetName);
            log.info(`[chart_viewer] DISPLAY_SERIES: Preset '${presetName}' mit ${(presetData?.indicators || []).length} Mitgliedern geladen.`);
          }

          const buildCandidate = (symLong: string, symShort: string) => {
            const barsLong = legMap.get(symLong);
            const barsShort = legMap.get(symShort);
            if (!barsLong || !barsShort) return null;
            const aligned = alignPair(barsLong, barsShort, tfSpec.seconds);
            if (aligned.length < 3) return null;
            const spread = spreadFromAligned(aligned, spreadMode, tfSpec.seconds);
            if (!spread) return null;
            const metrics = spreadMetrics(spread.closes);
            return {
              long: symLong,
              short: symShort,
              total: metrics.total,
              maxDD: metrics.maxDD,
              quality: metrics.quality,
              retLong: (aligned[aligned.length - 1].a.c / aligned[0].a.c - 1) * 100,
              retShort: (aligned[aligned.length - 1].b.c / aligned[0].b.c - 1) * 100,
              barsCount: spread.bars.length,
              bars: spread.bars,
              closes: spread.closes,
              timestamps: spread.timestamps,
              adr: {} as { adr1?: number; adr20?: number },
              unavailable: [] as string[],
            };
          };

          const candidates: any[] = [];
          if (pairSpecs.length > 0) {
            // Explicit pair list: keep the given order, no filter/ranking.
            for (const spec of pairSpecs) {
              const built = buildCandidate(spec.long, spec.short);
              if (built) candidates.push(built);
              else skipped.push({ symbol: `${spec.long}/${spec.short}`, reason: "keine überlappenden Bars" });
            }
          } else {
            // Full C(n,2) enumeration, oriented long = stronger leg.
            for (let i = 0; i < available.length; i++) {
              for (let j = i + 1; j < available.length; j++) {
                const symA = available[i];
                const symB = available[j];
                const aligned = alignPair(legMap.get(symA)!, legMap.get(symB)!, tfSpec.seconds);
                if (aligned.length < 3) continue;
                const retA = aligned[aligned.length - 1].a.c / aligned[0].a.c - 1;
                const retB = aligned[aligned.length - 1].b.c / aligned[0].b.c - 1;
                const built = retA >= retB ? buildCandidate(symA, symB) : buildCandidate(symB, symA);
                if (built) candidates.push(built);
              }
            }
          }

          const ranked = pairSpecs.length > 0
            ? candidates
            : candidates
              .filter((c) => c.total >= minSpread)
              .sort((x, y) => (rankBy === "quality" ? y.quality - x.quality : y.total - x.total));

          // ── 4. Push the top pairs as tiled windows (unless dry_run) ──
          const pushed: any[] = [];
          const targets = dry_run ? [] : ranked.slice(0, maxWindows);
          for (let idx = 0; idx < targets.length; idx++) {
            const entry = targets[idx];
            let overlays: any[] = [];
            try {
              if (presetData) {
                const builtPreset = buildPresetOverlays(presetData, entry.bars, entry.timestamps);
                overlays = builtPreset.overlays;
                entry.unavailable = builtPreset.unavailable;
                entry.adr = builtPreset.adrInfo;
              } else {
                overlays = buildSpreadOverlays(overlayKind, overlayPeriods, entry.bars, entry.timestamps);
              }
            } catch (e: any) {
              log.warn(`[chart_viewer] Spread-Overlay fehlgeschlagen (${entry.long}/${entry.short}): ${e?.message}`);
            }

            const winId = (window_id && targets.length === 1)
              ? window_id
              : `win_${sanitizeWindowId(entry.long)}_${sanitizeWindowId(entry.short)}_spread_${sanitizeWindowId(tf)}`;
            const col = idx % gridCols;
            const row = Math.floor(idx / gridCols);
            const position = { x: originX + col * (cellW + 10), y: originY + row * (cellH + 10) };
            const size = { width: cellW, height: cellH };

            try {
              const res = await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  action: "OPEN_WINDOW",
                  window_id: winId,
                  symbol: `${entry.long}-${entry.short} ${spreadMode}`,
                  timeframe: { unit: tfSpec.unit, multiplier: tfSpec.multiplier },
                  bars: entry.bars,
                  overlays,
                  annotations: [],
                  position,
                  size,
                }),
              });
              const data = await res.json().catch(() => ({}));
              if (!res.ok || (data && data.error)) {
                pushed.push({ ...spreadSummary(entry), window_id: winId, status: "error", error: (data && data.error) || `HTTP ${res.status}` });
                continue;
              }

              if (topbar !== false) {
                await fetch(`${CHART_VIEWER_API_URL}/api/command`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    action: "SET_TOPBAR",
                    window_id: winId,
                    block_id: "spread_stats",
                    position: { row: 0, col: 0 },
                    content: `${entry.long}-${entry.short}  Spread ${entry.total.toFixed(2)} pp  |  maxDD -${entry.maxDD.toFixed(2)} pp  |  Q ${entry.quality.toFixed(2)}  |  Long ${entry.long} ${entry.retLong.toFixed(1)}% / Short ${entry.short} ${entry.retShort.toFixed(1)}%` +
                      (entry.adr && entry.adr.adr20 !== undefined ? `  |  ADR20 ${entry.adr.adr20.toFixed(2)} pp` : ""),
                  }),
                }).catch(() => {});
              }
              pushed.push({ ...spreadSummary(entry), window_id: winId, status: "ok" });
            } catch (e: any) {
              pushed.push({ ...spreadSummary(entry), window_id: winId, status: "error", error: e?.message || "push failed" });
            }
          }

          // ── 5. Report ──
          const table = ranked.slice(0, 15).map((c: any, i: number) => {
            const wId = pushed.find((p) => p.long === c.long && p.short === c.short)?.window_id || "—";
            return `| ${i + 1} | ${c.long} / ${c.short} | ${c.total.toFixed(2)} | ${c.maxDD.toFixed(2)} | ${c.quality.toFixed(2)} | ${c.retLong.toFixed(1)}% / ${c.retShort.toFixed(1)}% | ${wId} |`;
          }).join("\n");

          const lines: string[] = [
            `🔗 **DISPLAY_SERIES** | ${available.length}/${symbols.length} Symbole mit Daten → ${candidates.length} Paare | Mode \`${spreadMode}\` | ${barsLimit} Bars (${tf})` +
            (dry_run
              ? " | **DRY RUN — keine Fenster geöffnet**"
              : ` | ${pushed.filter((p) => p.status === "ok").length} Fenster geöffnet`),
            ``,
            `**Filter:** ${pairSpecs.length > 0 ? `explizite Paarliste (${pairSpecs.length} Paare, Reihenfolge beibehalten)` : `Spread >= ${minSpread} pp → ${ranked.length} Paare`} | Ranking: \`${pairSpecs.length > 0 ? "wie vorgegeben" : rankBy}\` | ${presetData ? `Preset \`${presetName}\` (${(presetData.indicators || []).length} Mitglieder auf dem Spread)` : `Overlay \`${overlayKind}\`${overlayKind !== "none" ? ` ${JSON.stringify(overlayPeriods)}` : ""}`}`,
            ``,
            `| # | Long / Short | Spread pp | maxDD | Quality | Legs (long/short) | Fenster |`,
            `| --: | :-- | --: | --: | --: | :-- | :-- |`,
            table || `| — | keine Paare über dem Filter | — | — | — | — | — |`,
          ];
          if (skipped.length > 0) {
            lines.push(``);
            lines.push(`⚠️ Übersprungen (${skipped.length}): ${skipped.map((s) => `${s.symbol} — ${s.reason}`).join("; ")}`);
          }
          const presetMissing = [...new Set(targets.flatMap((t: any) => t.unavailable || []))];
          if (presetMissing.length > 0) {
            lines.push(``);
            lines.push(`ℹ️ Preset-Mitglieder ohne Spread-Äquivalent / zu wenig Bars: ${presetMissing.join(", ")}`);
          }
          if (!dry_run) {
            lines.push(``);
            lines.push(`📌 Fenster erscheinen, sobald der Desktop-Client verbunden ist (Viewer ist zustandslos).`);
          }

          return {
            content: [{
              type: "text",
              text: lines.join("\n") + "\n\n```json\n" + JSON.stringify({
                status: "success",
                action,
                mode: spreadMode,
                timeframe: tf,
                bars: barsLimit,
                universe: universeLabel,
                symbols_with_data: available.length,
                pairs_evaluated: candidates.length,
                pairs_above_filter: ranked.length,
                min_spread_pct: minSpread,
                rank_by: pairSpecs.length > 0 ? "explicit_pairs" : rankBy,
                preset: presetName || null,
                pairs: pairSpecs.length > 0 ? pairSpecs.map((p) => `${p.long}/${p.short}`) : null,
                dry_run: !!dry_run,
                pushed,
                top_pairs: ranked.slice(0, 15).map(spreadSummary),
                skipped,
              }, null, 2) + "\n```",
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
