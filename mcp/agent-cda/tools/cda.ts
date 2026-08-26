import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { STOCK_DATA_NODE_URL, log } from "./shared.ts";

export function registerCdaTools(server: McpServer) {

  server.registerTool(
    "get_staleness_report",
    {
      title: "Get Staleness Report",
      description: "Returns a distribution of how old the stock data in the system is.",
      inputSchema: {},
    },
    async () => {
      try {
        const res = await fetch(`${STOCK_DATA_NODE_URL}/staleness/report`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  server.registerTool(
    "get_queue_status",
    {
      title: "Get Queue Status",
      description: "Returns the current download queue size in the stock-data-node.",
      inputSchema: {},
    },
    async () => {
      try {
        const res = await fetch(`${STOCK_DATA_NODE_URL}/status`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  server.registerTool(
    "check_ticker_data",
    {
      title: "Check Ticker Data",
      description: "Checks if a parquet folder exists for a ticker, if it contains data, and the date of the last candle.",
      inputSchema: {
        ticker: z.string().describe("The ticker symbol to check"),
      },
    },
    async ({ ticker }: any) => {
      try {
        const res = await fetch(`${STOCK_DATA_NODE_URL}/data/status/${ticker.toUpperCase()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  server.registerTool(
    "request_priority_download",
    {
      title: "Request Priority Download",
      description: "Enqueues one or more tickers for immediate priority download via the stock-data-node REST API.",
      inputSchema: {
        tickers: z.array(z.string()).describe("List of ticker symbols to request (can be a single ticker or multiple)"),
      },
    },
    async ({ tickers }: any) => {
      try {
        const results: string[] = [];
        for (const ticker of tickers) {
          const res = await fetch(`${STOCK_DATA_NODE_URL}/download`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ticker: ticker.toUpperCase() }),
          });
          if (!res.ok) {
            results.push(`❌ ${ticker.toUpperCase()}: HTTP ${res.status}`);
          } else {
            const data = await res.json();
            results.push(`✅ ${ticker.toUpperCase()}: ${data.message || data.status || "queued"}`);
          }
        }
        log.info(`[CDA] Priority download requested for: ${tickers.join(", ")}`);
        return { content: [{ type: "text", text: results.join("\n") }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  server.registerTool(
    "set_data_provider",
    {
      title: "Set Data Provider",
      description: "Sets the data provider for a specific ticker ('YFINANCE' or 'IBKR'). Deletes existing chart data for consistency. Does NOT trigger a download — call request_priority_download afterwards.",
      inputSchema: {
        ticker: z.string().describe("The ticker symbol"),
        provider: z.enum(["IBKR", "YFINANCE"]).describe("The provider to use"),
      },
    },
    async ({ ticker, provider }: any) => {
      try {
        const res = await fetch(`${STOCK_DATA_NODE_URL}/config/provider/${ticker.toUpperCase()}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        return {
          content: [{
            type: "text",
            text: `Provider für ${ticker.toUpperCase()} auf ${provider} gesetzt. Alte Chartdaten wurden ${data.data_deleted ? "gelöscht" : "nicht gefunden"}.`,
          }],
        };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  server.registerTool(
    "check_yfinance_availability",
    {
      title: "Check YFinance Availability",
      description: "Check if a ticker is available in the fallback data provider (Yahoo Finance).",
      inputSchema: {
        ticker: z.string().describe("The ticker symbol to check"),
      },
    },
    async ({ ticker }: any) => {
      try {
        const res = await fetch(`${STOCK_DATA_NODE_URL}/fallback/check/${ticker.toUpperCase()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

}
