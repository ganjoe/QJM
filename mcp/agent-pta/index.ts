import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPTransport } from "@hono/mcp";
import { Hono } from "hono";
import { AGENT_ID, MCP_ACCESS_KEY, log } from "./tools/shared.ts";
import { registerPtaTools } from "./tools/pta.ts";
import { registerQuoteTools } from "./tools/get_quote.ts";
import { registerGatewayTools } from "./tools/gateway_tools.ts";
import { registerIbkrSyncTools } from "./tools/ibkr_sync_tools.ts";
import { registerResolveCompanyTools } from "./tools/resolve_company.ts";

// --- MCP Server Setup ---
const server = new McpServer({
  name: "openbrain-pta",
  version: "2.0.0",
});

// Register selected tools
registerPtaTools(server);
registerQuoteTools(server);
registerGatewayTools(server);
registerIbkrSyncTools(server);
registerResolveCompanyTools(server);

// --- Hono App ---
const app = new Hono();

import { resolveCompanyLogic } from "./tools/resolve_company.ts";

// Health check endpoint
app.get("/health", (c) => {
  return c.json({ status: "healthy", server: "openbrain-pta", version: "2.0.0" });
});

// Resolve company endpoint for CCO worker
app.get("/resolve", async (c) => {
  const company_name = c.req.query("company_name");
  if (!company_name) {
    return c.json({ error: "Missing company_name parameter" }, 400);
  }
  const result = await resolveCompanyLogic(company_name, true, false);
  return c.json(result);
});

// MCP Endpoint with key protection
app.all("*", async (c) => {
  const provided = c.req.header("x-brain-key") || new URL(c.req.url).searchParams.get("key");
  if (!provided || (MCP_ACCESS_KEY && provided !== MCP_ACCESS_KEY)) {
    return c.json({ error: "Invalid key" }, 401);
  }
  const transport = new StreamableHTTPTransport();
  await server.connect(transport);
  return transport.handleRequest(c);
});

const port = parseInt(Deno.env.get("PORT") || "8789");
log.info(`${AGENT_ID.toUpperCase()} MCP server starting on port ${port}...`);
Deno.serve({ port }, app.fetch);
