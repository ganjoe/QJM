import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { supabase, pcaCommand, FEATURES_SERVICE_URL, log } from "./shared.ts";

export function registerPcaTools(server: McpServer) {

  // ── manage_chart_view ─────────────────────────────────────────────
  server.registerTool(
    "manage_chart_view",
    {
      title: "Manage Chart View",
      description: "Manage layouts, load tickers, and navigate watchlists.",
      inputSchema: {
        action: z.enum(["OPEN_LAYOUT", "LIST_LAYOUTS", "LOAD_TICKER", "NEXT_TICKER", "PREV_TICKER"]).describe("The action to perform"),
        layout: z.string().optional().describe("Layout name, e.g. 'desktop' (for OPEN_LAYOUT)"),
        symbol: z.string().optional().describe("Ticker symbol (for LOAD_TICKER)"),
        list_name: z.string().optional().describe("Watchlist name (for NEXT/PREV)"),
        current_ticker: z.string().optional().describe("Currently displayed ticker (for NEXT/PREV)"),
      },
    },
    async ({ action, layout, symbol, list_name, current_ticker }: any) => {
      try {
        if (action === "OPEN_LAYOUT") {
          if (!layout) throw new Error("layout required for OPEN_LAYOUT");
          const result = await pcaCommand("open_layout", { layout });
          return { content: [{ type: "text", text: `Layout '${layout}' opened. ${result}` }] };
        } else if (action === "LIST_LAYOUTS") {
          const { data, error } = await supabase.from("pca_layouts").select("name, description, is_default").order("name");
          if (error) throw error;
          const lines = data.map((l: any) => `• ${l.name}${l.is_default ? " [default]" : ""}: ${l.description ?? "—"}`);
          return { content: [{ type: "text", text: lines.join("\n") || "No layouts found." }] };
        } else if (action === "LOAD_TICKER") {
          if (!symbol) throw new Error("symbol required for LOAD_TICKER");
          const result = await pcaCommand("load_ticker", { symbol: symbol.toUpperCase() });
          return { content: [{ type: "text", text: `Ticker ${symbol.toUpperCase()} loaded. ${result}` }] };
        } else if (action === "NEXT_TICKER" || action === "PREV_TICKER") {
          if (!current_ticker) throw new Error("current_ticker required");
          return _navigateWatchlist(list_name ?? "growth_stocks", current_ticker, action === "NEXT_TICKER" ? 1 : -1);
        }
        throw new Error("Invalid action");
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  // ── manage_watchlist ──────────────────────────────────────────
  server.registerTool(
    "manage_watchlist",
    {
      title: "Manage Watchlist",
      description: "List, load, add to, remove from, create, delete, clear, or rename watchlists.",
      inputSchema: {
        action: z.enum(["LIST", "LOAD", "ADD", "REMOVE", "CLUSTER", "DELETE", "CLEAR", "CREATE", "RENAME"]).describe("The action to perform"),
        list_name: z.string().optional().describe("Watchlist name"),
        list_names: z.array(z.string()).optional().describe("Array of watchlist names (for bulk DELETE)"),
        pattern: z.string().optional().describe("Wildcard/LIKE pattern to match watchlist names (e.g. 'cluster_*', 'Photonics_*')"),
        new_list_name: z.string().optional().describe("New name for watchlist (for RENAME)"),
        ticker: z.string().optional().describe("Ticker symbol (for ADD, REMOVE)"),
        tickers: z.array(z.string()).optional().describe("Array of tickers (for CREATE or bulk ADD)"),
        position: z.number().optional().describe("Position in list (for ADD)"),
        layout_name: z.string().optional().describe("Layout to update (for LOAD)"),
        source_watchlist: z.string().optional().describe("Source watchlist for CLUSTER (default: all tickers)"),
        lookback_days: z.number().optional().describe("Trading days for correlation (default: 63, ~3 months)"),
        num_clusters: z.number().optional().describe("Number of cluster groups (default: 10)"),
      },
    },
    async ({ action, list_name, list_names, pattern, new_list_name, ticker, tickers, position, layout_name, source_watchlist, lookback_days, num_clusters }: any) => {
      try {
        if (action === "LIST") {
          if (list_name) {
            const { data, error } = await supabase.from("pca_watchlists").select("ticker, position").eq("list_name", list_name).order("position");
            if (error) throw error;
            const resultTickers = data.map((r: any) => r.ticker).join(", ");
            return { content: [{ type: "text", text: `Watchlist '${list_name}': ${resultTickers}` }] };
          } else {
            const { data, error } = await supabase.from("pca_watchlists").select("list_name").order("list_name");
            if (error) throw error;
            const names = [...new Set(data.map((r: any) => r.list_name))].join(", ");
            return { content: [{ type: "text", text: `Available watchlists: ${names}` }] };
          }
        } else if (action === "LOAD") {
          if (!list_name) throw new Error("list_name required for LOAD");
          const result = await pcaCommand("load_watchlist", { list_name, layout_name: layout_name ?? "desktop" });
          return { content: [{ type: "text", text: `Watchlist '${list_name}' loaded and persisted in layout. ${result}` }] };
        } else if (action === "ADD") {
          if (!list_name) throw new Error("list_name required for ADD");
          if (tickers && tickers.length > 0) {
            const rows = tickers.map((t: string, idx: number) => ({
              list_name,
              ticker: t.toUpperCase(),
              position: (position ?? 0) + idx
            }));
            const { error } = await supabase.from("pca_watchlists").upsert(rows, { onConflict: "list_name,ticker" });
            if (error) throw error;
            return { content: [{ type: "text", text: `${tickers.length} tickers added to '${list_name}'.` }] };
          } else if (ticker) {
            const { error } = await supabase.from("pca_watchlists").insert({ list_name, ticker: ticker.toUpperCase(), position: position ?? 999 });
            if (error) throw error;
            return { content: [{ type: "text", text: `Added ${ticker.toUpperCase()} to '${list_name}'.` }] };
          }
          throw new Error("ticker or tickers required for ADD");
        } else if (action === "REMOVE") {
          if (!list_name || !ticker) throw new Error("list_name and ticker required for REMOVE");
          const { error } = await supabase.from("pca_watchlists").delete().eq("list_name", list_name).eq("ticker", ticker.toUpperCase());
          if (error) throw error;
          return { content: [{ type: "text", text: `Removed ${ticker.toUpperCase()} from '${list_name}'.` }] };
        } else if (action === "CREATE") {
          if (!list_name) throw new Error("list_name required for CREATE");
          if (tickers && tickers.length > 0) {
            const rows = tickers.map((t: string, idx: number) => ({
              list_name,
              ticker: t.toUpperCase(),
              position: idx
            }));
            const { error } = await supabase.from("pca_watchlists").upsert(rows, { onConflict: "list_name,ticker" });
            if (error) throw error;
            return { content: [{ type: "text", text: `Watchlist '${list_name}' created with ${tickers.length} tickers.` }] };
          }
          return { content: [{ type: "text", text: `Watchlist '${list_name}' registered.` }] };
        } else if (action === "DELETE") {
          if (list_name) {
            const { error } = await supabase.from("pca_watchlists").delete().eq("list_name", list_name);
            if (error) throw error;
            return { content: [{ type: "text", text: `Watchlist '${list_name}' deleted.` }] };
          }
          throw new Error("list_name required for DELETE");
        }
        throw new Error("Invalid action");
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  // ── get_technical_indicator ───────────────────────────────────────
  server.registerTool(
    "get_technical_indicator",
    {
      title: "Get Technical Indicator",
      description: "Compute or retrieve on-the-fly technical indicators (Moving Averages, RS-Rating, Minervini Trend Template).",
      inputSchema: {
        ticker: z.string().describe("Ticker symbol (e.g. 'AAPL')"),
        indicator_type: z.enum(["MA", "RS", "MINERVINI"]).describe("Type of indicator"),
        chart_timeframe: z.string().optional().default("1D").describe("Timeframe (default: '1D')"),
        ma_type: z.enum(["SMA", "EMA"]).optional().default("SMA").describe("MA type (for MA)"),
        ma_window: z.number().optional().default(50).describe("MA window period (for MA)"),
        benchmark_ticker: z.string().optional().describe("Benchmark ticker for RS rating (optional)"),
      },
    },
    async ({ ticker, indicator_type, chart_timeframe, ma_type, ma_window, benchmark_ticker }: any) => {
      try {
        const sym = ticker.toUpperCase();
        if (indicator_type === "MA") {
          const res = await fetch(`${FEATURES_SERVICE_URL}/features/ma`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ticker: sym, chart_timeframe, ma_type, ma_window }),
          });
          if (!res.ok) throw new Error(await res.text());
          const data = await res.json();
          const lastVal = data.values && data.values.length > 0 ? data.values[data.values.length - 1] : null;
          const lastClose = data.close && data.close.length > 0 ? data.close[data.close.length - 1] : null;
          return {
            content: [{
              type: "text",
              text: `📈 **${sym} ${data.ma_label?.toUpperCase()}**: ${lastVal ? lastVal.toFixed(2) : "N/A"} (Close: ${lastClose ? lastClose.toFixed(2) : "N/A"}) [${data.data_points} bars]`,
            }],
          };
        } else if (indicator_type === "RS") {
          const res = await fetch(`${FEATURES_SERVICE_URL}/features/rs`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ticker: sym, chart_timeframe, benchmark_ticker }),
          });
          if (!res.ok) throw new Error(await res.text());
          const data = await res.json();
          return { content: [{ type: "text", text: `📊 **${sym} RS Rating**: ${JSON.stringify(data, null, 2)}` }] };
        } else if (indicator_type === "MINERVINI") {
          const res = await fetch(`${FEATURES_SERVICE_URL}/features/minervini`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ticker: sym, chart_timeframe }),
          });
          if (!res.ok) throw new Error(await res.text());
          const data = await res.json();
          return { content: [{ type: "text", text: `🏛️ **${sym} Minervini Template**: ${JSON.stringify(data, null, 2)}` }] };
        }
        throw new Error("Unsupported indicator type");
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );
}

// Helper for watchlist navigation
async function _navigateWatchlist(list_name: string, current_ticker: string, direction: number) {
  try {
    const { data, error } = await supabase
      .from("pca_watchlists")
      .select("ticker")
      .eq("list_name", list_name)
      .order("position");
    if (error) throw error;
    if (!data || data.length === 0) throw new Error(`Watchlist '${list_name}' is empty.`);

    const tickers = data.map((r: any) => r.ticker);
    const idx = tickers.indexOf(current_ticker.toUpperCase());
    const nextIdx = ((idx === -1 ? 0 : idx) + direction + tickers.length) % tickers.length;
    const nextTicker = tickers[nextIdx];

    await pcaCommand("load_ticker", { symbol: nextTicker });
    return { content: [{ type: "text", text: `Chart switched to ${nextTicker} (${nextIdx + 1}/${tickers.length}).` }] };
  } catch (err: any) {
    return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
  }
}
