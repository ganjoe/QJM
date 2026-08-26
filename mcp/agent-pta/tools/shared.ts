import { createClient, SupabaseClient } from "@supabase/supabase-js";

// --- Standard Structured Logger ---
export const log = {
  info: (msg: string, ...args: any[]) => console.log(`[INFO] [${new Date().toISOString()}] ${msg}`, ...args),
  warn: (msg: string, ...args: any[]) => console.warn(`[WARN] [${new Date().toISOString()}] ${msg}`, ...args),
  error: (msg: string, ...args: any[]) => console.error(`[ERROR] [${new Date().toISOString()}] ${msg}`, ...args),
  debug: (msg: string, ...args: any[]) => console.debug(`[DEBUG] [${new Date().toISOString()}] ${msg}`, ...args),
};

// --- Configuration from Environment ---
export const SUPABASE_URL = Deno.env.get("SUPABASE_URL") || "http://host.docker.internal:8001";
export const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
export const MCP_ACCESS_KEY = Deno.env.get("MCP_ACCESS_KEY") || "";

export const AGENT_ID = Deno.env.get("AGENT_ID") || "pta";

// --- Supabase Client ---
export const supabase: SupabaseClient = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

// --- Active Trading Mode helper ---
export async function getActiveTradingMode(): Promise<"live" | "paper"> {
  try {
    const { data } = await supabase
      .from("system_settings")
      .select("value")
      .eq("key", "ib_gateway_config")
      .single();
    if (data?.value?.active_mode === "paper") return "paper";
  } catch (_e) {
    log.warn("[Mode] Failed to fetch trading mode, defaulting to live.");
  }
  return "live";
}
