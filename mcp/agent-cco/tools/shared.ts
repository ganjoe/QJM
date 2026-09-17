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

export const AGENT_ID = Deno.env.get("AGENT_ID") || "cco";
export const GLOBAL_BRAIN_ACCESS = Deno.env.get("GLOBAL_BRAIN_ACCESS") === "true";

export const X_BEARER_TOKEN = Deno.env.get("X_BEARER_TOKEN") || "";
export const X_CLIENT_ID = Deno.env.get("X_CLIENT_ID") || "";
export const X_CLIENT_SECRET = Deno.env.get("X_CLIENT_SECRET") || "";
export const TWITTER_API_IO_KEY = Deno.env.get("TWITTER_API_IO_KEY") || Deno.env.get("TWITTERAPI_IO_KEY") || "";

export const SWITCHYARD_URL = Deno.env.get("SWITCHYARD_URL") || "http://switchyard:4000/v1";
export const EMBED_MODEL = Deno.env.get("EMBED_MODEL") || "embeddings";
export const WEB_SCRAPER_URL = Deno.env.get("WEB_SCRAPER_URL") || "http://host.docker.internal:8797";

// Performance & Concurrency Settings
export const X_DISCOVERY_INTERVAL_SEC = parseInt(Deno.env.get("X_DISCOVERY_INTERVAL_SEC") || "30");
export const X_MIN_REQUEST_DELAY_MS = parseInt(Deno.env.get("X_MIN_REQUEST_DELAY_MS") || "2500");
export const MAX_CONCURRENT_METADATA_WORKERS = parseInt(Deno.env.get("MAX_CONCURRENT_METADATA_WORKERS") || "4");
export const MAX_CONCURRENT_YT_CHANNELS = parseInt(Deno.env.get("MAX_CONCURRENT_YT_CHANNELS") || "3");
export const EMBEDDING_BATCH_SIZE = parseInt(Deno.env.get("EMBEDDING_BATCH_SIZE") || "25");
export const AUTO_START_WORKERS = Deno.env.get("AUTO_START_WORKERS") !== "false";
export const X_INITIAL_BACKFILL_LIMIT = parseInt(Deno.env.get("X_INITIAL_BACKFILL_LIMIT") || "200");
export const X_INITIAL_SYNC_CONCURRENCY = parseInt(Deno.env.get("X_INITIAL_SYNC_CONCURRENCY") || "2");
// Günstiger Liveness-Check vor jedem Timeline-Fetch (spart ~95% der TwitterAPI.io-Kosten)
export const X_LIVENESS_CHECK = Deno.env.get("X_LIVENESS_CHECK") !== "false";

// --- X-Ingestion Modus ---
// "search"   = advanced_search mit since_time (Abrechnung pro geliefertem Tweet, ~$0.05/Tag)
// "timeline" = user/tweet_timeline + Liveness-Check (Abrechnung pro 20er-Page)
export const X_INGESTION_MODE = (Deno.env.get("X_INGESTION_MODE") || "search").toLowerCase();
export const X_SEARCH_INTERVAL_SEC = parseInt(Deno.env.get("X_SEARCH_INTERVAL_SEC") || "3600");
export const X_SEARCH_OVERLAP_SEC = parseInt(Deno.env.get("X_SEARCH_OVERLAP_SEC") || "900");
export const X_SEARCH_MAX_CATCHUP_SEC = parseInt(Deno.env.get("X_SEARCH_MAX_CATCHUP_SEC") || "86400");
export const X_RECONCILE_INTERVAL_SEC = parseInt(Deno.env.get("X_RECONCILE_INTERVAL_SEC") || "604800");
export const X_RECONCILE_LIMIT = parseInt(Deno.env.get("X_RECONCILE_LIMIT") || "2000");

// --- Database Client ---
export const supabase: SupabaseClient = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

