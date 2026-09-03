import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { STOCK_DATA_NODE_URL, log } from "./shared.ts";

export function registerDataTools(server: McpServer) {

  // ── add_ticker ──────────────────────────────────────────────────
  server.registerTool(
    "add_ticker",
    {
      title: "Add Ticker",
      description:
        "Adds one or more stock tickers to the local database and queues them for priority download. " +
        "Automatically resolves unknown ticker symbols across data providers according to provider ranking " +
        "(e.g. IBKR -> YFinance fallback, resolving '4GLD' to '4GLD.DE'). " +
        "The first ticker in the list receives highest download priority. " +
        "Returns 'OK' for direct matches, the resolved symbol and provider if an alias was required, " +
        "or a candidate list if ambiguous.",
      inputSchema: {
        tickers: z.array(z.string()).min(1).describe(
          "List of ticker symbols to add (e.g. ['AAPL'] or ['CBRS', 'HL', 'SPCX', 'TYC1', 'SIVE'])"
        ),
        provider: z.string().optional().describe(
          "Optional: Force a specific provider (e.g. 'IBKR' or 'YFINANCE'). Omit to use automatic provider ranking."
        ),
        override_symbol: z.string().optional().describe(
          "Optional: Explicit provider symbol (e.g. '4GLD.DE'). Only valid when adding a single ticker."
        ),
        sec_type: z.enum(["STK", "ETF", "OPT", "FUT"]).default("STK").optional().describe(
          "Security type to search for (default: 'STK')."
        ),
      },
    },
    async ({ tickers, provider, override_symbol, sec_type }: any) => {
      try {
        const cleanTickers = tickers.map((t: string) => t.trim().toUpperCase()).filter(Boolean);
        if (cleanTickers.length === 0) {
          return {
            content: [{ type: "text", text: "Error: No valid ticker provided." }],
            isError: true,
          };
        }

        if (override_symbol && cleanTickers.length > 1) {
          return {
            content: [{ type: "text", text: "Error: override_symbol can only be used with a single ticker." }],
            isError: true,
          };
        }

        log.info(`[add_ticker] Requesting add for: ${cleanTickers.join(", ")} (sec_type=${sec_type || "STK"})`);
        const payload: Record<string, any> = {
          tickers: cleanTickers,
          sec_type: sec_type || "STK",
        };
        if (provider) payload.provider = provider.toUpperCase();
        if (override_symbol) payload.override_symbol = override_symbol.trim();

        const res = await fetch(`${STOCK_DATA_NODE_URL}/add`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

        if (!res.ok) {
          const errText = await res.text();
          return {
            content: [{ type: "text", text: `Backend error (HTTP ${res.status}): ${errText}` }],
            isError: true,
          };
        }

        const data = await res.json();
        const results: any[] = data.results || [];

        // Build concise, agent-friendly summary
        const lines: string[] = [];
        let hasAmbiguous = false;
        let hasErrors = false;

        for (const item of results) {
          if (item.status === "ok") {
            lines.push(`✅ ${item.ticker}: OK (${item.provider || "IBKR"}) — Priority 1 download queued`);
          } else if (item.status === "resolved") {
            lines.push(`ℹ️ ${item.ticker}: Resolved to '${item.resolved_symbol}' via ${item.provider} (${item.sec_type || "STK"}) — Download queued`);
          } else if (item.status === "ambiguous") {
            hasAmbiguous = true;
            const candidatesStr = (item.candidates || [])
              .map((c: any, idx: number) => `   ${idx + 1}. Symbol: ${c.symbol} | Name: ${c.name} | Type: ${c.sec_type} | Exch: ${c.exchange}`)
              .join("\n");
            lines.push(`⚠️ ${item.ticker}: Ambiguous ticker — multiple matches found:\n${candidatesStr}\n   -> Please call add_ticker again with override_symbol to specify.`);
          } else {
            hasErrors = true;
            lines.push(`❌ ${item.ticker}: Failed (${item.error || "Could not resolve on any provider"})`);
          }
        }

        return {
          content: [{ type: "text", text: lines.join("\n") }],
          isError: hasErrors && !lines.some(l => l.startsWith("✅") || l.startsWith("ℹ️")),
        };
      } catch (err: any) {
        log.error(`add_ticker failed: ${err.message}`);
        return {
          content: [{ type: "text", text: `Internal Error connecting to stock-data-node: ${err.message}` }],
          isError: true,
        };
      }
    }
  );

  // ── override_ticker_mapping ──────────────────────────────────────────
  server.registerTool(
    "override_ticker_mapping",
    {
      title: "Override Ticker Mapping",
      description: "Directly override or correct the provider and symbol mapping for a ticker without immediately adding new download tasks. " +
        "Example: ticker='4GLD', provider='YFINANCE', symbol='4GLD.DE'.",
      inputSchema: {
        ticker: z.string().describe("The base ticker symbol (e.g. '4GLD')"),
        provider: z.string().describe("The provider to override (e.g. 'YFINANCE' or 'IBKR')"),
        symbol: z.string().describe("The exact symbol string expected by the provider (e.g. '4GLD.DE')"),
      },
    },
    async ({ ticker, provider, symbol }: any) => {
      try {
        log.info(`Overriding mapping for ${ticker} on ${provider} to ${symbol}...`);
        const res = await fetch(`${STOCK_DATA_NODE_URL}/mapping/${encodeURIComponent(ticker)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider, symbol }),
        });
        
        if (!res.ok) {
          return {
            content: [{ type: "text", text: `Error: API returned status ${res.status}` }],
            isError: true,
          };
        }

        const data = await res.json();
        return {
          content: [
            {
              type: "text",
              text: `Successfully overridden mapping for ${ticker}.\nNew Provider Symbols: ${JSON.stringify(data.provider_symbols, null, 2)}`,
            },
          ],
        };
      } catch (err: any) {
        log.error(`override_ticker_mapping failed: ${err.message}`);
        return {
          content: [{ type: "text", text: `Internal Error: ${err.message}` }],
          isError: true,
        };
      }
    }
  );

}
