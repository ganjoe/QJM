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
let lastXApiFetchTime = 0;
export const xRateLimitStats = {
  requestsInWindow: 0,
  windowStart: Date.now(),
  remaining: -1,
  resetEpoch: 0,
  totalRequests: 0,
  totalNewPosts: 0,
};

export async function throttledXFetch(url: string, init?: RequestInit): Promise<Response> {
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

  return res;
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

  const isAlphaSymbol = /^[A-Z0-9.\-\s]+$/.test(t) && /[A-Z]/.test(t);
  const isExchangeNumericCode = /^\d{4,6}(\.[A-Z]+)?$/.test(t);

  return isAlphaSymbol || isExchangeNumericCode;
}

export async function updateFirstMentions(author: string, tickers: string[], publishedAt: string, postId: string): Promise<void> {
  if (!tickers || tickers.length === 0 || !publishedAt || !postId) return;
  const cleanAuthor = author.toLowerCase().startsWith("@") ? author.toLowerCase() : `@${author.toLowerCase()}`;
  for (const ticker of tickers) {
    const cleanTicker = ticker.toUpperCase().replace(/^[$#]/, "").trim();
    if (!cleanTicker || !isValidTicker(cleanTicker)) continue;
    try {
      await supabase.from("x_first_mentions").upsert({
        ticker: cleanTicker,
        author: cleanAuthor,
        first_mentioned_at: publishedAt,
        post_id: postId
      }, { onConflict: "ticker,author" });
    } catch (e: any) {
      log.error(`Failed to update first mention for ${cleanTicker}: ${e.message}`);
    }
  }
}

// --- Embeddings via Switchyard ---
export async function getEmbedding(text: string): Promise<number[]> {
  const embeddings = await getEmbeddingsBatch([text]);
  return embeddings[0];
}

export async function getEmbeddingsBatch(texts: string[]): Promise<number[][]> {
  if (!texts || texts.length === 0) return [];
  const start = Date.now();
  const baseUrl = SWITCHYARD_URL.endsWith("/v1") ? SWITCHYARD_URL : `${SWITCHYARD_URL}/v1`;
  
  const r = await fetch(`${baseUrl}/embeddings`, {
    method: "POST",
    headers: { 
      "Content-Type": "application/json",
      "Authorization": "Bearer switchyard"
    },
    body: JSON.stringify({ model: EMBED_MODEL, input: texts }),
  });

  if (!r.ok) {
    const errText = await r.text();
    throw new Error(`Embeddings failed (${baseUrl}/embeddings, status ${r.status}): ${errText}`);
  }

  const d = await r.json();
  const duration = ((Date.now() - start) / 1000).toFixed(2);
  log.debug(`[Switchyard] Batch Embedding (${texts.length} items) completed in ${duration}s`);
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

// --- LLM Metadata Extraction (via Switchyard) ---
export async function extractMetadata(text: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
  const route = await getActiveProvider();
  
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
    log.error(`[extractMetadata] Switchyard (route=${route}) failed: ${res.status} - ${errText}`);
    return { topics: ["uncategorized"], type: "observation" };
  }

  const d = await res.json();
  let parsed: any = { topics: ["uncategorized"], type: "observation" };
  try {
    const content = d.choices?.[0]?.message?.content || "";
    const jsonMatch = content.match(/\{[\s\S]*\}/);
    parsed = JSON.parse(jsonMatch ? jsonMatch[0] : content);
  } catch {
    // fallback
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
