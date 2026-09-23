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
      description: "Calculates technical indicators dynamically on-the-fly for custom parameters, lookbacks, batch periods, or direct value arrays. Supports SMA, EMA, BOLLINGER, STOCHASTIC, ADR_PCT, and DAYS_BACK on configurable sources (close, open, high, low, volume, or feature columns like breadth_40_pct).\n\n" +
        "WHEN TO USE: Use when you need custom indicator calculations (e.g., 21 EMA, 10 SMA, custom Stochastic), live days_back extremes on any series, or calculations on directly passed number arrays.\n" +
        "WHEN NOT TO USE: For standard precalculated indicators (like 50/200 SMA, Minervini score), use `get_timeseries`. Do NOT use for bulk database calculations (use `manage_feature_calculation`).",
      inputSchema: {
        ticker: z.string().optional().describe("Stock ticker symbol (e.g. 'MSFT', 'AAPL', '$STATS.MARKET_BREADTH'). Optional if 'values' is provided."),
        values: z.array(z.number()).optional().describe("Direct array of numbers to calculate indicator on (e.g. custom series, live data, or passed lists)"),
        indicator_type: z.enum(["SMA", "EMA", "BOLLINGER", "STOCHASTIC", "ADR_PCT", "DAYS_BACK"]).describe("The indicator type to calculate"),
        periods: z.array(z.number()).optional().describe("Batch array of lookback periods (e.g. [10, 20, 50, 200])"),
        period: z.number().optional().describe("Single lookback period (e.g. 20)"),
        source: z.string().optional().default("close").describe("Price or feature source column for calculation (Default: 'close', e.g. 'close', 'volume', 'breadth_40_pct')"),
        timeframe: z.string().optional().default("1D").describe("Timeframe (Default: '1D')"),
        limit: z.number().optional().default(200).describe("Number of candles to calculate over (Default: 200, Max: 2000)"),
        std_dev: z.number().optional().default(2.0).describe("Standard deviation multiplier for Bollinger Bands (Default: 2.0)"),
        k_period: z.number().optional().default(14).describe("Stochastic %K period (Default: 14)"),
        d_period: z.number().optional().default(3).describe("Stochastic %D period (Default: 3)"),
        slowing: z.number().optional().default(3).describe("Stochastic slowing period (Default: 3)"),
      },
    },
    async ({ ticker, values, indicator_type, periods, period, source, timeframe, limit, std_dev, k_period, d_period, slowing }: any) => {
      try {
        if (!ticker && (!values || values.length === 0)) {
          throw new Error("Entweder 'ticker' oder 'values' muss angegeben werden.");
        }

        const body: Record<string, any> = {
          timeframe: timeframe || "1D",
          limit: limit ?? 200,
          source: source || "close",
          indicator_type,
        };

        if (ticker) {
          body.symbol = ticker.toUpperCase();
        }
        if (values && Array.isArray(values)) {
          body.values = values;
        }

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
      description: "Screens a ticker universe with the registered technical scanners — either as a plain true/false check on the latest bar or as a list of hits inside a time range.\n\n" +
        "UNIVERSE (union of both sources, de-duplicated, '$'-prefixed virtual tickers dropped):\n" +
        "- `tickers`: explicit symbols, e.g. ['AAPL', 'NVDA'].\n" +
        "- `watchlists`: Supabase watchlist references — a bare list name ('current_positions'), a PCA service link ('http://<host>:8794/api/watchlists/current_positions'), a PostgREST link containing 'list_name=eq.<name>', or a '<name>.txt' file.\n\n" +
        "OUTPUT MODES:\n" +
        "- WITHOUT `from`/`to` ('latest' mode): only the last available bar of each ticker is evaluated and the answer is a plain true/false map per ticker and scanner — no hit list.\n" +
        "- WITH `from` and/or `to` ('range' mode, inclusive bounds): every bar inside the window is evaluated causally (warm-up bars are loaded before 'from', no look-ahead) and the answer is a list of hits with ticker, scanner, hit date, score and matched bars.\n\n" +
        "Tickers without parquet data, without enough history or without bars in the window are reported in `skipped` with a machine readable reason — they are never silently reported as false.\n\n" +
        "AVAILABLE SCANNERS (run `list_scanners` for live metadata and history requirements):\n" +
        "- 'dcr': Day Close Range (DCR) in %: evaluates the position of the close within the daily candle's range ((Close - Low) / (High - Low) * 100). Used to identify leading groups and strong intraday accumulation/distribution; the aggregate count of high-ranking closes serves as a very short-term market breadth indicator. Supports 'direction' ('long' or 'short'), 'cutoff' in %, 'count' (top N). Needs 1 bar.\n" +
        "- 'wcr': Week Close Range (WCR) in %: evaluates the position of the close within the week-to-date candle's range ((Close - WeekLow) / (WeekHigh - WeekLow) * 100). Used to identify leading groups and weekly accumulation/distribution; the aggregate count of high-ranking closes serves as a very short-term market breadth indicator. Supports 'direction' ('long' or 'short'), 'cutoff' in %, 'count' (top N). Needs 1 bar.\n" +
        "- 'madbo': MADBO — Moving Average Dollar Volume Breakout. The five close SMAs (10/20/50/100/200) form a fan narrower than the bar's true range (ATR(1)) while dollar volume (close x volume) exceeds 2x its 50-bar average. Score = dollar-volume multiple. Needs 200 bars.\n" +
        "- 'minervini_trend': Minervini Trend Template (score 0-6, matched at >= 5: 200 SMA trending up, price above the 150 & 200 SMA, 50 SMA above the 150 & 200 SMA, >= 25% above the 52-week low, within 25% of the 52-week high). Needs 252 bars.\n" +
        "- 'sma_cross': 50/200 SMA golden cross active; score is the spread in percent. Needs 200 bars.\n" +
        "- 'phoenix': Phoenix — RS Rating Rebirth: the IBD Relative Strength rating (ibd_rs, 1-99) fell to or below 'rs_from' (default 30) at some point inside the last 'days' bars (default 20) and has since climbed to or above 'rs_to' (default 80). Flags relative-strength turnarounds from laggard to leadership. Params via scanner_params: { phoenix: { days, rs_from, rs_to } }. Score = RS rating on the matched bar. Needs 30 bars + the feature parquet.\n\n" +
        "WHEN TO USE: whenever asked to screen or scan a watchlist/ticker list for a pattern, or to find out WHEN a setup triggered inside a period.\n" +
        "WHEN NOT TO USE: for raw candle history use `get_timeseries`; for the live price use `get_quote`.",
      inputSchema: {
        scanners: z.array(z.string()).describe("Scanner names to run, e.g. ['dcr'], ['wcr'] or ['madbo', 'minervini_trend']"),
        tickers: z.array(z.string()).optional().describe("Explicit tickers to scan, e.g. ['AAPL', 'NVDA', 'MSFT']. If omitted and watchlists omitted, scans the entire database universe ('all') and returns matching tickers."),
        watchlists: z.array(z.string()).optional().describe("Supabase watchlist references: 'all' (entire 5,500+ universe), 'current_positions', etc. If omitted and tickers omitted, defaults to 'all'."),
        from: z.union([z.string(), z.number()]).optional().describe("Inclusive range start ('YYYY-MM-DD' or Unix seconds). Supplying from/to switches the output to a hit list"),
        to: z.union([z.string(), z.number()]).optional().describe("Inclusive range end ('YYYY-MM-DD' means end of that day; or Unix seconds)"),
        timeframe: z.string().optional().default("1D").describe("Candle timeframe (only '1D' is populated)"),
        hit_mode: z.enum(["first_of_episode", "all_bars"]).optional().default("first_of_episode").describe("Collapse consecutive matching bars into one hit (default) or return every matching bar"),
        limit_hits: z.number().optional().default(200).describe("Maximum hits returned (default 200, max 1000)"),
        max_tickers: z.number().optional().default(6000).describe("Universe cap (default 6000, covers full 5,500+ database)"),
        direction: z.enum(["long", "short"]).optional().default("long").describe("Direction for directional scanners like DCR/WCR: 'long' (closing near highs) or 'short' (closing near lows). Default: 'long'"),
        cutoff: z.number().optional().describe("Cutoff threshold in % for DCR/WCR (e.g. 90 for DCR >= 90% long, 10 for DCR <= 10% short)"),
        count: z.number().optional().describe("Maximum number of top matches to return, ranked by score among tickers that already matched (e.g. 10 or 20)"),
        scanner_params: z.record(z.string(), z.record(z.string(), z.any())).optional().describe("Per-scanner parameter overrides, e.g. { phoenix: { days: 20, rs_from: 30, rs_to: 80 } } or { dcr: { cutoff: 85 } }."),
        warmup_bars: z.number().optional().describe("Bars loaded before 'from' for indicator warm-up (default: the selected scanners' min_bars). Raise this when phoenix 'days' exceeds 30."),
      },
    },
    async ({ scanners, tickers, watchlists, from, to, timeframe, hit_mode, limit_hits, max_tickers, direction, cutoff, count, scanner_params, warmup_bars }: any) => {
      try {
        const body: Record<string, any> = {
          scanners,
          timeframe: timeframe || "1D",
          hit_mode: hit_mode || "first_of_episode",
          limit_hits: limit_hits ?? 200,
          max_tickers: max_tickers ?? 6000,
          direction: direction || "long",
        };
        if (cutoff !== undefined && cutoff !== null) body.cutoff = cutoff;
        if (count !== undefined && count !== null) body.count = count;
        if (scanner_params && typeof scanner_params === "object") body.scanner_params = scanner_params;
        if (warmup_bars !== undefined && warmup_bars !== null) body.warmup_bars = warmup_bars;
        if (Array.isArray(tickers) && tickers.length > 0) {
          body.tickers = tickers.map((t: string) => String(t).trim().toUpperCase()).filter(Boolean);
        }
        if (Array.isArray(watchlists) && watchlists.length > 0) {
          body.watchlists = watchlists.map((w: string) => String(w).trim()).filter(Boolean);
        }
        if (from !== undefined && from !== null && from !== "") body.from = from;
        if (to !== undefined && to !== null && to !== "") body.to = to;

        // If neither tickers nor watchlists specified, default to entire master universe "all"
        if (!body.tickers && !body.watchlists) {
          body.watchlists = ["all"];
        }

        const res = await fetch(`${PCA_SERVICE_URL}/api/scanner/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });

        if (!res.ok) {
          const errText = (await res.text()).slice(0, 300);
          throw new Error(`HTTP ${res.status}: ${errText}`);
        }

        const data = await res.json();
        const lines: string[] = [];
        const scannerNames: string[] = data.scanners || [];

        if (data.mode === "latest") {
          const scanned: string[] = Object.keys(data.matches || {});
          let matchedTickers = scanned.filter((t: string) => scannerNames.some((s: string) => data.matches[t]?.[s] === true));
          if (data.scores) {
            const primaryScanner = scannerNames[0];
            matchedTickers.sort((a: string, b: string) => {
              const sa = data.scores[a]?.[primaryScanner] ?? -9999;
              const sb = data.scores[b]?.[primaryScanner] ?? -9999;
              return body.direction === "short" ? (sa - sb) : (sb - sa);
            });
          }
          const matchSummary = scannerNames.map((s: string) => `${s}: ${data.true_count?.[s] ?? 0}`).join(" | ");
          lines.push(`🔍 **Scanner — latest bar** (${scannerNames.join(", ")}) | timeframe ${data.timeframe} | as of ${data.as_of ?? "n/a"} | ${scanned.length} tickers`);
          if (matchedTickers.length > 0) {
            const header = `| Ticker | ${scannerNames.join(" | ")} |`;
            const sep = `| :--- |${scannerNames.map(() => " :---: |").join("")}`;
            const rows = matchedTickers.slice(0, 300).map((t: string) => {
              const cells = scannerNames.map((s: string) => {
                const isMatched = Boolean(data.matches[t]?.[s]);
                const score = data.scores?.[t]?.[s];
                if (isMatched) {
                  return score !== undefined && score !== null ? `✅ ${score}%` : "✅";
                } else {
                  return score !== undefined && score !== null ? `❌ ${score}%` : "❌";
                }
              }).join(" | ");
              return `| ${t} | ${cells} |`;
            });
            lines.push([header, sep, ...rows].join("\n"));
            if (matchedTickers.length > 300) lines.push(`_… ${matchedTickers.length - 300} further matches omitted._`);
            lines.push(`📋 **Matching Tickers (${matchedTickers.length}):** \`${matchedTickers.join("`, `")}\``);
          } else {
            lines.push("_No matches found._");
          }
          lines.push(`**Matches:** ${matchSummary}`);
        } else {
          const hits: any[] = data.hits || [];
          const summary = data.summary || {};
          const range = data.range || {};
          lines.push(`🎯 **Scanner hits** (${scannerNames.join(", ")}) | ${range.from ?? "series start"} → ${range.to ?? "series end"} | hit_mode ${range.hit_mode ?? "first_of_episode"} | ${summary.hits_total ?? 0} hits in ${summary.tickers_with_hits ?? 0} tickers`);
          if (hits.length > 0) {
            const header = "| Ticker | Scanner | Hit date | Score | Bars | Last match |";
            const sep = "| :--- | :--- | :--- | :---: | :---: | :--- |";
            const rows = hits.map((h: any) =>
              `| ${h.ticker} | ${h.scanner} | ${h.date} | ${h.score ?? "–"} | ${h.bars_matched ?? 1} | ${h.last_match_date ?? h.date} |`);
            lines.push([header, sep, ...rows].join("\n"));
          } else {
            lines.push("_No hits in this window._");
          }
          if (summary.by_ticker && Object.keys(summary.by_ticker).length > 0) {
            const hitTickers = Object.keys(summary.by_ticker);
            const perTicker = Object.entries(summary.by_ticker).map(([t, c]) => `${t} (${c})`).join(", ");
            lines.push(`**By ticker:** ${perTicker}`);
            lines.push(`📋 **Matching Tickers (${hitTickers.length}):** \`${hitTickers.join("`, `")}\``);
          }
          if (summary.truncated) {
            lines.push(`⚠️ Output truncated: ${summary.hits_total} hits found, ${summary.hits_returned} returned — narrow the range or raise 'limit_hits'.`);
          }
        }

        const skipped: any[] = data.skipped || [];
        if (skipped.length > 0) {
          const byReason: Record<string, number> = {};
          for (const s of skipped) byReason[s.reason] = (byReason[s.reason] || 0) + 1;
          lines.push(`**Skipped (${skipped.length}):** ${Object.entries(byReason).map(([r, c]) => `${r}: ${c}`).join(", ")}`);
        }
        for (const w of data.warnings || []) lines.push(`⚠️ ${w}`);

        return { content: [{ type: "text", text: lines.join("\n\n") }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  // ─────────────────────────────────────────────────────────────────────────────
  // 4b. list_scanners — Live metadata of the registered scanner framework
  // ─────────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "list_scanners",
    {
      title: "List Registered Technical Scanners",
      description: "Lists every scanner registered in the PCA scanner framework with its metadata (minimum bars of history, feature requirement, range support), plus the available hit modes, output modes and accepted universe sources.\n\n" +
        "WHEN TO USE: before calling `run_technical_scanner` to confirm the exact scanner names and their history requirements, or when a scan returned an unknown-scanner error.",
      inputSchema: {},
    },
    async () => {
      try {
        const res = await fetch(`${PCA_SERVICE_URL}/api/scanner/list`);
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
        const data = await res.json();
        const scanners: any[] = data.scanners || [];

        const header = "| Scanner | Min bars | Features | Range | Description |";
        const sep = "| :--- | :---: | :---: | :---: | :--- |";
        const rows = scanners.map((s: any) =>
          `| \`${s.name}\` | ${s.min_bars} | ${s.requires_features ? "yes" : "no"} | ${s.support_range ? "yes" : "no"} | ${s.description} |`);
        const modeLines = Object.entries(data.output_modes || {}).map(([k, v]) => `- **${k}**: ${v}`).join("\n");

        const text = `🧰 **Registered scanners (${data.count ?? scanners.length})**\n\n${[header, sep, ...rows].join("\n")}\n\n` +
          `**Output modes**\n${modeLines}\n\n` +
          `**Hit modes:** ${Object.keys(data.hit_modes || {}).join(", ")}\n\n` +
          `**Universe sources:**\n${(data.universe_sources || []).map((u: string) => `- ${u}`).join("\n")}\n\n` +
          "```json\n" + JSON.stringify(data, null, 2) + "\n```";

        return { content: [{ type: "text", text }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  // ─────────────────────────────────────────────────────────────────────────────
  // 5. get_market_breadth — On-the-fly market breadth calculation
  // ─────────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "get_market_breadth",
    {
      title: "Get Market Breadth On-The-Fly",
      description:
        "Calculates universe or watchlist market breadth on-the-fly (% of stocks with close > MA).\n\n" +
        "WHEN TO USE: Use when querying market participation/breadth over recent days (e.g. % stocks > 200 SMA, 100 SMA, 50 SMA, 20 EMA).\n" +
        "WHEN NOT TO USE: For single-stock technical indicators, use `calculate_indicator` or `get_timeseries`.",
      inputSchema: {
        period: z.number().optional().default(200).describe("Moving average lookback period (e.g. 200, 100, 50, 20). Default: 200"),
        ma_type: z.enum(["SMA", "EMA"]).optional().default("SMA").describe("Moving average type: 'SMA' or 'EMA'. Default: 'SMA'"),
        lookback_days: z.number().optional().default(5).describe("Number of recent trading days to return (e.g. 5, 30). Default: 5"),
        tickers: z.array(z.string()).optional().describe("Optional list of tickers. If omitted, scans entire universe."),
        timeframe: z.string().optional().default("1D").describe("Candle timeframe (Default: '1D')"),
      },
    },
    async ({ period, ma_type, lookback_days, tickers, timeframe }: any) => {
      try {
        const payload: Record<string, any> = {
          period: period ?? 200,
          ma_type: ma_type || "SMA",
          lookback_days: lookback_days ?? 5,
          timeframe: timeframe || "1D",
        };
        if (tickers && Array.isArray(tickers) && tickers.length > 0) {
          payload.tickers = tickers.map((t: string) => t.toUpperCase());
        }

        const res = await fetch(`${PCA_SERVICE_URL}/api/indicators/breadth`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

        if (!res.ok) {
          const errText = (await res.text()).slice(0, 300);
          throw new Error(`HTTP ${res.status}: ${errText}`);
        }

        const data = await res.json();
        const rows = data.series || [];

        const tableHeader = "| Datum | Marktbreite (%) | Aktien > MA | Universum |\n| :--- | :---: | :---: | :---: |";
        const tableRows = rows.map((r: any) => `| ${r.date} | **${r.percentage.toFixed(2)}%** | ${r.count} | ${r.total} |`).join("\n");
        const summaryText = `📊 **Marktbreite (${data.ma_type} ${data.period})**\nScanned ${data.total_universe} Tickers in ${data.elapsed_seconds}s\n\n${tableHeader}\n${tableRows}\n\n\`\`\`json\n${JSON.stringify(data, null, 2)}\n\`\`\``;

        return {
          content: [{ type: "text", text: summaryText }],
        };
      } catch (err: any) {
        log.error(`get_market_breadth error: ${err.message}`);
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  // ─────────────────────────────────────────────────────────────────────────────
  // 6. list_scanner_fields — Dynamic Schema Introspection & Help
  // ─────────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "list_scanner_fields",
    {
      title: "List Universal Scanner Fields & Schema",
      description:
        "Returns the live dynamic catalog of all available technical, fundamental, and computed fields for `run_universal_scanner`.\n\n" +
        "Minimalist output: Field name and expanded abbreviation or single word.",
      inputSchema: {},
    },
    async () => {
      try {
        const res = await fetch(`${PCA_SERVICE_URL}/api/scanner/universal/fields`);
        if (!res.ok) {
          const errText = (await res.text()).slice(0, 300);
          throw new Error(`HTTP ${res.status}: ${errText}`);
        }
        const data = await res.json();
        const fields: Array<{ name: string; description: string; type: string; source: string }> = data.fields || [];

        const lines = fields.map(f => `• \`${f.name}\`${f.description ? `: ${f.description}` : ""}`);
        const text = `📋 **Verfügbare Scanner-Felder (${fields.length}):**\n\n${lines.join("\n")}\n\n\`\`\`json\n${JSON.stringify(data, null, 2)}\n\`\`\``;

        return { content: [{ type: "text", text }] };
      } catch (err: any) {
        log.error(`list_scanner_fields error: ${err.message}`);
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  // ─────────────────────────────────────────────────────────────────────────────
  // 7. run_universal_scanner — Dynamic Multi-Factor Stock Screener
  // ─────────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "run_universal_scanner",
    {
      title: "Run Universal Multi-Factor Stock Scanner",
      description:
        "Universal multi-factor stock scanner combining Parquet OHLCV/indicators and Supabase fundamentals (`cda_master_universe`).\n\n" +
        "ZERO HARDCODED FILTERS: Fully dynamic filtering across ANY field (price, IBD RS, DCR, WCR, ADR20, moving averages, market cap, EPS, revenue, volume).\n\n" +
        "HOW TO USE:\n" +
        "- To see all available fields: call `list_scanner_fields` or pass `{ help: true }`.\n" +
        "- Pass structured `filters: [{ field: 'ibd_rs', op: '>=', value: 90 }, { field: 'market_cap', op: '>=', value: 1e9 }]`.\n" +
        "- Or pass raw `expression: 'ibd_rs >= 90 AND market_cap >= 1e9 AND close > ma_sma_50'`.\n" +
        "- Cross-column comparisons: `{ field: 'close', op: '>', field_compare: 'ma_sma_50' }`.\n" +
        "- Omit tickers/watchlists to screen the entire database universe.\n\n" +
        "AUTO-WATCHLIST: Jeder Scan-Lauf schreibt ALLE Treffer (ohne Cap) in die rollierende Supabase-Watchlist `scan_latest` (Replace-Semantik: der vorherige Stand wird ersetzt). Die Response selbst bleibt auf `limit` gekappt – die vollständige Trefferzahl steht in `watchlist.matched_total`, `watchlist.truncated` zeigt an, ob gekappt wurde.\n" +
        "- Inhalt lesen: `manage_watchlist` (LOAD, list_name='scan_latest').\n" +
        "- Im Chart Viewer öffnen/aktualisieren: `manage_chart_viewer` (DISPLAY_WATCHLIST, list_name='scan_latest') – jeder Aufruf liest frisch aus Supabase.\n" +
        "- Bei `watchlist.count = 0` ist die Liste leer (Scan ohne Treffer); dann kein Öffnen anbieten.",
      inputSchema: {
        filters: z
          .array(
            z.object({
              field: z.string().describe("Field name to filter on (e.g. 'ibd_rs', 'market_cap', 'dcr', 'close')"),
              op: z.string().describe("Operator: '>=', '<=', '>', '<', '==', '=', '!=', 'in', 'not_in', 'between', 'is_null', 'is_not_null'"),
              value: z.any().optional().describe("Comparison value or array of values for 'in' / 'between'"),
              field_compare: z.string().optional().describe("Optional other field name to compare against (e.g. 'ma_sma_50')"),
            })
          )
          .optional()
          .describe("Array of structured filter conditions"),
        expression: z
          .string()
          .optional()
          .describe("DuckDB SQL filter expression string (e.g. 'ibd_rs >= 90 AND market_cap >= 1e9 AND close > ma_sma_50')"),
        sort_by: z.string().optional().default("dcr").describe("Field name to sort results by (Default: 'dcr')"),
        sort_direction: z.enum(["desc", "asc"]).optional().default("desc").describe("Sort direction: 'desc' or 'asc' (Default: 'desc')"),
        limit: z.number().optional().describe("Max matching results to return (Default: 50, Max: 1000)"),
        tickers: z.array(z.string()).optional().describe("Optional list of tickers to scan. If omitted, scans entire universe."),
        watchlists: z.array(z.string()).optional().describe("Optional watchlist names or URLs to scan."),
        help: z.boolean().optional().default(false).describe("If true, returns the dynamic field catalog without scanning."),
      },
    },
    async ({ filters, expression, sort_by, sort_direction, limit, tickers, watchlists, help }: any) => {
      try {
        const payload: Record<string, any> = {
          sort_by: sort_by || "dcr",
          sort_direction: sort_direction || "desc",
          help: Boolean(help),
        };
        if (limit !== undefined && limit !== null) payload.limit = limit;
        if (filters && Array.isArray(filters) && filters.length > 0) payload.filters = filters;
        if (expression && typeof expression === "string" && expression.trim()) payload.expression = expression.trim();
        if (tickers && Array.isArray(tickers) && tickers.length > 0) payload.tickers = tickers;
        if (watchlists && Array.isArray(watchlists) && watchlists.length > 0) payload.watchlists = watchlists;

        const res = await fetch(`${PCA_SERVICE_URL}/api/scanner/universal`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

        if (!res.ok) {
          const errText = (await res.text()).slice(0, 400);
          throw new Error(`HTTP ${res.status}: ${errText}`);
        }

        const data = await res.json();
        if (data.status === "help" && data.fields) {
          const fields: Array<{ name: string; description: string }> = data.fields;
          const lines = fields.map(f => `• \`${f.name}\`${f.description ? `: ${f.description}` : ""}`);
          return {
            content: [{
              type: "text",
              text: `📋 **Verfügbare Scanner-Felder (${fields.length}):**\n\n${lines.join("\n")}\n\n\`\`\`json\n${JSON.stringify(data, null, 2)}\n\`\`\``
            }]
          };
        }

        const records: any[] = data.records || [];
        const matchedTickers: string[] = data.matched_tickers || [];
        const applied: string[] = data.applied_filters || [];

        // Format numbers cleanly, honoring the instrument currency where available.
        const currencySymbol = (c: any) => {
          const cur = String(c || "USD").trim().toUpperCase();
          if (cur === "USD") return "$";
          if (cur === "EUR") return "€";
          if (cur === "GBP") return "£";
          if (cur === "JPY") return "¥";
          return cur ? `${cur} ` : "$";
        };
        const fmtCap = (val: any, currency: any) => {
          if (val === null || val === undefined) return "-";
          const num = Number(val);
          const s = currencySymbol(currency);
          if (num >= 1e12) return `${s}${(num / 1e12).toFixed(2)}T`;
          if (num >= 1e9) return `${s}${(num / 1e9).toFixed(2)}B`;
          if (num >= 1e6) return `${s}${(num / 1e6).toFixed(1)}M`;
          return `${s}${num.toLocaleString()}`;
        };

        const fmtPct = (val: any) => (val !== null && val !== undefined ? `${Number(val).toFixed(1)}%` : "-");
        const fmtMoney = (val: any, currency: any) => (val !== null && val !== undefined ? `${currencySymbol(currency)}${Number(val).toFixed(2)}` : "-");

        const tableHeader = "| Ticker | Kurs | Market Cap | 50d Vol | IBD RS | DCR % | WCR % | ADR20 % | EPS | Earnings |\n" +
                            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |";

        const tableRows = records.map(r => {
          const t = r.ticker || "-";
          const cur = r.currency;
          const px = fmtMoney(r.close, cur);
          const cap = fmtCap(r.market_cap, cur);
          const dvol = fmtCap(r.ma_sma_50_dollar_volume, cur);
          const rs = r.ibd_rs !== null && r.ibd_rs !== undefined ? `**${r.ibd_rs}**` : "-";
          const dcr = fmtPct(r.dcr);
          const wcr = fmtPct(r.wcr);
          const adr = fmtPct(r.adr_20);
          const eps = fmtMoney(r.eps, cur);
          const dte = r.days_to_earnings;
          const earn = dte !== null && dte !== undefined
            ? (dte > 0 ? `in ${dte}d` : (dte < 0 ? `vor ${-dte}d` : "heute"))
            : (r.earnings ? String(r.earnings).slice(0, 10) : "-");

          return `| **${t}** | ${px} | ${cap} | ${dvol} | ${rs} | ${dcr} | ${wcr} | ${adr} | ${eps} | ${earn} |`;
        }).join("\n");

        const appliedText = applied.length > 0 ? `\n**Filter:** ${applied.join("  •  ")}` : "\n**Filter:** _Keine (ungefiltertes Universum)_";
        const perf = data.performance;
        const perfText = perf ? `\n⏱️ **Performance:** ${perf.summary}` : "";

        // Auto-watchlist state: every scan run replaces `scan_latest` with the COMPLETE result set.
        const wl = data.watchlist || null;
        let wlText = "";
        if (wl) {
          const stamp = wl.updated_at ? `${String(wl.updated_at).slice(11, 19)} UTC` : "n/a";
          if (wl.status === "updated") {
            wlText = `\n📌 **Auto-Watchlist \`${wl.list_name}\`:** ${wl.count} Ticker (alle Treffer, aktualisiert ${stamp})` +
              ` — öffnen/aktualisieren mit \`manage_chart_viewer\` (DISPLAY_WATCHLIST, list_name='${wl.list_name}')`;
          } else if (wl.status === "cleared") {
            wlText = `\n⚠️ **Auto-Watchlist \`${wl.list_name}\` ist jetzt LEER** (0 Treffer)${wl.message ? ` — ${wl.message}` : ""}`;
          } else {
            wlText = `\n⚠️ **Auto-Watchlist \`${wl.list_name}\`:** Status \`${wl.status}\`${wl.message ? ` — ${wl.message}` : ""}`;
          }
          if (wl.truncated) {
            wlText += `\n↳ Response zeigt nur die Top-${wl.response_count} von ${wl.matched_total} Treffern – die vollständige Liste steht in \`${wl.list_name}\`.`;
          }
        }

        const summaryText =
          `🔍 **Universal Stock Scanner** | ${data.count} Matches (von ${data.total_evaluated} evaluiert) | Sort: \`${payload.sort_by}\` (${payload.sort_direction})${appliedText}${perfText}${wlText}\n\n` +
          `${tableHeader}\n${tableRows}\n\n` +
          (matchedTickers.length > 0
            ? `📋 **Matching Tickers (${matchedTickers.length}):** ${matchedTickers.map(t => `\`${t}\``).join(", ")}\n\n`
            : "*(Keine Ticker haben die Kriterien erfüllt)*\n\n") +
          `\`\`\`json\n${JSON.stringify({ count: data.count, total_evaluated: data.total_evaluated, matched_tickers: matchedTickers, applied_filters: applied, performance: data.performance, watchlist: wl }, null, 2)}\n\`\`\``;

        return { content: [{ type: "text", text: summaryText }] };
      } catch (err: any) {
        log.error(`run_universal_scanner error: ${err.message}`);
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

}

