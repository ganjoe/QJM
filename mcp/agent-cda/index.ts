import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPTransport } from "@hono/mcp";
import { Hono } from "hono";
import { AGENT_ID, MCP_ACCESS_KEY, STOCK_DATA_NODE_URL, log } from "./tools/shared.ts";
import { registerCdaTools } from "./tools/cda.ts";

// --- MCP Server Setup ---
const server = new McpServer({
  name: "openbrain-cda",
  version: "2.0.0",
});

registerCdaTools(server);

// --- Hono App ---
const app = new Hono();

// ── 1. Health Endpoint ───────────────────────────────────────────────
app.get("/health", (c) => {
  return c.json({ status: "healthy", server: "openbrain-cda", version: "2.0.0" });
});

// ── 2. Direct REST API Endpoints (für zukünftige Dashboards / UIs) ────
app.get("/api/status", async (c) => {
  try {
    const [statusRes, healthRes, connRes] = await Promise.allSettled([
      fetch(`${STOCK_DATA_NODE_URL}/status`).then((r) => r.json()),
      fetch(`${STOCK_DATA_NODE_URL}/health`).then((r) => r.json()),
      fetch(`${STOCK_DATA_NODE_URL}/status/connection`).then((r) => r.json()),
    ]);
    return c.json({
      status: "ok",
      server: "openbrain-cda",
      stock_data_node: {
        queue: statusRes.status === "fulfilled" ? statusRes.value : null,
        health: healthRes.status === "fulfilled" ? healthRes.value : null,
        connection: connRes.status === "fulfilled" ? connRes.value : null,
      },
    });
  } catch (err: any) {
    return c.json({ error: err.message }, 500);
  }
});

app.get("/api/staleness", async (c) => {
  try {
    const res = await fetch(`${STOCK_DATA_NODE_URL}/staleness/report`);
    const data = await res.json();
    return c.json(data, res.status as any);
  } catch (err: any) {
    return c.json({ error: err.message }, 500);
  }
});

app.post("/api/sweep", async (c) => {
  try {
    const res = await fetch(`${STOCK_DATA_NODE_URL}/trigger-staleness`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json();
    return c.json(data, res.status as any);
  } catch (err: any) {
    return c.json({ error: err.message }, 500);
  }
});

app.get("/api/data/status/:ticker", async (c) => {
  const ticker = c.req.param("ticker").toUpperCase();
  try {
    const res = await fetch(`${STOCK_DATA_NODE_URL}/data/status/${ticker}`);
    const data = await res.json();
    return c.json(data, res.status as any);
  } catch (err: any) {
    return c.json({ error: err.message }, 500);
  }
});

app.get("/api/fallback/:ticker", async (c) => {
  const ticker = c.req.param("ticker").toUpperCase();
  try {
    const res = await fetch(`${STOCK_DATA_NODE_URL}/fallback/check/${ticker}`);
    const data = await res.json();
    return c.json(data, res.status as any);
  } catch (err: any) {
    return c.json({ error: err.message }, 500);
  }
});

app.get("/api/mapping/:ticker", async (c) => {
  const ticker = c.req.param("ticker").toUpperCase();
  try {
    const res = await fetch(`${STOCK_DATA_NODE_URL}/mapping/${ticker}`);
    const data = await res.json();
    return c.json(data, res.status as any);
  } catch (err: any) {
    return c.json({ error: err.message }, 500);
  }
});

app.post("/api/download", async (c) => {
  try {
    const body = await c.req.json();
    const res = await fetch(`${STOCK_DATA_NODE_URL}/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    return c.json(data, res.status as any);
  } catch (err: any) {
    return c.json({ error: err.message }, 500);
  }
});

// ── 3. MCP Streamable-HTTP Handler ───────────────────────────────────
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
