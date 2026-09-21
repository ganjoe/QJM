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

// --- DeepSeek: direkte LLM-Anbindung (ersetzt Switchyard fuer Chat/Completion) ---
// Switchyard wird NUR noch fuer Embeddings genutzt (lokales Ollama/mxbai).
// Alle Chat-/Completion-Aufrufe (Metadaten, Vision-OCR, Company-Extraktion) gehen
// direkt an die DeepSeek-API, damit die erschoepfte Gemini-Quota den CCO-Agenten
// nicht mehr blockiert. Basis-URL/Modell sind per Env konfigurierbar.
export const DEEPSEEK_API_KEY = Deno.env.get("DEEPSEEK_API_KEY") || "";
export const DEEPSEEK_BASE_URL = (Deno.env.get("DEEPSEEK_BASE_URL") || "https://api.deepseek.com").replace(/\/+$/, "");
export const DEEPSEEK_MODEL = Deno.env.get("DEEPSEEK_MODEL") || "deepseek-flash";
// Kein separates Vision-Modell: deepseek-flash ist ausreichend. Die DeepSeek-API mappt
// experimentelle/Vision-Modell-IDs ohnehin auf deepseek-flash. Per Env ueberschreibbar.
export const DEEPSEEK_VISION_MODEL = Deno.env.get("DEEPSEEK_VISION_MODEL") || DEEPSEEK_MODEL;
export const DEEPSEEK_TIMEOUT_MS = parseInt(Deno.env.get("DEEPSEEK_TIMEOUT_MS") || "120000");
export const DEEPSEEK_MAX_RETRIES = parseInt(Deno.env.get("DEEPSEEK_MAX_RETRIES") || "4");
// Reasoning-Steuerung: "off" (Default) deaktiviert das Denken fuer einfache Aufgaben
// wie Ticker-/Metadaten-Extraktion und spart massiv Tokens. Erlaubt: off | low | high | max.
export const DEEPSEEK_REASONING_EFFORT = (Deno.env.get("DEEPSEEK_REASONING_EFFORT") || "off").toLowerCase();

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

  // Unterstrich ist gueltig: Continuous-Futures-Symbole wie CL_F, ES_F, HG_F, YM_F.
  const isAlphaSymbol = /^[A-Z0-9._\-\s]+$/.test(t) && /[A-Z]/.test(t);
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

// Zentrale Embedding-Version. Jede Aenderung an Modell, Textaufbau oder Normalisierung
// MUSS hier gebumpt werden, damit die Stale-Erkennung (embedding_version + source_hash) greift.
export const EMBED_VERSION = Deno.env.get("EMBED_VERSION") || "mxbai-v1";
// Nur Fallback: der Gateway pinnt das Backend-Modell und liefert es in der Antwort mit.
export const EMBED_MODEL_NAME = Deno.env.get("EMBED_MODEL_NAME") || "mxbai-embed-large";

// mxbai-embed-large ist ein englischer BERT-Encoder mit 512 TOKEN Kontext.
//
// GEMESSEN am 2026-09-18 gegen Ollama 0.21.2 (mxbai-embed-large, OLLAMA_CONTEXT_LENGTH=4096):
//   * Ein Input mit <= 512 Tokens wird normal eingebettet.
//   * Ein Input mit > 512 Tokens fuehrt zu HTTP 400 "the input length exceeds the context
//     length" — Ollama trunkiert also NICHT zuverlaessig (nur bei manchen langen Texten,
//     z.B. stark repetitiven, wird still auf 512 gekuerzt). Verlaesslich ist nur: <= 512.
//     Belege: Transkript-Text 1440 Zeichen -> 510 Tokens OK; 1450 Zeichen -> HTTP 400.
//     Die Token-Dichte ist dabei stark textabhaengig: dichte Transkripte mit Zeitstempeln
//     ~2,8 Zeichen/Token, normale Prosa ~4-5 Zeichen/Token.
//
// Darum wird JEDER Dokumenttext VOR dem Hashing an einer Wortgrenze gekuerzt. Budget 1100
// Zeichen => selbst bei der dichtesten gemessenen Tokenisierung (~2,8 Zeichen/Token) nur
// ~390 Tokens, also ~25 % Reserve. Zusaetzlich faengt getDocumentEmbeddingsDetailed einen
// 400er ab und verkleinert das Budget schrittweise (siehe unten).
export const EMBED_MAX_CHARS = parseInt(Deno.env.get("EMBED_MAX_CHARS") || "1100");

