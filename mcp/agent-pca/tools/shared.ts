import { createClient, SupabaseClient } from "@supabase/supabase-js";

// --- Configuration from environment ---
export const SUPABASE_URL              = Deno.env.get("SUPABASE_URL") || "http://host.docker.internal:8001";
export const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
export const MCP_ACCESS_KEY            = Deno.env.get("MCP_ACCESS_KEY") || "";
export const AGENT_ID                  = Deno.env.get("AGENT_ID") || "pca";

// Features service URL (FastAPI)
export const FEATURES_SERVICE_URL     = Deno.env.get("FEATURES_SERVICE_URL") || "http://host.docker.internal:8003";

// PCA desktop service base URL (FastAPI backend)
export const PCA_SERVICE_URL           = Deno.env.get("PCA_SERVICE_URL") || "http://host.docker.internal:8791";

// Stock Data Node URL
export const STOCK_DATA_NODE_URL       = Deno.env.get("STOCK_DATA_NODE_URL") || "http://host.docker.internal:8002";

export const supabase: SupabaseClient = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

// --- Logger Helper ---
export const log = {
  info: (msg: string) => console.log(`[INFO] ${new Date().toISOString()} ${msg}`),
  warn: (msg: string) => console.warn(`[WARN] ${new Date().toISOString()} ${msg}`),
  error: (msg: string) => console.error(`[ERROR] ${new Date().toISOString()} ${msg}`),
};

// --- Telemetry Helper (no-op, Nexus service discontinued) ---
export async function sendTelemetry(_text: string) {
  // Legacy: was sending to nexus-service:7734. Now a no-op.
}