// --- X OAuth 2.0 PKCE Helpers & Token Manager ---
function base64UrlEncode(bytes: Uint8Array): string {
  let str = "";
  for (let i = 0; i < bytes.length; i++) {
    str += String.fromCharCode(bytes[i]);
  }
  return btoa(str)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

export function generateCodeVerifier(): string {
  const array = new Uint8Array(64);
  crypto.getRandomValues(array);
  return base64UrlEncode(array);
}

export async function generateCodeChallenge(verifier: string): Promise<string> {
  const data = new TextEncoder().encode(verifier);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return base64UrlEncode(new Uint8Array(digest));
}

export interface XOAuthTokens {
  access_token: string;
  refresh_token: string;
  expires_at: number; // epoch ms
  user_id?: string;
  username?: string;
  name?: string;
  scope?: string;
}

export async function getXOAuthTokens(): Promise<XOAuthTokens | null> {
  try {
    const { data } = await supabase
      .from("system_settings")
      .select("value")
      .eq("key", "x_oauth_tokens")
      .single();
    if (data?.value && data.value.access_token) {
      return data.value as XOAuthTokens;
    }
  } catch (e: any) {
    log.error(`Failed to load X OAuth tokens: ${e.message}`);
  }
  return null;
}

export async function saveXOAuthTokens(tokens: XOAuthTokens): Promise<void> {
  const { error } = await supabase.from("system_settings").upsert({
    key: "x_oauth_tokens",
    value: tokens,
  }, { onConflict: "key" });
  if (error) {
    log.error(`Failed to save X OAuth tokens: ${error.message}`);
    throw error;
  }
}

export async function getValidXUserAccessToken(): Promise<{ access_token: string; user_id?: string; username?: string }> {
  const tokens = await getXOAuthTokens();
  if (!tokens || !tokens.access_token) {
    throw new Error("X OAuth 2.0 ist noch nicht autorisiert. Bitte öffne http://127.0.0.1:8788/auth/x/login im Browser.");
  }

  // If token expires in less than 2 minutes, refresh it
  if (Date.now() >= tokens.expires_at - 120000) {
    if (!tokens.refresh_token) {
      throw new Error("Kein Refresh-Token vorhanden. Bitte erneut unter http://127.0.0.1:8788/auth/x/login anmelden.");
    }
    if (!X_CLIENT_ID) {
      throw new Error("X_CLIENT_ID ist nicht konfiguriert.");
    }

    const headers: Record<string, string> = {
      "Content-Type": "application/x-www-form-urlencoded",
    };
    if (X_CLIENT_SECRET) {
      headers["Authorization"] = `Basic ${btoa(`${X_CLIENT_ID}:${X_CLIENT_SECRET}`)}`;
    }

    const bodyParams = new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: tokens.refresh_token,
      client_id: X_CLIENT_ID,
    });

    const res = await fetch("https://api.twitter.com/2/oauth2/token", {
      method: "POST",
      headers,
      body: bodyParams.toString(),
    });

    if (!res.ok) {
      const errText = await res.text();
      log.error(`X OAuth Refresh failed: ${errText}`);
      throw new Error(`X OAuth Token-Refresh fehlgeschlagen: ${errText}`);
    }

    const data = await res.json();
    const updated: XOAuthTokens = {
      access_token: data.access_token,
      refresh_token: data.refresh_token || tokens.refresh_token,
      expires_at: Date.now() + (data.expires_in * 1000),
      user_id: tokens.user_id,
      username: tokens.username,
      name: tokens.name,
      scope: data.scope || tokens.scope,
    };
    await saveXOAuthTokens(updated);
    log.info("X OAuth Token erfolgreich erneuert.");
    return { access_token: updated.access_token, user_id: updated.user_id, username: updated.username };
  }

  return { access_token: tokens.access_token, user_id: tokens.user_id, username: tokens.username };
}

// --- Global X API Rate Limiter ---
export let globalXApiBlockedUntil = 0;
let lastXApiFetchTime = 0;
export const xRateLimitStats = {
  requestsInWindow: 0,
  windowStart: Date.now(),
  remaining: -1,
  resetEpoch: 0,
  totalRequests: 0,
  totalNewPosts: 0,
};

// --- TwitterAPI.io Stats ---
export const twitterApiIoStats = {
  requestsInWindow: 0,
  windowStart: Date.now(),
  totalRequests: 0,
  lastRequestTime: 0,
};

