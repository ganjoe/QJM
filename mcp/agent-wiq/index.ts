import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPTransport } from "@hono/mcp";
import { Hono } from "hono";
import { AGENT_ID, MCP_ACCESS_KEY, log } from "./tools/shared.ts";
import { registerScheduleTools } from "./tools/schedules.ts";
import { registerWorkitemTools } from "./tools/workitems.ts";
import { registerRunTools } from "./tools/runs.ts";

// --- MCP Server Setup ---
const server = new McpServer({
  name: "openbrain-wiq",
  version: "1.0.0",
});

registerWorkitemTools(server);
registerRunTools(server);
registerScheduleTools(server);

// --- Hono App ---
const app = new Hono();

app.get("/health", (c) => {
  return c.json({ status: "healthy", server: "openbrain-wiq", version: "1.0.0" });
});

// MCP Endpoint mit Key-Schutz (Muster: mcp/agent-pca/index.ts)
app.all("*", async (c) => {
  const provided = c.req.header("x-brain-key") || new URL(c.req.url).searchParams.get("key");
  if (!provided || (MCP_ACCESS_KEY && provided !== MCP_ACCESS_KEY)) {
    return c.json({ error: "Invalid key" }, 401);
  }
  const transport = new StreamableHTTPTransport();
  await server.connect(transport);
  return transport.handleRequest(c);
});

const port = parseInt(Deno.env.get("PORT") || "8798");
log.info(AGENT_ID.toUpperCase() + " MCP server starting on port " + port + "...");
Deno.serve({ port }, app.fetch);
