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

export const supabase: SupabaseClient = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

// --- Logger Helper ---
export const log = {
  info: (msg: string) => console.log(`[INFO] ${new Date().toISOString()} ${msg}`),
  warn: (msg: string) => console.warn(`[WARN] ${new Date().toISOString()} ${msg}`),
  error: (msg: string) => console.error(`[ERROR] ${new Date().toISOString()} ${msg}`),
};

// --- Telemetry Helper ---
export async function sendTelemetry(text: string) {
  try {
    await fetch("http://nexus-service:7734/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from_agent: AGENT_ID, to: "all", text, msg_type: "telemetry" }),
    });
  } catch (e: any) {
    log.warn(`Telemetry failed: ${e.message}`);
  }
}

// --- PCA Service proxy helper ---
export async function pcaCommand(action: string, payload: Record<string, unknown> = {}): Promise<string> {
  const r = await fetch(`${PCA_SERVICE_URL}/api/command`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, payload }),
  });
  if (!r.ok) {
    const err = await r.text();
    throw new Error(`PCA service error ${r.status}: ${err}`);
  }
  const data = await r.json();
  return JSON.stringify(data);
}
