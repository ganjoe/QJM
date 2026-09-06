import { McpServer } from "npm:@modelcontextprotocol/sdk/server/mcp.js";
import { Server } from "npm:@modelcontextprotocol/sdk/server/index.js";
import { z } from "npm:zod";

const server = new McpServer({
  name: "openbrain-pca",
  version: "2.0.0",
});

server.registerTool(
  "manage_chart_viewer",
  {
    title: "Control Desktop Chart Viewer",
    description: "Controls the TC2000-style native desktop chart viewer running on the user's screen.",
    inputSchema: {
      action: z.enum(["DISPLAY_STOCK", "DISPLAY_WATCHLIST"]).describe("The action to perform"),
      ticker: z.string().optional().describe("Stock ticker symbol (or comma-separated symbols for DISPLAY_WATCHLIST)"),
      list_name: z.string().optional().describe("Supabase list name for DISPLAY_WATCHLIST (e.g. 'current_positions')"),
    },
  },
  async (args: any) => { return { content: [] }; }
);

async function test() {
  const req = { method: "tools/list", params: {} };
  const s = (server as any).server as Server;
  const handler = (s as any)._requestHandlers["tools/list"];
  const res = await handler(req, {
    sendLoggingMessage: () => {},
    sendResourceUpdated: () => {},
    sendResourceListChanged: () => {},
    sendToolListChanged: () => {},
    sendPromptListChanged: () => {},
  });
  console.log(JSON.stringify(res, null, 2));
}

test().catch(console.error);
