import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPTransport } from "@hono/mcp";
import { Hono } from "hono";
import { registerDrawioTools } from "./tools/drawio.ts";
import { BASE, ensureBase } from "./tools/storage.ts";

const MCP_ACCESS_KEY = Deno.env.get("MCP_ACCESS_KEY") || "";
const PORT = parseInt(Deno.env.get("PORT") || "8796");

ensureBase();

const server = new McpServer({
  name: "openbrain-drawio",
  version: "2.0.0",
});

registerDrawioTools(server);

const app = new Hono();

app.get("/health", (c) => {
  return c.json({ status: "healthy", server: "openbrain-drawio", version: "2.0.0", base: BASE });
});

app.all("*", async (c) => {
  const provided = c.req.header("x-brain-key") || new URL(c.req.url).searchParams.get("key");
  if (MCP_ACCESS_KEY && provided !== MCP_ACCESS_KEY) {
    return c.json({ error: "Invalid MCP access key" }, 401);
  }
  const transport = new StreamableHTTPTransport();
  await server.connect(transport);
  return transport.handleRequest(c);
});

console.log("draw.io MCP Server (QJM) startet auf Port " + PORT + ", Ablage: " + BASE);
Deno.serve({ port: PORT }, app.fetch);
