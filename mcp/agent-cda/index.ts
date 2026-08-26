import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPTransport } from "@hono/mcp";
import { Hono } from "hono";
import { AGENT_ID, MCP_ACCESS_KEY, log } from "./tools/shared.ts";
import { registerCdaTools } from "./tools/cda.ts";

// --- MCP Server Setup ---
const server = new McpServer({
  name: "openbrain-cda",
  version: "2.0.0",
});

registerCdaTools(server);

// --- Hono App ---
const app = new Hono();

app.get("/health", (c) => {
  return c.json({ status: "healthy", server: "openbrain-cda", version: "2.0.0" });
});

app.all("*", async (c) => {
  const provided = c.req.header("x-brain-key") || new URL(c.req.url).searchParams.get("key");
  if (!provided || (MCP_ACCESS_KEY && provided !== MCP_ACCESS_KEY)) {
    return c.json({ error: "Invalid key" }, 401);
  }
  const transport = new StreamableHTTPTransport();
  await server.connect(transport);
  return transport.handleRequest(c);
});

const port = parseInt(Deno.env.get("PORT") || "8795");
log.info(`${AGENT_ID.toUpperCase()} MCP server starting on port ${port}...`);
Deno.serve({ port }, app.fetch);