/**
 * Ist der Fehler der Ollama-Kontextgrenze zuzuordnen (HTTP 400 "exceeds the context length")?
 * Der Gateway reicht den Backend-Fehler als Fehlertext durch.
 */
export function isContextLengthError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err ?? "");
  return /context length|context window|exceeds the context/i.test(msg);
}

// Erwartete Vektor-Dimension. Ein Mischbetrieb zweier Modelle in einem Index ist
// unmoeglich (pgvector erzwingt die Spaltendimension); dieser Guard laesst falsche
// Vektoren gar nicht erst bis zur DB kommen und macht eine falsche Gateway-Route laut.
export const EMBED_DIM = parseInt(Deno.env.get("EMBED_DIM") || "1024");

// mxbai-embed-large erwartet diesen Prefix NUR fuer Queries; Dokumente bleiben roh.
// Quelle: Modellkarte mxbai-embed-large-v1 (Mixedbread).
export const EMBED_QUERY_PREFIX = Deno.env.get("EMBED_QUERY_PREFIX") ??
  "Represent this sentence for searching relevant passages: ";

/**
 * Kuerzt einen Dokumenttext auf das Embedding-Kontextfenster — an der letzten Wortgrenze,
 * damit keine halben Tokens entstehen. Idempotent: bereits kurze Texte bleiben unveraendert.
 * WICHTIG: Das Ergebnis ist der exakt eingebettete Text und damit die Basis fuer source_hash.
 */
export function fitForEmbedding(text: string, maxChars: number = EMBED_MAX_CHARS): string {
  if (typeof text !== "string" || text.length === 0) return "";
  if (maxChars <= 0 || text.length <= maxChars) return text;
  const head = text.slice(0, maxChars);
  const cut = Math.max(head.lastIndexOf(" "), head.lastIndexOf("\n"), head.lastIndexOf("\t"));
  return (cut > maxChars * 0.5 ? head.slice(0, cut) : head).trimEnd();
}

