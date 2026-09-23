// @ts-nocheck
declare const Deno: any;
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPTransport } from "@hono/mcp";
import { Hono } from "hono";
import { registerShellyTools } from "./tools/shelly.ts";

// 8797 ist auf diesem Host bereits belegt (web-scraper) -> freier Default-Port.
const PORT = parseInt(Deno.env.get("SHELLEY_MCP_PORT") || "8799");
const MCP_ACCESS_KEY = Deno.env.get("MCP_ACCESS_KEY") || "";

const server = new McpServer({
  name: "openbrain-shelly",
  version: "2.0.0",
});

registerShellyTools(server);

const app = new Hono();

app.get("/health", (c) => {
  return c.json({ status: "healthy", server: "openbrain-shelly", version: "2.0.0" });
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

console.log(`Shelly MCP Server (QJM) startet auf Port ${PORT}`);
Deno.serve({ port: PORT }, app.fetch);
