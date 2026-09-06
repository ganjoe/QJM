import { McpServer } from "npm:@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPTransport } from "npm:@hono/mcp";
import { Hono } from "npm:hono";
import { z } from "npm:zod";

const server = new McpServer({ name: "openbrain-pca", version: "2.0.0" });

server.registerTool("manage_chart_viewer", "test desc", {
  action: z.enum(["DISPLAY_STOCK"]),
}, async () => ({ content: [] }));

const app = new Hono();
app.all("*", async (c) => {
  const transport = new StreamableHTTPTransport();
  await server.connect(transport);
  return transport.handleRequest(c);
});

Deno.serve({ port: 9999 }, app.fetch);