/** Deterministischer SHA-256-Hex eines Textes — die eine kanonische source_hash-Funktion. */
export async function sha256Hex(text: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function getEmbedding(
  text: string,
  priority: EmbeddingPriority = "x_post",
): Promise<number[]> {
  const embeddings = await getEmbeddingsBatch([text], priority);
  return embeddings[0];
}

/**
 * Wie getEmbeddingsBatch, liefert zusaetzlich den tatsaechlich vom Gateway verwendeten
 * Modellnamen (fuer embedding_model). Der Gateway hat kein Modell-Fallback.
 */
export async function getEmbeddingsBatchDetailed(
  texts: string[],
  priority: EmbeddingPriority = "x_post",
): Promise<{ vectors: number[][]; model: string | null }> {
  if (!texts || texts.length === 0) return { vectors: [], model: null };
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
  const vectors = Array.isArray(d?.data) ? d.data.map((item: any) => item.embedding) : [];
  const reportedModel = typeof d?.model === "string" ? d.model : null;

  // Dimensions-Guard: verhindert, dass Vektoren eines fremden Modells in den Index laufen.
  for (let i = 0; i < vectors.length; i++) {
    const v = vectors[i];
    if (!Array.isArray(v) || v.length !== EMBED_DIM) {
      throw new Error(
        `Embedding-Dimension ${Array.isArray(v) ? v.length : "?"} != ${EMBED_DIM} (Index ${i}, Modell ${reportedModel ?? "?"}) — ` +
        `falsche Gateway-Route oder Modellwechsel ohne Re-Embed. Batch wird verworfen.`,
      );
    }
  }

  return { vectors, model: reportedModel };
}

export async function getEmbeddingsBatch(
  texts: string[],
  priority: EmbeddingPriority = "x_post",
): Promise<number[][]> {
  return (await getEmbeddingsBatchDetailed(texts, priority)).vectors;
}

// Fallback-Budgets, falls das Backend einen Text trotz fitForEmbedding ablehnt
// (z.B. extrem token-dichter Inhalt). Reihenfolge: gross -> klein.
export const EMBED_SHRINK_BUDGETS = (Deno.env.get("EMBED_SHRINK_BUDGETS") || "1100,800,560,380,240,140")
  .split(",").map((s) => parseInt(s.trim())).filter((n) => Number.isFinite(n) && n > 0);

/**
 * DOCUMENT-Embedding mit Garantie: liefert die tatsaechlich eingebetteten Texte ZURUECK.
 *
 * Warum das wichtig ist: source_hash muss den exakt eingebetteten Text beschreiben. Wenn
 * das Backend einen Text wegen der 512-Token-Grenze ablehnt (HTTP 400), wird er hier
 * schrittweise verkleinert und einzeln erneut eingebettet. Aufrufer MUESSEN `texts[i]`
 * (nicht ihren Originaltext) hashen und fuer YT-Chunks auch als `content` speichern.
 */
export async function getDocumentEmbeddingsDetailed(
  texts: string[],
  priority: EmbeddingPriority = "x_post",
): Promise<{ vectors: number[][]; model: string | null; texts: string[] }> {
  if (!texts || texts.length === 0) return { vectors: [], model: null, texts: [] };
  const fitted = texts.map((t) => fitForEmbedding(t));

  try {
    const { vectors, model } = await getEmbeddingsBatchDetailed(fitted, priority);
    return { vectors, model, texts: fitted };
  } catch (err) {
    if (!isContextLengthError(err)) throw err;
    log.warn(
      `[Embeddings] Backend meldet Kontextgrenze für einen Batch mit ${fitted.length} Text(en) ` +
      `(max ${Math.max(...fitted.map((t) => t.length))} Zeichen) — verkleinere betroffene Texte schrittweise.`,
    );
  }

  const outTexts: string[] = [];
  const outVectors: number[][] = [];
  let model: string | null = null;

  for (const original of fitted) {
    let done = false;
    for (const budget of EMBED_SHRINK_BUDGETS) {
      const candidate = fitForEmbedding(original, budget);
      if (!candidate) continue;
      try {
        const { vectors, model: m } = await getEmbeddingsBatchDetailed([candidate], priority);
        outTexts.push(candidate);
        outVectors.push(vectors[0]);
        model = m ?? model;
        done = true;
        break;
      } catch (err) {
        if (!isContextLengthError(err)) throw err;
      }
    }
    if (!done) {
      throw new Error(
        `Embedding-Text auch mit dem kleinsten Budget (${EMBED_SHRINK_BUDGETS[EMBED_SHRINK_BUDGETS.length - 1]} Zeichen) ` +
        `nicht einbettbar — Backend-Kontextgrenze oder fehlerhafte Route.`,
      );
    }
  }

  return { vectors: outVectors, model, texts: outTexts };
}

/** Einzel-Dokument-Variante von getDocumentEmbeddingsDetailed (kuerzt + faengt 400er ab). */
export async function getDocumentEmbedding(
  text: string,
  priority: EmbeddingPriority = "x_post",
): Promise<number[]> {
  const res = await getDocumentEmbeddingsDetailed([text], priority);
  return res.vectors[0];
}

/**
 * QUERY-Embedding: haengt den mxbai-Query-Prefix an. NUR fuer Suchanfragen verwenden —
 * Dokumente werden roh eingebettet (getEmbedding/getEmbeddingsBatch).
 */
export async function getQueryEmbedding(
  text: string,
  priority: EmbeddingPriority = "x_search",
): Promise<number[]> {
  return await getEmbedding(EMBED_QUERY_PREFIX + text, priority);
}

/** Batch-Variante von getQueryEmbedding. */
export async function getQueryEmbeddingsBatch(
  texts: string[],
  priority: EmbeddingPriority = "x_search",
): Promise<number[][]> {
  return await getEmbeddingsBatch(texts.map((t) => EMBED_QUERY_PREFIX + t), priority);
}

// ============================================================================
// Switchyard-Chat-Routen -- DEAKTIVIERT (DeepSeek-Migration 2026-09-20)
// ----------------------------------------------------------------------------
// Diese Helfer banden den CCO-Agenten fuer Chat/Completion an die Switchyard-
// Routen (Gemini). Weil die Gemini-Quota erschoepft war und alle Routen ueber
// denselben Key liefen, sind sie deaktiviert. Chat/Completion laeuft jetzt
// direkt ueber DeepSeek (siehe unten). Embeddings bleiben unveraendert auf
// Switchyard/Ollama (getEmbeddingsBatchDetailed oben).
// ============================================================================
/*
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

/** Liest die registrierten Chat-Routen aus GET /v1/models (5 min TTL, negativer Cache 60 s). * /
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
 * /
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
 * /
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

*/

// ============================================================================
// DeepSeek Chat/Completion (ersetzt Switchyard fuer LLM-Aufgaben)
// ============================================================================

/** Fehler eines LLM-Aufrufs. retryable=false => Auth-/Billing-Fehler, nicht wiederholen. */
export class LlmUnavailableError extends Error {
  readonly retryable: boolean;
  constructor(message: string, retryable = true) {
    super(message);
    this.name = "LlmUnavailableError";
    this.retryable = retryable;
  }
}

const DEEPSEEK_RETRY_BASE_MS = parseInt(Deno.env.get("DEEPSEEK_RETRY_BASE_MS") || "1500");

function llmSleepAbortable(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

/**
 * Ruft DeepSeek /chat/completions auf. Retry mit exponentiellem Backoff bei
 * 429/5xx/Netzwerkfehlern; 401/402/403 werfen sofort (Auth/Billing).
 */
export async function deepseekChat(
  payload: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<any> {
  if (!DEEPSEEK_API_KEY) {
    throw new LlmUnavailableError("DEEPSEEK_API_KEY ist nicht gesetzt", false);
  }
  const url = `${DEEPSEEK_BASE_URL}/chat/completions`;
  let lastErr = "unbekannter Fehler";

  for (let attempt = 0; attempt <= DEEPSEEK_MAX_RETRIES; attempt++) {
    let res: Response | null = null;
    try {
      res = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${DEEPSEEK_API_KEY}`,
        },
        body: JSON.stringify(payload),
        signal,
      });
    } catch (e: any) {
      if (e?.name === "AbortError") throw e;
      lastErr = `Netzwerkfehler: ${e?.message || e}`;
      if (attempt < DEEPSEEK_MAX_RETRIES) {
        await llmSleepAbortable(DEEPSEEK_RETRY_BASE_MS * 2 ** attempt, signal);
        continue;
      }
      throw new LlmUnavailableError(lastErr, true);
    }

    if (res.ok) return await res.json();

    const body = await res.text().catch(() => "");
    if (res.status === 401 || res.status === 402 || res.status === 403) {
      throw new LlmUnavailableError(`DeepSeek HTTP ${res.status}: ${body}`, false);
    }
    if (res.status === 429 || res.status >= 500) {
      lastErr = `DeepSeek HTTP ${res.status}: ${body}`;
      const retryAfterS = Number(res.headers.get("retry-after")) || 0;
      const wait = Math.max(retryAfterS * 1000, DEEPSEEK_RETRY_BASE_MS * 2 ** attempt);
      if (attempt < DEEPSEEK_MAX_RETRIES) {
        log.warn(`[DeepSeek] ${lastErr} — Retry ${attempt + 1}/${DEEPSEEK_MAX_RETRIES} in ${Math.round(wait)}ms`);
        await llmSleepAbortable(wait, signal);
        continue;
      }
      throw new LlmUnavailableError(lastErr, true);
    }
    throw new LlmUnavailableError(`DeepSeek HTTP ${res.status}: ${body}`, false);
  }

  throw new LlmUnavailableError(lastErr, true);
}

/**
 * Uebersetzt den Reasoning-Modus in die DeepSeek-Wire-Form:
 *   off          -> { thinking: { type: "disabled" } }
 *   low|high|max -> { thinking: { type: "enabled" }, reasoning_effort }
 * Quelle: @deepseek-ai/dsh-llm-deepseek (resolveThinking).
 */
function deepseekThinkingPayload(effort: string): Record<string, unknown> {
  switch (effort) {
    case "off":
    case "disabled":
      return { thinking: { type: "disabled" } };
    case "low":
    case "high":
    case "max":
      return { thinking: { type: "enabled" }, reasoning_effort: effort };
    default:
      return {};
  }
}

/** Bequemer Chat-Helfer: liefert den Text der ersten Assistant-Nachricht. */
export async function deepseekChatCompletion(
  messages: Array<Record<string, unknown>>,
  opts: { model?: string; temperature?: number; maxTokens?: number; reasoningEffort?: string; signal?: AbortSignal } = {},
): Promise<string> {
  const effort = (opts.reasoningEffort ?? DEEPSEEK_REASONING_EFFORT).toLowerCase();
  const data = await deepseekChat({
    model: opts.model || DEEPSEEK_MODEL,
    messages,
    temperature: opts.temperature ?? 0.1,
    ...deepseekThinkingPayload(effort),
    ...(opts.maxTokens ? { max_tokens: opts.maxTokens } : {}),
  }, opts.signal);
  const usage = data?.usage;
  if (usage) {
    const reasoning = usage.completion_tokens_details?.reasoning_tokens;
    log.debug(
      `[DeepSeek] model=${opts.model || DEEPSEEK_MODEL} effort=${effort || "default"} ` +
      `tokens=${usage.total_tokens} (prompt=${usage.prompt_tokens}, completion=${usage.completion_tokens}${reasoning ? `, reasoning=${reasoning}` : ""})`,
    );
  }
  return data?.choices?.[0]?.message?.content || "";
}

/** Laedt den Metadaten-Prompt; identische Suchreihenfolge wie zuvor (Switchyard-Version). */
function loadMetadataPrompt(): string {
  const fallback = "Extract metadata from the user's captured thought. Return ONLY valid JSON.";
  const promptPaths = [
    "/app/prompts/metadata-prompt.txt",
    "/app/metadata-prompt.txt",
    "prompts/metadata-prompt.txt",
    "metadata-prompt.txt",
  ];
  for (const p of promptPaths) {
    try {
      return Deno.readTextFileSync(p);
    } catch (_e) {
      // naechster Pfad
    }
  }
  return fallback;
}

/**
 * Metadaten-Extraktion ueber DeepSeek. Liefert bei Fehlern ein Objekt mit
 * _extraction_failed=true, _extraction_error und _retryable, damit der Worker
 * Backoff/Retry und den Circuit Breaker korrekt steuern kann.
 */
export async function extractMetadata(text: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
  const systemPrompt = loadMetadataPrompt();
  try {
    const content = await deepseekChatCompletion(
      [{ role: "system", content: systemPrompt }, { role: "user", content: text }],
      { signal, temperature: 0.1 },
    );

    let parsed: any = null;
    let parseError = "invalid JSON";
    try {
      const jsonMatch = content.match(/\{[\s\S]*\}/);
      parsed = JSON.parse(jsonMatch ? jsonMatch[0] : content);
    } catch (e: any) {
      parseError = e.message;
    }

    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      log.warn(`[extractMetadata] DeepSeek-Antwort nicht als Objekt parsebar (${parseError}).`);
      return {
        topics: ["uncategorized"],
        type: "observation",
        _extraction_failed: true,
        _extraction_error: `parse: ${parseError}`,
        _retryable: true,
      };
    }
    return parsed;
  } catch (e: any) {
    if (e?.name === "AbortError") throw e;
    const retryable = !(e instanceof LlmUnavailableError) || e.retryable;
    log.error(`[extractMetadata] DeepSeek (model=${DEEPSEEK_MODEL}) failed: ${e?.message || e}`);
    return {
      topics: ["uncategorized"],
      type: "observation",
      _extraction_failed: true,
      _extraction_error: String(e?.message || e),
      _retryable: retryable,
    };
  }
}

/** Vision/OCR ueber DeepSeek (OpenAI-kompatibles Bildformat). imageUrl = data:- oder http(s)-URL. */
export async function deepseekVisionExtract(
  imageUrl: string,
  prompt: string,
  signal?: AbortSignal,
): Promise<string> {
  return await deepseekChatCompletion(
    [{
      role: "user",
      content: [
        { type: "text", text: prompt },
        { type: "image_url", image_url: { url: imageUrl } },
      ],
    }],
    { model: DEEPSEEK_VISION_MODEL, temperature: 0.1, maxTokens: 4096, signal },
  );
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
