import { createClient, SupabaseClient } from "@supabase/supabase-js";

// --- Konfiguration aus der Umgebung (Muster: mcp/agent-pca/tools/shared.ts) ---
export const SUPABASE_URL              = Deno.env.get("SUPABASE_URL") || "http://host.docker.internal:8001";
export const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
export const MCP_ACCESS_KEY            = Deno.env.get("MCP_ACCESS_KEY") || "";
export const AGENT_ID                  = Deno.env.get("AGENT_ID") || "wiq";

/** Groesster akzeptierter result-Payload (Zeichen, grob). Schutz gegen Kontext-Bomben. */
export const MAX_RESULT_CHARS = 256_000;

export const supabase: SupabaseClient = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

export const log = {
  info:  (msg: string) => console.log(`[INFO] ${new Date().toISOString()} ${msg}`),
  warn:  (msg: string) => console.warn(`[WARN] ${new Date().toISOString()} ${msg}`),
  error: (msg: string) => console.error(`[ERROR] ${new Date().toISOString()} ${msg}`),
};

/** Einheitliche Textantwort fuer MCP. */
export function text(body: string) {
  return { content: [{ type: "text" as const, text: body }] };
}

/** Einheitlicher Fehler: wird als isError ausgeliefert, nie als Exception verschluckt. */
export function fail(body: string) {
  return { content: [{ type: "text" as const, text: body }], isError: true };
}

/** PostgREST-Fehler in eine lesbare Zeile verwandeln. */
export function describeError(error: { message?: string; code?: string; details?: string } | null): string {
  if (!error) return "unbekannter Fehler";
  return [error.code, error.message, error.details].filter(Boolean).join(" | ");
}
