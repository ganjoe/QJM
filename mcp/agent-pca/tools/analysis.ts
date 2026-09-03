import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { PCA_SERVICE_URL, log } from "./shared.ts";

export function registerAnalysisTools(server: McpServer) {

  // ─────────────────────────────────────────────────────────────────────────────
  // 1. get_timeseries — OHLCV + Precalculated Features from Parquet
  // ─────────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "get_timeseries",
    {
      title: "Get Time Series (Historical OHLCV + Precalculated Features)",
      description: "Fetches historical daily OHLCV candle bars and precalculated technical features (MAs, Bollinger Bands, Minervini score, ADR, RS rating) from local Parquet storage for a specific ticker and date range. Automatically detects staleness/lags at the series end.\n\n" +
        "WHEN TO USE: Use whenever you need daily historical chart data, candle history, or precalculated technical indicators for a stock.\n" +
        "WHEN NOT TO USE: Do NOT use for live real-time intraday quotes (use `get_quote` in agent-pta instead). Do NOT use for custom indicator periods like EMA 21 or custom Bollinger (use `calculate_indicator` instead).\n" +
        "NOTE: Parameter `limit` is strictly REQUIRED to prevent context flooding. Always pass how many candles you need (e.g. limit: 50, 100).",
      inputSchema: {
        ticker: z.string().describe("Stock ticker symbol (e.g. 'MSFT', 'NVDA')"),
        timeframe: z.string().optional().default("1D").describe("Candle timeframe (currently '1D' is populated with data)"),
        from: z.union([z.string(), z.number()]).optional().describe("Start date (inclusive): ISO date 'YYYY-MM-DD' or Unix timestamp in seconds"),
        to: z.union([z.string(), z.number()]).optional().describe("End date (inclusive): ISO date 'YYYY-MM-DD' or Unix timestamp in seconds"),
        limit: z.number().describe("Number of candles before date filtering (REQUIRED to prevent context flooding. Recommended: 30-100, Max 2000)"),
        features: z.boolean().optional().default(true).describe("Whether to include precalculated feature columns (Default: true)"),
      },
    },
    async ({ ticker, timeframe, from, to, limit, features }: any) => {
      try {
        if (limit === undefined || limit === null || typeof limit !== "number" || isNaN(limit) || limit <= 0) {
          throw new Error("Parameter 'limit' is required for get_timeseries to prevent context flooding. Please specify how many candles you need (e.g. limit: 50, limit: 100).");
        }
        const sym = ticker.toUpperCase();
        const capped = Math.min(Math.max(1, Math.floor(limit)), 2000);
        const tf = timeframe || "1D";
        const wantFeatures = features !== false;

        const url = `${PCA_SERVICE_URL}/api/chartdata?symbol=${encodeURIComponent(sym)}&timeframe=${encodeURIComponent(tf)}&limit=${capped}&features=${wantFeatures ? "true" : "false"}`;
        const res = await fetch(url);
        if (!res.ok) {
          const errText = (await res.text()).slice(0, 300);
          throw new Error(`PCA-Service HTTP ${res.status}: ${errText}`);
        }

        const payload = await res.json();
        if (payload.status && payload.status !== "ok") {
          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                ticker: sym,
                timeframe: tf,
                status: payload.status,
                count: 0,
                notice: payload.notice || `Keine Daten für ${sym} vorhanden.`,
              }, null, 2),
            }],
          };
        }

        const columns: string[] = payload.columns || [];
        const rows: any[] = payload.data || [];
        const idx = (name: string) => columns.indexOf(name);
        const TI = idx("timestamp"), OI = idx("open"), HI = idx("high"), LI = idx("low"), CI = idx("close"), VI = idx("volume");

        if (TI === -1 || OI === -1 || CI === -1) {
          throw new Error(`Unerwartetes Schema vom PCA-Service: ${columns.join(", ")}`);
        }

        const toSec = (v: any, isEnd: boolean): number | null => {
          if (v === undefined || v === null || v === "") return null;
          if (typeof v === "number") return Math.floor(v);
          const s = String(v).trim();
          if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
            const d = new Date(s + (isEnd ? "T23:59:59.999Z" : "T00:00:00.000Z"));
            return Math.floor(d.getTime() / 1000);
          }
          const d = new Date(s);
          return Number.isNaN(d.getTime()) ? null : Math.floor(d.getTime() / 1000);
        };

        const fromSec = toSec(from, false);
        const toSecEnd = toSec(to, true);

        const filtered = rows.filter((r: any) => {
          const t = r[TI];
          if (fromSec !== null && t < fromSec) return false;
          if (toSecEnd !== null && t > toSecEnd) return false;
          return true;
        });

        if (filtered.length === 0) {
          const known = rows.length > 0 ? { min: rows[0][TI], max: rows[rows.length - 1][TI], count: rows.length } : null;
          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                ticker: sym,
                timeframe: tf,
                count: 0,
                bars: [],
                notice: known
                  ? `Keine Daten im Zeitraum ${fromSec ?? "Start"} bis ${toSecEnd ?? "Ende"}. Verfügbar: ${known.min} bis ${known.max} (${known.count} Candles).`
                  : `Keine Daten vorhanden für ${sym}.`,
              }, null, 2),
            }],
          };
        }

        const formatPrecision = (val: any): any => {
          if (typeof val !== "number" || !Number.isFinite(val)) return val;
          const decimals = Math.abs(val) > 100 ? 1 : 2;
          const factor = 10 ** decimals;
          return Math.round(val * factor) / factor;
        };

        const featureCols = columns.filter((c: string) => !["timestamp", "open", "high", "low", "close", "volume"].includes(c));
        const bars = filtered.map((r: any) => {
          const bar: Record<string, any> = {
            timestamp: r[TI],
            open: formatPrecision(r[OI]),
            high: formatPrecision(r[HI]),
            low: formatPrecision(r[LI]),
            close: formatPrecision(r[CI]),
            volume: r[VI],
          };
          if (wantFeatures && featureCols.length > 0) {
            const feats: Record<string, any> = {};
            for (const col of featureCols) {
              const val = r[idx(col)];
              if (val !== null && val !== undefined && val === val) {
                feats[col] = formatPrecision(val);
              }
            }
            if (Object.keys(feats).length > 0) {
              bar.features = feats;
            }
          }
          return bar;
        });

        const result: Record<string, any> = {
          ticker: sym,
          timeframe: tf,
          range: {
            from: filtered[0][TI],
            to: filtered[filtered.length - 1][TI],
            count: filtered.length,
          },
          features_stale: payload.features_stale ?? false,
          lag_bars: payload.lag_bars ?? 0,
          lag_days: payload.lag_days ?? 0,
          notice: payload.notice ?? null,
          bars,
        };

        return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
      } catch (err: any) {
        log.error(`get_timeseries error: ${err.message}`);
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );


  // ─────────────────────────────────────────────────────────────────────────────
  // 2. list_available_features — Available precalculated feature columns
  // ─────────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "list_available_features",
    {
      title: "List Available Precalculated Features",
      description: "Returns the complete schema of all 21+ precalculated technical indicator columns stored in local Parquet files (e.g. ma_sma_50, ma_sma_200, bb_20_upper, minervini_score, adr_20, ibd_rs).\n\n" +
        "WHEN TO USE: Call this tool first to discover available column names before querying or interpreting `get_timeseries`.",
      inputSchema: {
        ticker: z.string().optional().describe("Optional: Stock ticker symbol to verify schema against a specific stock file"),
      },
    },
    async ({ ticker }: any) => {
      try {
        const query = ticker ? `?symbol=${encodeURIComponent(ticker.toUpperCase())}` : "";
        const res = await fetch(`${PCA_SERVICE_URL}/api/features/schema${query}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
        const data = await res.json();
        return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );


  // ─────────────────────────────────────────────────────────────────────────────
  // 3. calculate_indicator — On-the-fly technical indicator calculations
  // ─────────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "calculate_indicator",
    {
      title: "Calculate Technical Indicator On-The-Fly",
      description: "Calculates technical indicators dynamically on-the-fly for custom parameters, custom lookbacks, or batch period arrays (e.g. periods: [10, 20, 50, 200]). Supports SMA, EMA, BOLLINGER, and STOCHASTIC on configurable price sources (close, open, high, low, volume).\n\n" +
        "WHEN TO USE: Use when you need custom indicator calculations (e.g., 21 EMA, 10 SMA, custom Stochastic) not found in precalculated features, or batch lookback comparisons on a single stock.\n" +
        "WHEN NOT TO USE: For standard precalculated indicators (like 50/200 SMA, Minervini score), use `get_timeseries`. Do NOT use for bulk database calculations (use `manage_feature_calculation`).",
      inputSchema: {
        ticker: z.string().describe("Stock ticker symbol (e.g. 'MSFT', 'AAPL')"),
        indicator_type: z.enum(["SMA", "EMA", "BOLLINGER", "STOCHASTIC"]).describe("The indicator type to calculate"),
        periods: z.array(z.number()).optional().describe("Batch array of lookback periods (e.g. [10, 20, 50, 200])"),
        period: z.number().optional().describe("Single lookback period (e.g. 20)"),
        source: z.enum(["close", "open", "high", "low", "volume"]).optional().default("close").describe("Price source column for calculation (Default: 'close')"),
        timeframe: z.string().optional().default("1D").describe("Timeframe (Default: '1D')"),
        limit: z.number().optional().default(200).describe("Number of candles to calculate over (Default: 200, Max: 2000)"),
        std_dev: z.number().optional().default(2.0).describe("Standard deviation multiplier for Bollinger Bands (Default: 2.0)"),
        k_period: z.number().optional().default(14).describe("Stochastic %K period (Default: 14)"),
        d_period: z.number().optional().default(3).describe("Stochastic %D period (Default: 3)"),
        slowing: z.number().optional().default(3).describe("Stochastic slowing period (Default: 3)"),
      },
    },
    async ({ ticker, indicator_type, periods, period, source, timeframe, limit, std_dev, k_period, d_period, slowing }: any) => {
      try {
        const body: Record<string, any> = {
          symbol: ticker.toUpperCase(),
          timeframe: timeframe || "1D",
          limit: limit ?? 200,
          source: source || "close",
          indicator_type,
        };

        if (periods && periods.length > 0) {
          body.periods = periods;
        } else if (period) {
          body.periods = [period];
        }

        if (indicator_type === "BOLLINGER") {
          body.std_dev = std_dev ?? 2.0;
        } else if (indicator_type === "STOCHASTIC") {
          body.k_period = k_period ?? 14;
          body.d_period = d_period ?? 3;
          body.slowing = slowing ?? 3;
        }

        const res = await fetch(`${PCA_SERVICE_URL}/api/indicators/calculate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });

        if (!res.ok) {
          const errText = (await res.text()).slice(0, 300);
          throw new Error(`HTTP ${res.status}: ${errText}`);
        }

        const data = await res.json();
        return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
      } catch (err: any) {
        log.error(`calculate_indicator error: ${err.message}`);
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );


  // ─────────────────────────────────────────────────────────────────────────────
  // 4. run_technical_scanner — Technical Analysis Scanner Framework
  // ─────────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "run_technical_scanner",
    {
      title: "Run Technical Stock Scanner",
      description: "Evaluates technical pattern scanners on-the-fly across a list of tickers. Returns matches (true/false), trend template scores, and detailed metric evaluations for each ticker.\n\n" +
        "AVAILABLE SCANNERS:\n" +
        "- 'minervini_trend': Evaluates Mark Minervini's Trend Template (Score 0-6). Checks Stage 2 uptrend criteria: 200 SMA trending up, Price > 150 & 200 SMA, 50 SMA > 150 & 200 SMA.\n" +
        "- 'sma_cross': Analyzes 50/200 SMA Golden Cross & Death Cross status and spread percentage.\n\n" +
        "WHEN TO USE: Use whenever asked to scan, screen, or filter a watchlist or list of tickers for Minervini trend template compliance or moving average breakouts.",
      inputSchema: {
        scanners: z.array(z.string()).describe("Array of scanner names to execute (e.g. ['minervini_trend'] or ['minervini_trend', 'sma_cross'])"),
        tickers: z.array(z.string()).describe("List of stock ticker symbols to evaluate (e.g. ['AAPL', 'NVDA', 'MSFT'])"),
        timeframe: z.string().optional().default("1D").describe("Candle timeframe (Default: '1D')"),
      },
    },
    async ({ scanners, tickers, timeframe }: any) => {
      try {
        const res = await fetch(`${PCA_SERVICE_URL}/api/scanner/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            scanners,
            tickers: tickers.map((t: string) => t.toUpperCase()),
            timeframe: timeframe || "1D",
          }),
        });

        if (!res.ok) {
          const errText = (await res.text()).slice(0, 300);
          throw new Error(`HTTP ${res.status}: ${errText}`);
        }

        const data = await res.json();
        return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

}