export async function throttledXFetch(url: string, init?: RequestInit): Promise<Response> {
  if (Date.now() < globalXApiBlockedUntil) {
    throw new Error(`X API fetch skipped: Blocked until ${new Date(globalXApiBlockedUntil).toISOString()} due to previous 402 Payment Required error.`);
  }

  const now = Date.now();
  const elapsed = now - lastXApiFetchTime;
  if (elapsed < X_MIN_REQUEST_DELAY_MS) {
    await new Promise(r => setTimeout(r, X_MIN_REQUEST_DELAY_MS - elapsed));
  }

  // If rate limit is close to exhaustion, wait until reset
  if (xRateLimitStats.remaining >= 0 && xRateLimitStats.remaining <= 2 && xRateLimitStats.resetEpoch > 0) {
    const sleepMs = Math.max(1000, (xRateLimitStats.resetEpoch * 1000) - Date.now() + 1000);
    log.warn(`[X Rate Limiter] Proaktive Pause: remaining=${xRateLimitStats.remaining}, warte ${Math.round(sleepMs/1000)}s bis Reset...`);
    await new Promise(r => setTimeout(r, sleepMs));
  }

  lastXApiFetchTime = Date.now();
  xRateLimitStats.totalRequests++;
  xRateLimitStats.requestsInWindow++;

  if (Date.now() - xRateLimitStats.windowStart > 15 * 60 * 1000) {
    xRateLimitStats.requestsInWindow = 0;
    xRateLimitStats.windowStart = Date.now();
  }

  const res = await fetch(url, init);

  const remainingHeader = res.headers.get("x-rate-limit-remaining");
  const resetHeader = res.headers.get("x-rate-limit-reset");
  if (remainingHeader !== null) xRateLimitStats.remaining = Number(remainingHeader);
  if (resetHeader !== null) xRateLimitStats.resetEpoch = Number(resetHeader);

  if (res.status === 402) {
    globalXApiBlockedUntil = Date.now() + 15 * 60 * 1000;
    log.error(`[X API] 402 Payment Required empfangen. Pausiere alle X API Requests für 15 Minuten bis ${new Date(globalXApiBlockedUntil).toISOString()}`);
  }

  return res;
}

// --- TwitterAPI.io Fetcher (fremde Profile – kein Fallback zur offiziellen API) ---
export function isTwitterApiIoAvailable(): boolean {
  return Boolean(TWITTER_API_IO_KEY);
}

export async function twitterApiIoFetch(
  endpoint: string,
  params: Record<string, string | undefined>,
): Promise<any> {
  if (!TWITTER_API_IO_KEY) {
    throw new Error(
      "❌ TwitterAPI.io ist nicht konfiguriert. Setze TWITTER_API_IO_KEY in der .env. " +
      "Prüfe den Provider-Status mit 'manage_sync_pipeline STATUS'.",
    );
  }

  // Rate-Limiting: TwitterAPI.io supports 200 QPS, wir limitieren konservativ auf 25 req/15s
  const now = Date.now();
  if (now - twitterApiIoStats.windowStart > 15_000) {
    twitterApiIoStats.requestsInWindow = 0;
    twitterApiIoStats.windowStart = now;
  }
  if (twitterApiIoStats.requestsInWindow >= 25) {
    const sleepMs = 15_000 - (now - twitterApiIoStats.windowStart) + 500;
    log.warn(`[TwitterAPI.io] Rate-Limit (25/15s) erreicht, warte ${Math.round(sleepMs / 1000)}s...`);
    await new Promise((r) => setTimeout(r, sleepMs));
    twitterApiIoStats.requestsInWindow = 0;
    twitterApiIoStats.windowStart = Date.now();
  }

  // Minimaler Delay zwischen Requests (200ms)
  const elapsed = Date.now() - twitterApiIoStats.lastRequestTime;
  if (elapsed < 200) {
    await new Promise((r) => setTimeout(r, 200 - elapsed));
  }

  const url = new URL(`https://api.twitterapi.io/twitter/${endpoint}`);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") {
      url.searchParams.set(k, v);
    }
  }

  twitterApiIoStats.lastRequestTime = Date.now();
  twitterApiIoStats.totalRequests++;
  twitterApiIoStats.requestsInWindow++;

  const res = await fetch(url.toString(), {
    headers: {
      "X-API-Key": TWITTER_API_IO_KEY,
      "User-Agent": "OpenBrain-CCO/2.0",
    },
  });

  if (!res.ok) {
    const errText = await res.text().catch(() => "unreadable body");
    if (res.status === 429) {
      throw new Error(`TwitterAPI.io Rate Limit überschritten: ${errText}`);
    }
    if (res.status === 402) {
      throw new Error(`TwitterAPI.io Payment Required (402): Guthaben aufgebraucht? ${errText}`);
    }
    if (res.status === 403) {
      throw new Error(`TwitterAPI.io Forbidden (403): API-Key ungültig oder Endpunkt nicht verfügbar. ${errText}`);
    }
    throw new Error(`TwitterAPI.io HTTP ${res.status}: ${errText}`);
  }

  const json = await res.json();

  // Auto-unwrap: manche Endpoints wrappen in "data" (user/info, tweet_timeline),
  // andere nicht (tweets, followings). Einheitlich auflösen, dabei Paginierungs-Metadaten erhalten.
  if (json.data && typeof json.data === "object" && !Array.isArray(json.data)) {
    return {
      ...json.data,
      has_next_page: json.has_next_page ?? json.data.has_next_page ?? json.hasNextPage ?? json.data.hasNextPage,
      next_cursor: json.next_cursor ?? json.data.next_cursor ?? json.nextCursor ?? json.data.nextCursor ?? json.cursor ?? json.data.cursor,
    };
  }

  return json;
}

