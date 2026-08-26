// --- Configuration from Environment ---
export const MCP_ACCESS_KEY = Deno.env.get("MCP_ACCESS_KEY") || "";
export const AGENT_ID = Deno.env.get("AGENT_ID") || "cda";
export const STOCK_DATA_NODE_URL = Deno.env.get("STOCK_DATA_NODE_URL") || "http://stock-data-node:8002";

// --- Structured Logger ---
export const log = {
  info: (msg: string, ...args: any[]) => console.log(`[INFO] [${new Date().toISOString()}] ${msg}`, ...args),
  warn: (msg: string, ...args: any[]) => console.warn(`[WARN] [${new Date().toISOString()}] ${msg}`, ...args),
  error: (msg: string, ...args: any[]) => console.error(`[ERROR] [${new Date().toISOString()}] ${msg}`, ...args),
};
