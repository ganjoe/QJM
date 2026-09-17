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
export const SWITCHYARD_URL = Deno.env.get("SWITCHYARD_URL") || "http://switchyard:4000/v1";

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

// --- Switchyard Route Validation ---
// Siehe mcp/agent-cco/tools/shared.ts: verhindert 404 "No route registered for model X",
// wenn system_settings.provider_config auf eine nicht (mehr) existierende Route zeigt.
export const FALLBACK_ROUTE = "auto";

const ROUTES_TTL_MS = 5 * 60 * 1000;
const INVALID_TTL_MS = 60 * 1000;
const ROUTES_TIMEOUT_MS = 2000;

let routesCache: { names: Set<string>; fetchedAt: number } | null = null;
const invalidModelCache = new Map<string, number>();

/** Liest die registrierten Chat-Routen aus GET /v1/models (5 min TTL, negativer Cache 60 s). */
async function fetchRegisteredRoutes(): Promise<Set<string> | null> {
  const base = SWITCHYARD_URL.replace(/\/v1\/?$/, "");
  try {
    const res = await fetch(`${base}/v1/models`, { signal: AbortSignal.timeout(ROUTES_TIMEOUT_MS) });
    if (!res.ok) return null;
    const json = await res.json();
    const data = Array.isArray(json?.data) ? json.data : [];
    const chatRoutes = data.filter(
      (m: any) => m && typeof m.id === "string" && m.type !== "embedding",
    );
    if (chatRoutes.length === 0) return null;
    return new Set<string>(chatRoutes.map((m: any) => String(m.id)));
  } catch (_e) {
    return null;
  }
}

export async function getActiveProvider(key: string = "pta"): Promise<string> {
  try {
    const { data } = await supabase
      .from("system_settings")
      .select("value")
      .eq("key", "provider_config")
      .single();
    if (data?.value && data.value[key]) return String(data.value[key]);
  } catch (_e) {
    // default
  }
  return FALLBACK_ROUTE;
}

/**
 * Löst einen logischen Provider-/Routennamen gegen die real registrierten Switchyard-Routen auf.
 * Fällt nur dann auf FALLBACK_ROUTE zurück, wenn die Registry abrufbar ist, der Name aber fehlt.
 * Ist die Registry nicht abrufbar (Netzfehler, Timeout), bleibt der Name unverändert.
 */
export async function resolveSwitchyardRoute(
  requested: string,
  opts: { logFallback?: boolean } = {},
): Promise<string> {
  if (!requested) return FALLBACK_ROUTE;

  if (routesCache && Date.now() - routesCache.fetchedAt > ROUTES_TTL_MS) routesCache = null;
  if (!routesCache) {
    const names = await fetchRegisteredRoutes();
    if (names) routesCache = { names, fetchedAt: Date.now() };
  }

  if (!routesCache) return requested;

  if (routesCache.names.has(requested)) {
    invalidModelCache.delete(requested);
    return requested;
  }

  const invalidUntil = invalidModelCache.get(requested);
  if (!invalidUntil || Date.now() > invalidUntil) {
    invalidModelCache.set(requested, Date.now() + INVALID_TTL_MS);
    if (opts.logFallback) {
      log.warn(
        `[Switchyard] Route '${requested}' ist nicht registriert (verfügbar: ${Array.from(routesCache.names).sort().join(", ")}). ` +
        `Fallback auf '${FALLBACK_ROUTE}'. provider_config in system_settings korrigieren oder Route in routes.toml anlegen.`,
      );
    }
  }

  return routesCache.names.has(FALLBACK_ROUTE) ? FALLBACK_ROUTE : requested;
}