// --- Ticker Validator & First Mentions ---
export function isValidTicker(ticker: string): boolean {
  if (!ticker) return false;
  const t = ticker.trim().toUpperCase().replace(/^[$#]/, "");
  if (!t) return false;

  // Reject pure numbers, decimals, or common prices/years
  if (/^\d+(\.\d+)?$/.test(t)) {
    if (/^\d{1,3}$/.test(t) || t.includes(".")) return false;
    if (t === "2024" || t === "2025" || t === "2026" || t === "2027") return false;
  }

  // Reject monetary amounts & multipliers (e.g. "1K", "15K", "100K", "400M", "2B", "9.8B", "1T")
  if (/^\d+(\.\d+)?[KkMmBbTt]$/.test(t)) return false;

  // Reject percentage or multiplier formats (e.g. "50%", "5X")
  if (/^\d+[%xX]$/.test(t)) return false;

  // Fiat-Währungen / Stablecoins sind keine handelbaren Aktien-Ticker
  // (verhindert False Positives wie "$USD", "$USDC").
  const NON_TICKERS = new Set([
    "USD", "USDT", "USDC", "BUSD", "TUSD", "DAI",
    "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "CNY", "HKD", "INR", "KRW", "SEK", "NOK", "MXN", "BRL",
  ]);
  if (NON_TICKERS.has(t)) return false;

  const isAlphaSymbol = /^[A-Z0-9.\-\s]+$/.test(t) && /[A-Z]/.test(t);
  const isExchangeNumericCode = /^\d{4,6}(\.[A-Z]+)?$/.test(t);

  return isAlphaSymbol || isExchangeNumericCode;
}

/**
 * Normalisiert rohe Ticker-Ausgaben (LLM/Text): trimmt, entfernt führende $/#,
 * UPPERCASES, dedupliziert und verwirft ungültige Tokens (Preise, Jahre, Mengen).
 */
export function normalizeTickers(raw: unknown): string[] {
  const parts: string[] = [];
  if (Array.isArray(raw)) {
    for (const item of raw) {
      if (item == null) continue;
      parts.push(...String(item).split(/[,\s]+/));
    }
  } else if (typeof raw === "string") {
    parts.push(...raw.split(/[,\s]+/));
  } else {
    return [];
  }
  const out = new Set<string>();
  for (const part of parts) {
    const t = part.trim().toUpperCase().replace(/^[$#]+/, "").replace(/[.,;:!?)\]}]+$/, "");
    if (!t || !isValidTicker(t)) continue;
    out.add(t);
  }
  return Array.from(out);
}

/**
 * Persistiert Erst-Erwähnungen. Wichtig: Ein späterer Post darf ein bereits
 * gespeichertes früheres Datum NICHT überschreiben. Die vorherige Version nutzte
 * ein blindes Upsert und verschob die "Erst"-Erwähnung bei jeder neuen Erwähnung
 * nach vorne (deshalb war max(first_mentioned_at) == Verarbeitungszeitpunkt).
 */
export async function updateFirstMentions(author: string, tickers: string[], publishedAt: string, postId: string): Promise<void> {
  if (!tickers || tickers.length === 0 || !publishedAt || !postId) return;
  const cleanAuthor = author.toLowerCase().startsWith("@") ? author.toLowerCase() : `@${author.toLowerCase()}`;
  for (const ticker of tickers) {
    const cleanTicker = ticker.toUpperCase().replace(/^[$#]/, "").trim();
    if (!cleanTicker || !isValidTicker(cleanTicker)) continue;
    try {
      // Atomar: upsert_first_mention aktualisiert nur, wenn das neue Datum früher liegt.
      const { error } = await supabase.rpc("upsert_first_mention", {
        p_ticker: cleanTicker,
        p_author: cleanAuthor,
        p_first_mentioned_at: publishedAt,
        p_post_id: postId,
      });
      if (error) throw error;
    } catch (e: any) {
      // Fallback, falls die RPC noch nicht deployt ist: manuell, aber nie ein späteres Datum übernehmen.
      try {
        const { data: existing } = await supabase
          .from("x_first_mentions")
          .select("first_mentioned_at")
          .eq("ticker", cleanTicker)
          .eq("author", cleanAuthor)
          .maybeSingle();
        if (!existing) {
          await supabase.from("x_first_mentions").insert({
            ticker: cleanTicker,
            author: cleanAuthor,
            first_mentioned_at: publishedAt,
            post_id: postId,
          });
        } else if (new Date(publishedAt).getTime() < new Date(existing.first_mentioned_at).getTime()) {
          await supabase
            .from("x_first_mentions")
            .update({ first_mentioned_at: publishedAt, post_id: postId })
            .eq("ticker", cleanTicker)
            .eq("author", cleanAuthor);
        }
      } catch (e2: any) {
        log.error(`Failed to update first mention for ${cleanTicker}: ${e2.message} (rpc: ${e.message})`);
      }
    }
  }
}

// --- Embeddings via Switchyard ---
// Prioritätsklassen für den Switchyard-Embedding-Scheduler:
//   'x_search' = interaktive Suche (Vorrang), 'x_post' = X-Posts/Profile, 'yt' = YouTube-Chunks.
// Ohne Angabe behandelt der Gateway die Anfrage als interaktiv.
export type EmbeddingPriority = "x_search" | "x_post" | "yt";

export async function getEmbedding(
  text: string,
  priority: EmbeddingPriority = "x_post",
): Promise<number[]> {
  const embeddings = await getEmbeddingsBatch([text], priority);
  return embeddings[0];
}

export async function getEmbeddingsBatch(
  texts: string[],
  priority: EmbeddingPriority = "x_post",
): Promise<number[][]> {
  if (!texts || texts.length === 0) return [];
  const start = Date.now();
  const baseUrl = SWITCHYARD_URL.endsWith("/v1") ? SWITCHYARD_URL : `${SWITCHYARD_URL}/v1`;
  const cls = priority || "x_post";

  const r = await fetch(`${baseUrl}/embeddings`, {
    method: "POST",
    headers: { 
      "Content-Type": "application/json",
      "Authorization": "Bearer switchyard"
    },
    body: JSON.stringify({ model: EMBED_MODEL, input: texts, priority: cls }),
  });

  if (!r.ok) {
    const errText = await r.text();
    throw new Error(`Embeddings failed (${baseUrl}/embeddings, class ${cls}, status ${r.status}): ${errText}`);
  }

  const d = await r.json();
  const duration = ((Date.now() - start) / 1000).toFixed(2);
  log.debug(`[Switchyard] Batch Embedding (${texts.length} items, class ${cls}) completed in ${duration}s`);
  return d.data.map((item: any) => item.embedding);
}

// --- Active Provider Helper (returns Switchyard route) ---
export async function getActiveProvider(key: string = "cco"): Promise<string> {
  try {
    const { data } = await supabase
      .from("system_settings")
      .select("value")
      .eq("key", "provider_config")
      .single();
    
    if (data?.value && data.value[key]) {
      return data.value[key];
    }
  } catch (_e) {
    // default
  }
  return "auto";
}

// --- Switchyard Route Validation ---
// Verhindert 404 "No route registered for model X", wenn system_settings.provider_config
// auf eine Route zeigt, die in llm-gateway/switchyard-config/routes.toml nicht (mehr) existiert.
// Beispiel: 'local' wurde mit Commit 07daea0 entfernt, provider_config zeigt aber weiterhin darauf.
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

/**
 * Löst einen logischen Provider-/Routennamen gegen die real registrierten Switchyard-Routen auf.
 * Fällt nur dann auf FALLBACK_ROUTE zurück, wenn die Registry abrufbar ist, der Name aber fehlt.
 * Ist die Registry nicht abrufbar (Netzfehler, Timeout), bleibt der Name unverändert --
 * sonst würde eine funktionierende Konfiguration bei einem transienten Fehler umgebogen.
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

/**
 * Modell für Vision-/OCR-Aufrufe: bevorzugt system_settings.vision_model_config.model,
 * sonst die konfigurierte Provider-Route. In beiden Fällen gegen die Registry validiert.
 */
export async function resolveVisionModel(): Promise<string> {
  let requested = "";
  try {
    const { data } = await supabase
      .from("system_settings")
      .select("value")
      .eq("key", "vision_model_config")
      .single();
    if (data?.value?.model) requested = String(data.value.model);
  } catch (_e) {
    // fällt unten auf die Provider-Route zurück
  }
  if (!requested) requested = await getActiveProvider();
  return await resolveSwitchyardRoute(requested, { logFallback: true });
}

// --- LLM Metadata Extraction (via Switchyard) ---
export async function extractMetadata(text: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
  const configuredRoute = await getActiveProvider();
  const route = await resolveSwitchyardRoute(configuredRoute, { logFallback: true });
  
  let systemPrompt = "Extract metadata from the user's captured thought. Return ONLY valid JSON.";
  const promptPaths = [
    "/app/prompts/metadata-prompt.txt",
    "/app/metadata-prompt.txt",
    "prompts/metadata-prompt.txt",
    "metadata-prompt.txt"
  ];
  for (const p of promptPaths) {
    try {
      systemPrompt = Deno.readTextFileSync(p);
      break;
    } catch (_e) {
      // try next
    }
  }

  const baseUrl = SWITCHYARD_URL.endsWith("/v1") ? SWITCHYARD_URL : `${SWITCHYARD_URL}/v1`;
  const res = await fetch(`${baseUrl}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer switchyard",
    },
    body: JSON.stringify({
      model: route,
      messages: [{ role: "system", content: systemPrompt }, { role: "user", content: text }],
      temperature: 0.1,
    }),
    signal,
  });

  if (!res.ok) {
    const errText = await res.text();
    log.error(`[extractMetadata] Switchyard (route=${route}${route !== configuredRoute ? `, konfiguriert=${configuredRoute}` : ""}) failed: ${res.status} - ${errText}`);
    return {
      topics: ["uncategorized"],
      type: "observation",
      _extraction_failed: true,
      _extraction_error: `Switchyard HTTP ${res.status}`,
    };
  }

  const d = await res.json();
  let parsed: any = null;
  let parseError = "invalid JSON";
  try {
    const content = d.choices?.[0]?.message?.content || "";
    const jsonMatch = content.match(/\{[\s\S]*\}/);
    parsed = JSON.parse(jsonMatch ? jsonMatch[0] : content);
  } catch (e: any) {
    parseError = e.message;
  }

  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    log.warn(`[extractMetadata] LLM-Antwort nicht als Objekt parsebar (${parseError}); markiere als fehlgeschlagen.`);
    return {
      topics: ["uncategorized"],
      type: "observation",
      _extraction_failed: true,
      _extraction_error: `parse: ${parseError}`,
    };
  }

  return parsed;
}

// --- Author Handle Resolution ---
export async function resolveAuthorHandles(authorInput: string): Promise<{ primaryUsername: string, allHandles: string[] }> {
  const clean = authorInput.replace(/^@/, "").trim().toLowerCase();
  if (!clean) return { primaryUsername: "", allHandles: [] };

  const handles = new Set<string>();
  handles.add(`@${clean}`);
  handles.add(clean);

  let primaryUsername = clean;

  try {
    const { data: matchedUsers } = await supabase
      .from("x_users")
      .select("username, screen_name, is_active")
      .or(`username.ilike.%${clean}%,screen_name.ilike.%${clean}%`);

    if (matchedUsers && matchedUsers.length > 0) {
      const activeMatch = matchedUsers.find((u: any) => u.is_active) || matchedUsers[0];
      if (activeMatch?.username) {
        primaryUsername = activeMatch.username.toLowerCase();
      }

      for (const u of matchedUsers) {
        if (u.username) {
          handles.add(`@${u.username.toLowerCase()}`);
          handles.add(u.username.toLowerCase());
        }
        if (u.screen_name) {
          handles.add(`@${u.screen_name.toLowerCase()}`);
          handles.add(u.screen_name.toLowerCase());
        }
      }
    }
  } catch (e: any) {
    log.warn(`[Author Resolution] Failed to query x_users: ${e.message}`);
  }

  return {
    primaryUsername,
    allHandles: Array.from(handles)
  };
}
