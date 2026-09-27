import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import {
  supabase,
  log,
  getQueryEmbedding,
  getDocumentEmbedding,
  X_BEARER_TOKEN,
  X_CLIENT_ID,
  AGENT_ID,
  GLOBAL_BRAIN_ACCESS,
  resolveAuthorHandles,
  getXOAuthTokens,
  getValidXUserAccessToken,
  throttledXFetch,
  twitterApiIoFetch,
  isTwitterApiIoAvailable,
  X_INITIAL_BACKFILL_LIMIT,
  X_INITIAL_SYNC_CONCURRENCY,
  deepseekChatCompletion,
  upsertOpenBrainEntry,
  openBrainRef,
  interactiveBudgetStatus,
  X_TIER2_MIN_POSTS,
  X_TIER2_MAX_AUTHORS,
  X_TIER2_MAX_POSTS,
  X_TIER2_WINDOW_DAYS,
} from "./shared.ts";
import {
  activeSyncControllers,
  ingestInfluencerTweets,
  recordSyncLog,
} from "../workers/x_ingestion_worker.ts";

function formatSearchResults(data: any[], returnMode: string) {
  if (returnMode === "ids_only") {
    return data.map((t: any, i: number) => `[${i + 1}] ID: ${t.id} | Date: ${new Date(t.created_at).toLocaleDateString()}`).join("\n");
  } else if (returnMode === "full_text") {
    return data.map((t: any, i: number) => `[${i + 1}] ID: ${t.id} | Agent: ${t.agent_id} | Type: ${t.artifact_type} | Date: ${new Date(t.created_at).toLocaleDateString()}\nContent: ${t.content}\nMetadata: ${JSON.stringify(t.metadata)}`).join("\n\n");
  } else {
    // "snippets" = kompakter Modus (voller Post-Text, ohne Metadata-JSON).
    // Frueher wurde der Text hier auf 150 Zeichen gekuerzt; das hat bei 39 %
    // der Posts Information (Cashtags/Level am Textende) verworfen und pro
    // Aufruf nur ~35 Tokens/Post gespart. Kostensteuerung gehoert an
    // `limit` und `return_mode: "ids_only"`, nicht an eine Textkuerzung.
    return data.map((t: any, i: number) => {
      const content = t.content || "";
      const author = t.metadata?.author || "Unknown";
      return `[${i + 1}] ID: ${t.id} | Date: ${new Date(t.created_at).toLocaleDateString()} | Author: ${author}\nContent: ${content}`;
    }).join("\n\n");
  }
}

// =====================================================================
// Live-Ticker-Suche auf X (advanced_search) – read-only, schreibt nichts in die DB
// =====================================================================

export interface TickerSearchOptions {
  ticker: string;
  limit?: number;
  min_likes?: number;
  min_retweets?: number;
  min_followers?: number;
  /** Festes Fenster in Tagen (0 = heute). undefined = automatische Leiter. */
  max_age_days?: number | null;
  exclude_replies?: boolean;
  exclude_known?: boolean;
  /** Max. Treffer pro Autor (0 = unbegrenzt). Verhindert, dass ein "Listen-Poster" das Ergebnis dominiert. */
  max_per_author?: number;
  /** Max. Cashtags im Post-Text (0 = unbegrenzt). Filtert Sammel-/Listen-Posts. */
  max_cashtags?: number;
  /** Krypto-Token-Spam (Contract-Address im Text) ausschließen (default: true). */
  exclude_crypto_spam?: boolean;
  sort?: "engagement" | "recent";
  page_budget?: number;
  timezone?: string;
}

export interface TickerSearchStats {
  ticker: string;
  windows_tried: string[];
  window_used: string;
  query: string;
  pages: number;
  fetched: number;
  credits: number;
  candidates: number;
  after_filters: number;
  returned: number;
  known_in_pool: number;
  today_count: number;
  budget_exhausted: boolean;
  authors: number;
  passes: number;
  prefilter_relaxed: boolean;
  spam_filtered: number;
  cashtag_filtered: number;
}

const TICKER_CREDITS_PER_TWEET = 15;
// Feiner gestufte Leiter: bevorzugt Aktualität, bevor teuer in die Vergangenheit gegangen wird.
const TICKER_LADDER: (number | null)[] = [0, 7, 30, 90, 365, null];
const TICKER_QUERY_MAX_CHARS = 500;
// Zusatz-Seiten für den zweiten Durchlauf ohne min_faves/min_retweets (Recall-Fix).
const TICKER_RETRY_PAGES = 3;
const TICKER_CASHTAG_RE = /\$[A-Za-z][A-Za-z0-9._-]{0,9}/g;
// Krypto-Token-Promo: Contract-Address, Chain-Präfix oder typische Launch-Vokabeln.
const TICKER_CONTRACT_RE = /0x[a-fA-F0-9]{16,}|(?:ethereum|solana|bsc|polygon|arbitrum|base):\s*0x|(?:airdrop|presale|tokenomics|contract address|staking rewards)/i;

/** Anzahl der Cashtags im Text – hohe Werte deuten auf Sammel-/Listen-Posts. */
export function countCashtags(text: string): number {
  const m = String(text || "").match(TICKER_CASHTAG_RE);
  return m ? m.length : 0;
}

/** Krypto-Token-Promo erkennen (Contract-Address / Chain-Präfix / Launch-Vokabular). */
export function looksLikeCryptoSpam(tweet: any): boolean {
  return TICKER_CONTRACT_RE.test(String(tweet?.text || ""));
}

/** Mitternacht (00:00) der angegebenen Zeitzone als Unix-Sekunden. */
export function localMidnightEpoch(timeZone = "Europe/Berlin"): number {
  const now = new Date();
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).formatToParts(now);
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value || 0);
  const asUtc = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
  const offsetMs = asUtc - now.getTime();
  const midnightUtc = Date.UTC(get("year"), get("month") - 1, get("day"));
  return Math.floor((midnightUtc - offsetMs) / 1000);
}

function tickerWindowLabel(days: number | null): string {
  if (days === null) return "all-time";
  if (days === 0) return "heute";
  return `${days} Tage`;
}

function formatCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n || 0);
}

/**
 * Live-Suche nach einem Cashtag mit Qualitätsfiltern und automatischer Fenster-Weitung.
 * Billing: 15 Credits pro geliefertem Tweet (100.000 Credits = $1).
 */
export async function searchTickerOnline(
  opts: TickerSearchOptions,
): Promise<{ text: string; results: any[]; stats: TickerSearchStats }> {
  if (!isTwitterApiIoAvailable()) {
    throw new Error("TWITTER_API_IO_KEY fehlt – Live-Suche auf X nicht möglich.");
  }

  const ticker = String(opts.ticker || "").trim().toUpperCase().replace(/^\$/, "");
  if (!ticker) throw new Error("Kein Ticker angegeben.");

  const limit = Math.max(1, opts.limit ?? 20);
  const minLikes = Math.max(0, opts.min_likes ?? 0);
  const minRetweets = Math.max(0, opts.min_retweets ?? 0);
  const minFollowers = Math.max(0, opts.min_followers ?? 0);
  const excludeReplies = opts.exclude_replies !== false;
  const excludeKnown = opts.exclude_known !== false;
  const maxPerAuthor = Math.max(0, opts.max_per_author ?? 0);
  const maxCashtags = Math.max(0, opts.max_cashtags ?? 0);
  const excludeSpam = opts.exclude_crypto_spam !== false;
  const sort = opts.sort === "recent" ? "recent" : "engagement";
  const pageBudget = Math.max(1, opts.page_budget ?? 6);
  const midnight = localMidnightEpoch(opts.timezone || "Europe/Berlin");

  const ladder: (number | null)[] = (opts.max_age_days === undefined || opts.max_age_days === null)
    ? TICKER_LADDER
    : [opts.max_age_days];

  // X' min_faves/min_retweets arbeiten mit veralteten Index-Werten. Deshalb:
  // API-Vorfilter ~30 % unter der Wunschgrenze + exakte Prüfung clientseitig.
  const apiMinLikes = minLikes >= 3 ? Math.floor(minLikes * 0.7) : 0;
  const apiMinRetweets = minRetweets >= 3 ? Math.floor(minRetweets * 0.7) : 0;

  const queryParts = [`$${ticker}`];
  if (apiMinLikes > 0) queryParts.push(`min_faves:${apiMinLikes}`);
  if (apiMinRetweets > 0) queryParts.push(`min_retweets:${apiMinRetweets}`);
  if (excludeReplies) queryParts.push("-filter:replies");
  const baseQuery = queryParts.join(" ");

  const pool = new Map<string, any>();
  const known = new Set<string>();
  const windowsTried: string[] = [];
  const windowsSeen = new Set<string>();
  let windowUsed = tickerWindowLabel(ladder[0]);
  let pages = 0;
  let fetched = 0;
  let knownInPool = 0;
  let usedQuery = baseQuery;
  let prefilterRelaxed = false;

  const isCryptoSpam = (t: any) => excludeSpam && looksLikeCryptoSpam(t);
  const tooManyCashtags = (t: any) => maxCashtags > 0 && countCashtags(t.text) > maxCashtags;

  const passes = (t: any): boolean => {
    if (excludeKnown && known.has(String(t.id))) return false;
    if ((t.likeCount || 0) < minLikes) return false;
    if ((t.retweetCount || 0) < minRetweets) return false;
    if (((t.author?.followers) || 0) < minFollowers) return false;
    if (excludeReplies && t.isReply) return false;
    if (isCryptoSpam(t)) return false;
    if (tooManyCashtags(t)) return false;
    return true;
  };
  const sortedPool = () => [...pool.values()].filter(passes).sort((a, b) => {
    if (sort === "recent") return Date.parse(b.createdAt || "") - Date.parse(a.createdAt || "");
    const sa = (a.likeCount || 0) + 2 * (a.retweetCount || 0);
    const sb = (b.likeCount || 0) + 2 * (b.retweetCount || 0);
    if (sb !== sa) return sb - sa;
    return Date.parse(b.createdAt || "") - Date.parse(a.createdAt || "");
  });

  /** Begrenzt Treffer pro Autor, damit ein einzelner "Listen-Poster" nicht alles dominiert. */
  const selectDiverse = (list: any[], want: number): any[] => {
    if (maxPerAuthor <= 0) return list.slice(0, want);
    const perAuthor = new Map<string, number>();
    const out: any[] = [];
    for (const r of list) {
      const a = String(r.author?.userName || "?").toLowerCase();
      const n = perAuthor.get(a) || 0;
      if (n >= maxPerAuthor) continue;
      perAuthor.set(a, n + 1);
      out.push(r);
      if (out.length >= want) break;
    }
    return out;
  };

  const enough = () => selectDiverse(sortedPool(), limit).length >= limit;

  /**
   * Ein Durchlauf über die Fensterleiter. `budgetPages` ist die harte Seitengrenze
   * (jede Seite = bis zu 20 Tweets = bis zu 300 Credits).
   */
  const walk = async (queryBase: string, budgetPages: number): Promise<boolean> => {
    for (const days of ladder) {
      if (pages >= budgetPages) break;
      const since = days === null ? null : midnight - days * 86400;
      const query = since ? `${queryBase} since_time:${since}` : queryBase;
      usedQuery = query;
      const label = tickerWindowLabel(days);
      if (!windowsSeen.has(label)) {
        windowsSeen.add(label);
        windowsTried.push(label);
      }
      windowUsed = label;

      let cursor: string | undefined;
      while (pages < budgetPages) {
        const json = await twitterApiIoFetch("tweet/advanced_search", { query, queryType: "Top", cursor }, { interactive: true });
        pages++;
        const tweets: any[] = json.tweets || [];
        fetched += tweets.length;

        // DB-Dedupe: neue IDs einmalig gegen agent_workspace prüfen
        if (excludeKnown && tweets.length > 0) {
          const fresh = tweets.map((t) => String(t.id)).filter((id) => !known.has(id));
          if (fresh.length > 0) {
            const { data, error } = await supabase
              .from("agent_workspace")
              .select("metadata")
              .in("metadata->>external_id", fresh);
            if (error) {
              log.warn(`[Ticker-Suche] DB-Dedupe fehlgeschlagen: ${error.message}`);
            } else {
              for (const row of data || []) {
                const id = (row as any)?.metadata?.external_id;
                if (id) { known.add(String(id)); knownInPool++; }
              }
            }
          }
        }

        for (const t of tweets) {
          const id = String(t.id);
          if (!pool.has(id)) pool.set(id, t);
        }

        if (enough()) return true;
        if (!(json.has_next_page && json.next_cursor)) break;
        cursor = json.next_cursor;
      }

      if (enough()) return true;
    }
    return enough();
  };

  let done = await walk(baseQuery, pageBudget);

  // Recall-Fix: X' min_faves/min_retweets-Vorfilter arbeitet mit veralteten
  // Index-Werten und versteckt dadurch Posts, die die Schwelle aktuell erfüllen.
  // Wenn zu wenig Treffer zusammenkommen, zweiter Durchlauf OHNE diese Operatoren
  // (die exakte Prüfung läuft ohnehin clientseitig).
  if (!done && (apiMinLikes > 0 || apiMinRetweets > 0)) {
    prefilterRelaxed = true;
    const relaxedParts = [`$${ticker}`];
    if (excludeReplies) relaxedParts.push("-filter:replies");
    log.info(`[Ticker-Suche] $${ticker}: Vorfilter lieferte zu wenige Treffer – zweiter Durchlauf ohne min_faves/min_retweets.`);
    done = await walk(relaxedParts.join(" "), pageBudget + TICKER_RETRY_PAGES);
  }

  const pageLimitUsed = prefilterRelaxed ? pageBudget + TICKER_RETRY_PAGES : pageBudget;
  const budgetExhausted = !done && pages >= pageLimitUsed;

  const poolArr = [...pool.values()];
  const spamFiltered = poolArr.filter(isCryptoSpam).length;
  const cashtagFiltered = poolArr.filter((t) => !isCryptoSpam(t) && tooManyCashtags(t)).length;
  const allMatches = sortedPool();
  const results = selectDiverse(allMatches, limit);

  const credits = fetched * TICKER_CREDITS_PER_TWEET;
  const todayCount = results.filter((r) => Date.parse(r.createdAt || "") / 1000 >= midnight).length;
  const stamps = results.map((r) => Date.parse(r.createdAt || "")).filter((d) => !Number.isNaN(d));
  const oldest = stamps.length ? new Date(Math.min(...stamps)) : null;

  const filterBits: string[] = [];
  if (minLikes > 0) filterBits.push(`≥${minLikes} Likes${apiMinLikes > 0 ? ` (API-Vorfilter ${apiMinLikes})` : ""}`);
  if (minRetweets > 0) filterBits.push(`≥${minRetweets} Retweets`);
  if (minFollowers > 0) filterBits.push(`≥${formatCompact(minFollowers)} Follower`);
  if (excludeReplies) filterBits.push("ohne Replies");
  if (excludeKnown) filterBits.push("ohne DB-Posts");
  if (maxPerAuthor > 0) filterBits.push(`max. ${maxPerAuthor} pro Autor`);
  if (maxCashtags > 0) filterBits.push(`max. ${maxCashtags} Cashtags`);
  if (excludeSpam) filterBits.push("ohne Krypto-Spam");

  const hints: string[] = [];
  if (prefilterRelaxed) {
    hints.push("Recall-Fix aktiv: zweiter Durchlauf ohne min_faves/min_retweets, weil X' Index-Werte veraltet sind – die Likes/RT-Schwellen wurden exakt geprüft.");
  }
  if (ticker.length <= 2) {
    hints.push(`Kurzer Cashtag: $${ticker} wird auch von anderen Projekten/Coins verwendet – \`min_followers\` filtert Fremdtreffer zuverlässig.`);
  }
  if (/^[0-9]/.test(ticker) || ticker.length > 5) {
    hints.push("Hinweis: X' Cashtag-Suche funktioniert praktisch nur für US-Symbole – 0 Treffer bei Auslands-/Nummern-Tickern sind normal.");
  }
  if (results.length > 0 && results.length < limit && !budgetExhausted) {
    hints.push(`${allMatches.length} Posts erfüllen die Filter insgesamt – mehr gibt es im weitesten Fenster nicht.`);
  }

  const lines: string[] = [];
  lines.push(`### $${ticker} — ${results.length}${results.length < limit ? ` von max. ${limit}` : ""} Posts`);
  lines.push(`Fenster: ${windowUsed}${windowsTried.length > 1 ? ` (versucht: ${windowsTried.join(" → ")})` : ""} | ${todayCount} von heute, ${results.length - todayCount} älter${oldest ? ` (ältester ${oldest.toLocaleDateString("de-DE", { timeZone: "Europe/Berlin" })})` : ""}`);
  lines.push(`Filter: ${filterBits.length ? filterBits.join(", ") : "keine"} | Kandidaten: ${pool.size} → nach Filtern: ${allMatches.length}${knownInPool ? ` (davon ${knownInPool} schon in der DB)` : ""}${spamFiltered ? ` | Krypto-Spam verworfen: ${spamFiltered}` : ""}${cashtagFiltered ? ` | zu viele Cashtags: ${cashtagFiltered}` : ""}`);
  lines.push(`Kosten: ${pages} Seiten, ${fetched} Tweets geladen = ~${credits} Credits (~$${(credits / 100000).toFixed(4)})`);
  if (results.length < limit) {
    lines.push(budgetExhausted
      ? `⚠️ Seitenbudget (${pageLimitUsed}) erreicht – es können noch mehr Treffer existieren. Mit höherem page_budget erneut suchen.`
      : `⚠️ Es gibt nicht ${limit} Posts, die alle Filter erfüllen (alle Fenster bis ${windowUsed} ausgeschöpft). Schwellen senken oder Filter lockern.`);
  }
  for (const h of hints) lines.push(`ℹ️ ${h}`);
  lines.push("");

  results.forEach((r, i) => {
    const author = r.author?.userName || "?";
    const followers = r.author?.followers || 0;
    const parsed = Date.parse(r.createdAt || "");
    const when = Number.isNaN(parsed)
      ? "?"
      : new Date(parsed).toLocaleString("de-DE", { timeZone: "Europe/Berlin", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    const text = String(r.text || "").replace(/\s+/g, " ").trim();
    const tags = countCashtags(r.text);
    lines.push(`${i + 1}. ❤ ${r.likeCount || 0} · 🔁 ${r.retweetCount || 0} · 💬 ${r.replyCount || 0} · 👁 ${formatCompact(r.viewCount || 0)}${tags > 1 ? ` · 🏷 ${tags}` : ""} | @${author} (${formatCompact(followers)} Follower) | ${when}`);
    lines.push(`   ${text.length > 240 ? text.slice(0, 240) + "…" : text}`);
    if (r.url) lines.push(`   ${r.url}`);
    lines.push("");
  });

  const stats: TickerSearchStats = {
    ticker,
    windows_tried: windowsTried,
    window_used: windowUsed,
    query: usedQuery,
    pages,
    fetched,
    credits,
    candidates: pool.size,
    after_filters: allMatches.length,
    returned: results.length,
    known_in_pool: knownInPool,
    today_count: todayCount,
    budget_exhausted: budgetExhausted,
    authors: new Set(results.map((r) => String(r.author?.userName || "?").toLowerCase())).size,
    passes: prefilterRelaxed ? 2 : 1,
    prefilter_relaxed: prefilterRelaxed,
    spam_filtered: spamFiltered,
    cashtag_filtered: cashtagFiltered,
  };

  return { text: lines.join("\n"), results, stats };
}

// =====================================================================
// Influencer-Profil (OpenBrain) + Favoriten-Helfer
// =====================================================================

/** Handles, die im Regel-Sync laufen (Tier 1). Format: "@name", lowercase. */
export async function getFavoriteHandles(): Promise<string[]> {
  const { data } = await supabase
    .from("x_users")
    .select("username")
    .eq("is_active", true)
    .eq("favorite", true);
  return (data || []).map((u: any) => `@${String(u.username).toLowerCase()}`);
}

/**
 * Leitet aus den gespeicherten Posts ein Themenprofil ab: Top-Ticker, Top-Keywords
 * und ein kurzer LLM-Text ("gut bei den Themen ..."). Ohne Posts bleibt es bei
 * einem Startprofil, das der Nutzer per MCP ergänzen kann.
 */
export async function generateInfluencerProfile(handleInput: string): Promise<string> {
  const handle = handleInput.replace(/^@/, "").toLowerCase();
  const { data: user } = await supabase
    .from("x_users")
    .select("username, screen_name, notes")
    .eq("username", handle)
    .single();

  const since = new Date(Date.now() - 30 * 86400000).toISOString();
  const { data: posts } = await supabase
    .from("agent_workspace")
    .select("content, metadata, created_at")
    .eq("artifact_type", "x_post")
    .or(`metadata->>author.eq.@${handle},metadata->>author.eq.${handle}`)
    .gte("created_at", since)
    .order("created_at", { ascending: false })
    .limit(120);

  const list = (posts || []) as any[];
  const tickerCount = new Map<string, number>();
  const keywordCount = new Map<string, number>();
  for (const p of list) {
    for (const t of (p.metadata?.tickers || [])) {
      const k = String(t).toUpperCase();
      tickerCount.set(k, (tickerCount.get(k) || 0) + 1);
    }
    for (const k of (p.metadata?.keywords || [])) {
      const key = String(k);
      keywordCount.set(key, (keywordCount.get(key) || 0) + 1);
    }
  }
  const top = (m: Map<string, number>, n: number) =>
    [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, n).map(([k, c]) => `${k} (${c}x)`).join(", ");

  const screenName = user?.screen_name || handle;
  const notes = user?.notes || "";
  const topTickers = top(tickerCount, 15);
  const topKeywords = top(keywordCount, 15);

  const header = `Influencer @${handle} (${screenName})` +
    (notes ? `\nEinordnung: ${notes}` : "");

  if (list.length < 5) {
    return `${header}\nThemenprofil: noch zu wenig Daten (${list.length} Posts im Korpus).` +
      `\nBitte ergänzen: Themenfelder, Stärken, typische Ticker.`;
  }

  const sample = list.slice(0, 25)
    .map((p) => `- ${String(p.content || "").replace(/\s+/g, " ").slice(0, 220)}`)
    .join("\n");

  const prompt =
    `Handle: @${handle}\nName: ${screenName}\nNotizen: ${notes || "(keine)"}\n` +
    `Häufige Ticker: ${topTickers || "(keine)"}\nHäufige Keywords: ${topKeywords || "(keine)"}\n\n` +
    `Beispielposts (neueste zuerst):\n${sample}`;

  let summary = "";
  try {
    summary = (await deepseekChatCompletion([
      {
        role: "system",
        content:
          "Du beschreibst Finanz-Influencer für ein Aktien-Recherche-System. Antworte auf Deutsch, " +
          "3-5 sätze, sachlich, ohne Werbung. Nenne: (1) Themenfelder/Stärken, (2) typische Ticker oder Branchen, " +
          "(3) Stil der Posts (z. B. Makro, Options-Flow, Einzelaktien-Analyse, News-Aggregator, Threads). " +
          "Keine Anlageempfehlung, keine Bewertung der Person.",
      },
      { role: "user", content: prompt },
    ], { temperature: 0.2, maxTokens: 450 })).trim();
  } catch (e: any) {
    log.warn(`[Influencer-Profil] LLM-Zusammenfassung für @${handle} fehlgeschlagen: ${e.message}`);
  }

  const parts = [header];
  if (summary) parts.push(`Themenprofil: ${summary}`);
  if (topTickers) parts.push(`Häufige Ticker: ${topTickers}`);
  if (topKeywords) parts.push(`Häufige Keywords: ${topKeywords}`);
  parts.push(`Datenbasis: ${list.length} Posts der letzten 30 Tage.`);
  return parts.join("\n");
}

/**
 * Schreibt das Influencer-Profil in den OpenBrain und verlinkt es in x_users
 * (description = openbrain://<id>, brain_ref, brain_content, embedding).
 * Ein vorhandener Eintrag wird soft-deleted, damit nichts dupliziert.
 */
export async function syncInfluencerBrainProfile(
  handleInput: string,
  opts: { refreshFromPosts?: boolean; contentOverride?: string } = {},
): Promise<{ ref: string | null; content: string }> {
  const handle = handleInput.replace(/^@/, "").toLowerCase();
  const { data: user } = await supabase
    .from("x_users")
    .select("username, screen_name, notes, brain_ref")
    .eq("username", handle)
    .single();

  const content = opts.contentOverride
    ?? (opts.refreshFromPosts ? await generateInfluencerProfile(handle) : "");

  if (!content) return { ref: user?.brain_ref || null, content: "" };

  const id = await upsertOpenBrainEntry(content, "observation");
  const oldRef = user?.brain_ref as string | undefined;
  if (oldRef && id && oldRef !== id) {
    await supabase.from("open_brain").update({ deleted_at: new Date().toISOString() }).eq("id", oldRef);
  }

  const embedText = `username: @${handle} screen_name: ${user?.screen_name || ""} beschreibung: ${content}`;
  const embedding = await getDocumentEmbedding(embedText).catch(() => null);

  await supabase.from("x_users").update({
    description: id ? openBrainRef(id) : null,
    brain_ref: id,
    brain_content: content,
    ...(embedding && embedding.length ? { embedding } : {}),
  }).eq("username", handle);

  return { ref: id, content };
}

/** Alle getrackten Handles (is_active) als lowercase-Set – für Tier 2. */
export async function getTrackedHandles(): Promise<Set<string>> {
  const { data } = await supabase.from("x_users").select("username").eq("is_active", true);
  return new Set((data || []).map((u: any) => String(u.username).toLowerCase()));
}

/** Favoriten-Status + Profil für die Anzeige. */
function formatInfluencerRow(u: any, idx: number): string {
  const star = u.favorite ? "⭐" : "  ";
  const desc = (u.brain_content || "").replace(/\s+/g, " ").slice(0, 140);
  return `${idx + 1}. ${star} @${u.username} (${u.screen_name || "N/A"})` +
    (u.notes ? ` – ${u.notes}` : "") +
    (u.description ? `\n     brain: ${u.description}` : "") +
    (desc ? `\n     ${desc}…` : "");
}

export function registerXTools(server: McpServer) {
  // 1. Tool: Search Influencer Posts
  server.registerTool(
    "search_influencer_posts",
    {
      title: "Search Influencer Posts",
      description: "Search stored influencer posts in the database using hybrid semantic or exact keyword search.\n\n" +
        "WHEN TO USE: Use when searching for posts discussing specific topics, tickers (e.g. '$NVDA', 'breakout'), trade setups, or sentiment across monitored influencers.\n" +
        "WHEN NOT TO USE: To simply browse chronological timeline feeds (single or multiple influencers, or specific dates) or fetch a single tweet by ID, use `show_x_content`.",
      inputSchema: {
        action: z.enum(["READ", "READ_IDS"]).default("READ").describe("READ = search posts, READ_IDS = fetch specific posts by ID array"),
        tier: z.number().min(1).max(3).optional().default(2).describe(
          "1 = nur offline (nur ⭐ Favoriten, 0 Credits). " +
          "2 = DB aller Getrackten + automatisches, gezieltes Live-Nachladen, wenn die Datenlage dünn ist (nur mit Begründung). " +
          "3 = zusätzlich freie Live-Suche auf X (Thema/Ticker), auch außerhalb der überwachten Accounts.",
        ),
        query: z.string().optional().describe("Search query (ticker, topic, keyword). Leave empty to list recent posts."),
        limit: z.number().optional().default(200).describe("Max results (default: 200)"),
        threshold: z.number().optional().default(0.5).describe("Similarity threshold for semantic search (default: 0.5)"),
        artifact_type: z.string().optional().default("x_post").describe("Filter by artifact type (default: 'x_post')"),
        days_back: z.number().optional().describe("Filter posts from the last X days"),
        return_mode: z.enum(["ids_only", "snippets", "full_text"]).optional().default("snippets").describe("Return format for READ (default: snippets = full post text without metadata)"),
        ids: z.array(z.string()).optional().describe("Array of post IDs (for READ_IDS only)"),
        authors: z.array(z.string()).optional().describe("Filter to specific influencers/authors (e.g. ['@serenity'])"),
        ...(GLOBAL_BRAIN_ACCESS ? { owner: z.string().optional().describe("Filter by agent ID.") } : {}),
      },
    },
    async ({ action, query, limit, threshold, artifact_type, days_back, return_mode, ids, authors, owner, tier: tierRaw }: any) => {
      try {
        const p_agent_id = GLOBAL_BRAIN_ACCESS ? (owner || null) : AGENT_ID;

        if (action === "READ_IDS") {
          if (!ids || ids.length === 0) return { content: [{ type: "text", text: "No IDs provided." }] };

          const CHUNK_SIZE = 50;
          const chunks: string[][] = [];
          for (let i = 0; i < ids.length; i += CHUNK_SIZE) {
            chunks.push(ids.slice(i, i + CHUNK_SIZE));
          }

          const results = await Promise.all(
            chunks.map(async (chunk) => {
              const { data, error } = await supabase.from("agent_workspace").select("*").in("id", chunk);
              if (error) throw error;
              return data || [];
            })
          );

          const data = results.flat();
          if (!data || data.length === 0) return { content: [{ type: "text", text: "No posts found for IDs." }] };
          const resultsText = data.map((t: any, i: number) => `[${i + 1}] ID: ${t.id} | Date: ${new Date(t.created_at).toLocaleDateString()}\nContent: ${t.content}\nMetadata: ${JSON.stringify(t.metadata)}`);
          return { content: [{ type: "text", text: resultsText.join("\n\n") }] };
        }

        const actual_query = query || "";
        const trimmedQuery = actual_query.trim();

        let expandedAuthors: string[] | null = null;
        const expandedHandlesSet = new Set<string>();
        if (authors && authors.length > 0) {
          for (const author of authors) {
            const { allHandles } = await resolveAuthorHandles(author);
            allHandles.forEach(h => expandedHandlesSet.add(h.toLowerCase()));
          }
          expandedAuthors = Array.from(expandedHandlesSet);
        }

        // ---------------------------------------------------------------
        // Tier-Logik
        //  1 = nur offline, nur Favoriten        (0 Credits)
        //  2 = DB (alle Getrackten) + gezieltes Live-Nachladen bei dünner
        //      Datenlage – passiert automatisch, aber nur mit Begründung
        //  3 = zusätzlich freie Live-Suche auf X (Thema/Ticker)
        // ---------------------------------------------------------------
        const tier = Math.min(3, Math.max(1, Number(tierRaw ?? 2)));
        const favoriteHandles = await getFavoriteHandles();
        const favoriteSet = new Set(favoriteHandles.map((h) => h.toLowerCase()));
        // Nur echte Handles (resolveAuthorHandles liefert auch Screen-Names mit Leerzeichen).
        const requestedHandles = expandedAuthors
          ? [...new Set(
              expandedAuthors
                .map((h) => h.replace(/^@/, "").toLowerCase())
                .filter((h) => /^[a-z0-9_]{1,15}$/.test(h)),
            )]
          : [];

        let scopeAuthors = expandedAuthors;
        let tierNote = "";
        if (tier === 1) {
          if (expandedAuthors) {
            const allowed = expandedAuthors.filter((h) => favoriteSet.has(`@${h.replace(/^@/, "").toLowerCase()}`));
            scopeAuthors = [...new Set(allowed)];
            if (scopeAuthors.length === 0) {
              return {
                content: [{
                  type: "text",
                  text: `🔎 Tier 1 (offline, nur ⭐ Favoriten): Für ${requestedHandles.map((h) => "@" + h).join(", ")} liegen keine Favoriten-Daten vor.\n` +
                    `Tier 2 würde die Posts dieser Accounts gezielt live nachladen (kostet Credits).`,
                }],
              };
            }
          } else {
            scopeAuthors = favoriteHandles;
          }
          tierNote = `Tier 1 (offline, ${scopeAuthors?.length ?? 0} ⭐ Favoriten) · 0 Credits`;
        } else {
          tierNote = `Tier ${tier} (DB: alle Getrackten${tier === 3 ? " + Live-Suche auf X" : ""})`;
        }

        // --- Tier 2: gezieltes Live-Nachladen, aber nur mit Begründung ---
        // Begründung = dünne Datenlage: für die angefragten Accounts liegen im
        // Fenster weniger als X_TIER2_MIN_POSTS vor (oder gar keine).
        const fetchedAuthors: string[] = [];
        const skippedUntracked: string[] = [];
        let tier2Credits = 0;
        if (tier >= 2 && requestedHandles.length > 0) {
          const windowDays = days_back && days_back > 0 ? days_back : X_TIER2_WINDOW_DAYS;
          const sinceIso = new Date(Date.now() - windowDays * 86400000).toISOString();
          const tracked = await getTrackedHandles();

          const thin: string[] = [];
          for (const h of requestedHandles) {
            const { count } = await supabase
              .from("agent_workspace")
              .select("*", { count: "exact", head: true })
              .eq("artifact_type", "x_post")
              .or(`metadata->>author.eq.@${h},metadata->>author.eq.${h}`)
              .gte("created_at", sinceIso);
            if ((count || 0) < X_TIER2_MIN_POSTS) thin.push(h);
          }

          const fetchable = thin.filter((h) => tracked.has(h));
          for (const h of thin.filter((h) => !tracked.has(h))) skippedUntracked.push(h);

          const toFetch = fetchable.slice(0, X_TIER2_MAX_AUTHORS);
          // Zeitfilter nur setzen, wenn der Aufrufer selbst ein Fenster vorgibt.
          // Sonst die neuesten Posts holen – bei stillen Accounts liegt der letzte
          // Post oft älter als das Standardfenster, und genau der ist interessant.
          const fetchStart = days_back && days_back > 0 ? sinceIso : undefined;
          const budgetBefore = interactiveBudgetStatus().used;
          for (const h of toFetch) {
            try {
              log.info(`[Tier 2] Nachladen für @${h} (${fetchStart ? `${windowDays}d Fenster` : "neueste Posts"}, max ${X_TIER2_MAX_POSTS})...`);
              await ingestInfluencerTweets(`@${h}`, h, X_TIER2_MAX_POSTS, fetchStart, undefined, false, {
                source: "tier2",
                maxPages: 3,
                stopOnKnownPage: true,
                interactive: true,
              });
              fetchedAuthors.push(h);
            } catch (e: any) {
              log.warn(`[Tier 2] Nachladen für @${h} fehlgeschlagen: ${e.message}`);
            }
          }
          tier2Credits = interactiveBudgetStatus().used - budgetBefore;
          if (toFetch.length > 0 || skippedUntracked.length > 0) {
            tierNote += ` · dünne Datenlage: ${fetchedAuthors.length}/${toFetch.length} Accounts live nachgeladen` +
              (tier2Credits > 0 ? ` (${tier2Credits} Credits)` : "") +
              (skippedUntracked.length > 0 ? ` · nicht getrackt (Tier 3 nötig): ${skippedUntracked.map((h) => "@" + h).join(", ")}` : "");
          }
        }

        let data: any[];

        if (!trimmedQuery) {
          // Leere Query = "zeige die neuesten Posts": reine Liste, keine Vektorsuche.
          const { data: exactData, error } = await supabase.rpc("exact_search_workspace", {
            p_exact_keyword: null,
            match_count: limit,
            p_agent_id: p_agent_id,
            p_artifact_type: artifact_type || null,
            p_days_back: days_back || null,
            p_authors: scopeAuthors,
          });
          if (error) throw error;
          data = exactData || [];
        } else {
          // Hybrid (Vektor + Keyword, RRF) — wie search_yt_chunks, statt Entweder/Oder.
          const qEmb = await getQueryEmbedding(trimmedQuery, "x_search");
          const { data: hybData, error } = await supabase.rpc("hybrid_search_workspace", {
            query_embedding: qEmb,
            query_text: trimmedQuery,
            match_threshold: threshold,
            match_count: limit,
            p_agent_id: p_agent_id,
            p_artifact_type: artifact_type || null,
            p_days_back: days_back || null,
            p_authors: scopeAuthors,
          });
          if (error) throw error;
          data = hybData || [];
        }

        if (expandedAuthors && expandedAuthors.length > 0) {
          data = data.filter((t: any) => {
            const postAuthor = (t.metadata?.author || "").toLowerCase();
            const cleanPostAuthor = postAuthor.startsWith("@") ? postAuthor : `@${postAuthor}`;
            return expandedHandlesSet.has(postAuthor) || expandedHandlesSet.has(cleanPostAuthor);
          });
        }

        // --- Tier 3: freie Live-Suche auf X (Thema/Ticker), auch außerhalb des Korpus ---
        let liveBlock = "";
        if (tier >= 3 && trimmedQuery) {
          try {
            const handles = requestedHandles.slice(0, 20);
            const parts: string[] = [];
            if (handles.length > 0) parts.push(`(${handles.map((h) => `from:${h}`).join(" OR ")})`);
            parts.push(trimmedQuery);
            parts.push("-filter:replies");
            const q = parts.join(" ").slice(0, 500);

            const before = interactiveBudgetStatus().used;
            const json = await twitterApiIoFetch("tweet/advanced_search", { query: q, queryType: "Latest" }, { interactive: true });
            const tweets: any[] = json.tweets || [];
            const spent = interactiveBudgetStatus().used - before;

            liveBlock = tweets.length > 0
              ? `\n\n🌐 Live-Suche auf X (Tier 3${handles.length ? ` · ${handles.slice(0, 5).map((h) => "@" + h).join(", ")}${handles.length > 5 ? "…" : ""}` : " · alle X-Accounts"}): ` +
                `${tweets.length} Posts, ${spent} Credits\n` +
                tweets.slice(0, 20).map((t: any, i: number) =>
                  `[L${i + 1}] ${t.createdAt ? new Date(t.createdAt).toLocaleDateString("de-DE") : "?"} | @${t.author?.userName || "?"} | ❤️ ${t.likeCount || 0}\n${t.text}`).join("\n\n")
              : `\n\n🌐 Live-Suche auf X (Tier 3): keine Treffer (${spent} Credits).`;
          } catch (e: any) {
            liveBlock = `\n\n🌐 Live-Suche auf X (Tier 3) fehlgeschlagen: ${e.message}`;
          }
        }

        const header = `🔎 ${tierNote}\n\n`;
        if (!data || data.length === 0) {
          return { content: [{ type: "text", text: `${header}Keine Treffer im lokalen Korpus.${liveBlock}` }] };
        }

        return { content: [{ type: "text", text: `${header}${formatSearchResults(data, return_mode)}${liveBlock}` }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  // 2. Tool: Show X Content
  server.registerTool(
    "show_x_content",
    {
      title: "Show X Content (Timeline & Live Tweet)",
      description: "Browse X/Twitter content.\n- DATABASE: Lists stored posts in reverse chronological order (optional filter by array of usernames, or specific dates/times).\n- ONLINE: Fetches a single live tweet directly from the X API by tweet ID or URL.\n\n" +
        "WHEN TO USE: Use when asked to show recent tweets from influencers, retrieve tweets from a specific day/time window, or to look up a specific tweet by URL/ID.\n" +
        "WHEN NOT TO USE: To search across posts for specific keywords, topics, or stock tickers, use `search_influencer_posts`.\n" +
        "WHEN NOT TO USE (Ranglisten): Fuer Rang-, Breiten- oder Zaehlaufgaben ueber mehr als ~50 Posts nutze `ticker_breadth`. Jede Zeile hier enthaelt den vollen Post-Text (gemessen: ~400k Tokens pro Tageshaelfte) — und die Ticker stehen ohnehin schon als 🔑-Feld in jeder Zeile.",
      inputSchema: {
        action: z.enum(["DATABASE", "ONLINE"]).describe("DATABASE = list stored posts chronologically. ONLINE = fetch a single tweet live from API."),
        usernames: z.array(z.string()).optional().describe("Array of influencer handles to filter by (for DATABASE, e.g. ['@zerohedge']). Leave empty for all."),
        date: z.string().optional().describe("Specific calendar day to fetch posts for (YYYY-MM-DD) (for DATABASE). Evaluated in UTC."),
        start_time: z.string().optional().describe("Specific start time window (ISO 8601, e.g. 2026-09-08T09:30:00Z) (for DATABASE). Overrides date shortcut."),
        end_time: z.string().optional().describe("Specific end time window (ISO 8601, e.g. 2026-09-08T16:00:00Z) (for DATABASE)."),
        limit: z.number().optional().default(10).describe("Max posts to show (for DATABASE, default: 10)"),
        tweet_id: z.string().optional().describe("Tweet ID or URL to fetch (for ONLINE)"),
      },
    },
    async ({ action, usernames, date, start_time, end_time, limit, tweet_id }: any) => {
      try {
        if (action === "DATABASE") {
          let query = supabase
            .from("agent_workspace")
            .select("id, content, metadata, created_at")
            .eq("artifact_type", "x_post")
            .order("created_at", { ascending: false })
            .limit(limit || 10);

          if (usernames && usernames.length > 0) {
            const allHandlesSet = new Set<string>();
            for (const u of usernames) {
              const { allHandles } = await resolveAuthorHandles(u);
              allHandles.forEach(h => allHandlesSet.add(h));
            }
            query = query.in("metadata->>author", Array.from(allHandlesSet));
          }

          let filterStart = start_time;
          let filterEnd = end_time;

          if (date && !start_time && !end_time) {
            filterStart = `${date}T00:00:00Z`;
            filterEnd = `${date}T23:59:59Z`;
          }

          if (filterStart) {
            query = query.gte("created_at", filterStart);
          }
          if (filterEnd) {
            query = query.lte("created_at", filterEnd);
          }

          const { data, error } = await query;
          if (error) throw error;
          if (!data || data.length === 0) return { content: [{ type: "text", text: "Keine Posts in der Datenbank gefunden." }] };

          const formatted = data.map((p: any, i: number) => {
            const author = p.metadata?.author || "Unknown";
            const dateStr = p.metadata?.published_at ? new Date(p.metadata.published_at).toLocaleString('de-DE') : new Date(p.created_at).toLocaleString('de-DE');
            const tickers = p.metadata?.tickers?.length ? p.metadata.tickers.join(", ") : "Keine";
            return `[${i + 1}] 📅 ${dateStr} | 👤 ${author} | 🔑 ${tickers}\n${p.content}`;
          }).join("\n\n---\n\n");

          return { content: [{ type: "text", text: `${data.length} Posts gefunden:\n\n${formatted}` }] };
        } else if (action === "ONLINE") {
          if (!tweet_id) throw new Error("tweet_id ist für ONLINE erforderlich");
          if (!isTwitterApiIoAvailable()) throw new Error(
            "❌ TwitterAPI.io ist nicht konfiguriert (TWITTER_API_IO_KEY fehlt). " +
            "Einzel-Tweet-Lookup für fremde Profile ist nur mit TwitterAPI.io möglich. " +
            "Prüfe den Status mit 'manage_sync_pipeline STATUS'."
          );

          let id = tweet_id;
          const urlMatch = tweet_id.match(/status\/(\d+)/);
          if (urlMatch) id = urlMatch[1];

          const json = await twitterApiIoFetch("tweets", { tweet_ids: id }, { interactive: true });
          const tweets: any[] = json.tweets || [];
          if (!tweets[0]) throw new Error("Tweet nicht gefunden");

          const tweet = tweets[0];
          const author = tweet.author?.userName || tweet.author?.name || "Unbekannt";
          const dateStr = tweet.createdAt ? new Date(tweet.createdAt).toLocaleString('de-DE') : 'Unbekanntes Datum';
          return { content: [{ type: "text", text: `📅 ${dateStr} | 👤 @${author}\n\n${tweet.text}` }] };
        }

        throw new Error("Invalid action");
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // --- Helper Functions for Influencer Management & Batching ---
  function resolveContainerPath(filePath: string): string {
    if (filePath.startsWith("/home/daniel/QJM/dsh_playground")) {
      return filePath.replace("/home/daniel/QJM/dsh_playground", "/dsh_playground");
    }
    return filePath;
  }

  async function extractInfluencerHandles(
    username?: string,
    usernames?: string[],
    filePath?: string,
    rawText?: string,
  ): Promise<string[]> {
    const handles = new Set<string>();

    const addLineOrTokens = (str: string) => {
      const lineWithoutComment = str.split("#")[0].trim();
      if (!lineWithoutComment) return;
      const tokens = lineWithoutComment.split(/[,\s\r\n]+/);
      for (const t of tokens) {
        const clean = t.trim().replace(/^@/, "").toLowerCase();
        if (clean && /^[a-zA-Z0-9_]{1,25}$/.test(clean)) {
          handles.add(clean);
        }
      }
    };

    if (filePath) {
      const containerPath = resolveContainerPath(filePath);
      let readOk = false;
      try {
        const content = await Deno.readTextFile(containerPath);
        for (const line of content.split("\n")) {
          addLineOrTokens(line);
        }
        readOk = true;
      } catch {
        // Fallback: try direct path if containerPath failed
        try {
          const content = await Deno.readTextFile(filePath);
          for (const line of content.split("\n")) {
            addLineOrTokens(line);
          }
          readOk = true;
        } catch (e: any) {
          throw new Error(`Konnte Datei '${filePath}' (Container: '${containerPath}') nicht lesen: ${e.message}`);
        }
      }
    }

    if (rawText) {
      for (const line of rawText.split("\n")) {
        addLineOrTokens(line);
      }
    }

    if (usernames && Array.isArray(usernames)) {
      for (const u of usernames) {
        if (typeof u === "string") addLineOrTokens(u);
      }
    }

    if (username && typeof username === "string") {
      for (const line of username.split("\n")) {
        addLineOrTokens(line);
      }
    }

    return Array.from(handles);
  }

  function queueInitialSyncs(handles: string[]) {
    const queue = [...handles];
    const concurrency = Math.max(1, Math.min(X_INITIAL_SYNC_CONCURRENCY, queue.length));

    const workers = Array.from({ length: concurrency }, async () => {
      while (queue.length > 0) {
        const cleanName = queue.shift();
        if (!cleanName) break;
        const autoCleanName = `@${cleanName}`;
        if (activeSyncControllers.has(autoCleanName)) continue;

        const controller = new AbortController();
        activeSyncControllers.set(autoCleanName, controller);
        try {
          log.info(`[Initial Sync Queue] Starting initial backfill for ${autoCleanName} (${X_INITIAL_BACKFILL_LIMIT} posts)...`);
          await ingestInfluencerTweets(autoCleanName, cleanName, X_INITIAL_BACKFILL_LIMIT, undefined, controller.signal, false);
          log.info(`[Initial Sync Queue] Finished initial backfill for ${autoCleanName}.`);
        } catch (e: any) {
          log.error(`[Initial Sync Queue] Error for ${autoCleanName}: ${e.message}`);
        } finally {
          activeSyncControllers.delete(autoCleanName);
        }
      }
    });

    Promise.all(workers).catch(e => log.error(`[Initial Sync Queue] Worker pool error: ${e.message}`));
  }

  // 3. Tool: Manage Influencers
  server.registerTool(
    "manage_influencers",
    {
      title: "Manage Influencers",
      description:
        "List, add, remove or update influencers. ⭐ favorite = the account runs in the regular sync " +
        "(daily search + timeline check); everything else stays searchable and can be fetched on demand (Tier 2). " +
        "Each influencer has a description that lives as an OpenBrain entry (themes/strengths) and is referenced in x_users.description.",
      inputSchema: {
        action: z.enum(["LIST", "ADD", "REMOVE", "UPDATE"]).describe("LIST = show (favorites_only possible), ADD = add (favorite defaults to false), REMOVE = deactivate, UPDATE = set favorite/notes/description or rebuild the topic profile"),
        username: z.string().optional().describe("A single X username or comma-/newline-separated usernames (e.g. '@RayDalio, @fundstrat' or '@RayDalio')"),
        usernames: z.array(z.string()).optional().describe("An array of X usernames for batch operations (e.g. ['@RayDalio', '@fundstrat'])"),
        file_path: z.string().optional().describe("Optional path to a text file containing influencer handles (e.g. /home/daniel/QJM/dsh_playground/fintwit/01_makro_strategen.txt)"),
        raw_text: z.string().optional().describe("Raw text containing comma-, space- or newline-separated handles"),
        notes: z.string().optional().describe("Optional notes/category about the influencer(s) (ADD/UPDATE)"),
        favorite: z.boolean().optional().describe("ADD/UPDATE: only favorites run in the regular sync. Default on ADD: false"),
        favorites_only: z.boolean().optional().default(false).describe("LIST: show favorites only"),
        description: z.string().optional().describe("UPDATE: description text for the influencer – written to the OpenBrain entry and linked in x_users.description"),
        refresh_description: z.boolean().optional().default(false).describe("UPDATE: rebuild the topic profile from the stored posts (LLM) and refresh the OpenBrain entry"),
      },
    },
    async ({ action, username, usernames, file_path, raw_text, notes, favorite, favorites_only, description, refresh_description }: any) => {
      try {
        if (action === "LIST") {
          let listQuery = supabase
            .from("x_users")
            .select("username, screen_name, notes, favorite, description, brain_content")
            .eq("is_active", true)
            .order("favorite", { ascending: false })
            .order("username");
          if (favorites_only) listQuery = listQuery.eq("favorite", true);
          const { data, error } = await listQuery;
          if (error) throw error;
          if (!data || data.length === 0) {
            return { content: [{ type: "text", text: `Keine aktiven Influencer in der Datenbank gefunden.` }] };
          }
          const favCount = data.filter((u: any) => u.favorite).length;
          const formatted = data.map((u: any, idx: number) => formatInfluencerRow(u, idx)).join("\n");
          return {
            content: [{
              type: "text",
              text: `Influencer (${data.length} aktiv, davon ${favCount} ⭐ im Regel-Sync):\n\n${formatted}`,
            }],
          };
        }

        const targetHandles = await extractInfluencerHandles(username, usernames, file_path, raw_text);

        if (action === "ADD") {
          if (targetHandles.length === 0) {
            throw new Error("Mindestens ein Benutzername (username, usernames, file_path oder raw_text) ist für ADD erforderlich.");
          }
          if (!isTwitterApiIoAvailable()) {
            throw new Error(
              "❌ TwitterAPI.io ist nicht konfiguriert (TWITTER_API_IO_KEY fehlt). " +
              "Influencer können nicht hinzugefügt werden. " +
              "Prüfe den Status mit 'manage_sync_pipeline STATUS'."
            );
          }

          // 1. Check existing users in Supabase
          const { data: existingUsers } = await supabase
            .from("x_users")
            .select("username, is_active, screen_name")
            .in("username", targetHandles);

          const existingActiveMap = new Map<string, string>();
          if (existingUsers) {
            for (const u of existingUsers) {
              if (u.is_active) {
                existingActiveMap.set(u.username, u.screen_name || u.username);
              }
            }
          }

          const alreadyTracked: string[] = [];
          const toProcess: string[] = [];

          for (const h of targetHandles) {
            if (existingActiveMap.has(h)) {
              alreadyTracked.push(`@${h} (${existingActiveMap.get(h)})`);
            } else {
              toProcess.push(h);
            }
          }

          if (toProcess.length === 0) {
            return {
              content: [{
                type: "text",
                text: `ℹ️ Alle ${targetHandles.length} angegebenen Influencer werden bereits aktiv überwacht:\n${alreadyTracked.map(u => `• ${u}`).join("\n")}`
              }]
            };
          }

          // Helper to onboard a single user
          const onboardUser = async (cleanName: string) => {
            const json = await twitterApiIoFetch("user/info", { userName: cleanName }, { interactive: true });
            if (!json.id) {
              throw new Error("User nicht auf X gefunden (TwitterAPI.io)");
            }
            const userId = json.id;
            const screenName = json.name || cleanName;
            // Neue Influencer starten OHNE favorite: sie sind bekannt/durchsuchbar,
            // laufen aber erst nach ausdruecklicher Freigabe im Regel-Sync.
            const { error: upsertError } = await supabase.from("x_users").upsert({
              username: cleanName,
              x_id: userId,
              screen_name: screenName,
              notes: notes || null,
              is_active: true,
              favorite: favorite === true,
            }, { onConflict: "username" });

            if (upsertError) throw upsertError;

            // Beschreibung + OpenBrain-Eintrag: passiert standardmaessig. Ohne eigenen
            // Text wird ein Themenprofil aus den bereits gespeicherten Posts erzeugt
            // (bei einem brandneuen Account ist das ein Startprofil zum Ergaenzen).
            syncInfluencerBrainProfile(cleanName, {
              contentOverride: typeof description === "string" && description.trim() ? description.trim() : undefined,
              refreshFromPosts: !(typeof description === "string" && description.trim()),
            })
              .then((r) => log.info(`[Influencer-Profil] @${cleanName}: ${r.ref ? `OpenBrain ${r.ref}` : "kein Eintrag"}`))
              .catch((e: any) => log.debug(`Influencer-Profil für @${cleanName}: ${e.message}`));

            // Trigger initial backfill in background queue
            queueInitialSyncs([cleanName]);
            await recordSyncLog("influencer_added", `@${cleanName}`, `Influencer @${cleanName} (${screenName}) registriert und Initial-Sync gestartet.`);
            return { cleanName, screenName, userId };
          };

          // Single user: process synchronously for immediate feedback
          if (toProcess.length === 1) {
            try {
              const res = await onboardUser(toProcess[0]);
              return {
                content: [{
                  type: "text",
                  text: `✅ Influencer @${res.cleanName} (${res.screenName}) hinzugefügt.\n` +
                    `• Sync: ${favorite === true ? "⭐ favorite = läuft im Regel-Sync" : "kein favorite – nur auf Abruf (Tier 2), nicht im Regel-Sync"}\n` +
                    `• Initial-Sync (${X_INITIAL_BACKFILL_LIMIT} Posts) gestartet, Beschreibung/OpenBrain-Eintrag wird im Hintergrund erzeugt.\n` +
                    `• Profil später verfeinern: manage_influencers UPDATE mit refresh_description=true.`
                }]
              };
            } catch (err: any) {
              return {
                content: [{
                  type: "text",
                  text: `Fehler beim Hinzufügen von @${toProcess[0]}: ${err.message}`
                }],
                isError: true
              };
            }
          }

          // Batch mode (> 1 user): Process progressively in background to prevent MCP 60s timeout
          (async () => {
            log.info(`[Batch Import] Starte Hintergrund-Import für ${toProcess.length} Influencer...`);
            let successCount = 0;
            let failCount = 0;
            for (const cleanName of toProcess) {
              try {
                await onboardUser(cleanName);
                successCount++;
                log.info(`[Batch Import] (${successCount + failCount}/${toProcess.length}) @${cleanName} erfolgreich hinzugefügt.`);
              } catch (err: any) {
                failCount++;
                log.error(`[Batch Import] Fehler bei @${cleanName}: ${err.message}`);
                await recordSyncLog("influencer_add_failed", `@${cleanName}`, `Fehler: ${err.message}`);
              }
            }
            log.info(`[Batch Import] Abgeschlossen. Erfolgreich: ${successCount}, Fehlgeschlagen: ${failCount}.`);
          })().catch(e => log.error(`[Batch Import] Fataler Fehler: ${e.message}`));

          let responseText = `🚀 **Batch-Import im Hintergrund gestartet!**\n\n` +
            `• **Zu importieren:** ${toProcess.length} Accounts\n` +
            `• **Bereits aktiv:** ${alreadyTracked.length} Accounts\n` +
            `• **favorite:** ${favorite === true ? "ja (Regel-Sync)" : "nein (nur auf Abruf)"}\n` +
            `• **Initial-Backfill:** ${X_INITIAL_BACKFILL_LIMIT} Posts pro Account (Parallelität: ${X_INITIAL_SYNC_CONCURRENCY})\n` +
            `• **Beschreibung:** OpenBrain-Eintrag wird je Account automatisch erzeugt\n\n` +
            `Die Accounts werden nun schrittweise aufgelöst, in der Datenbank gespeichert und synchronisiert.`;

          if (alreadyTracked.length > 0) {
            responseText += `\n\nBereits aktive Accounts:\n${alreadyTracked.map(u => `• ${u}`).join("\n")}`;
          }

          return { content: [{ type: "text", text: responseText }] };

        } else if (action === "UPDATE") {
          if (targetHandles.length === 0) {
            throw new Error("Mindestens ein Benutzername (username, usernames) ist für UPDATE erforderlich.");
          }

          const lines: string[] = [];
          for (const h of targetHandles) {
            const patch: Record<string, unknown> = {};
            if (typeof favorite === "boolean") patch.favorite = favorite;
            if (typeof notes === "string") patch.notes = notes;

            if (Object.keys(patch).length > 0) {
              const { error } = await supabase.from("x_users").update(patch).eq("username", h);
              if (error) throw error;
            }

            const wantsProfile = !!description || refresh_description === true;
            let profileNote = "";
            if (wantsProfile) {
              const res = await syncInfluencerBrainProfile(h, {
                contentOverride: description ? String(description).trim() : undefined,
                refreshFromPosts: !description && refresh_description === true,
              });
              profileNote = res.ref
                ? ` | Beschreibung: openbrain://${res.ref}${description ? " (Text gesetzt)" : " (aus Posts erzeugt)"}`
                : " | ⚠️ OpenBrain-Eintrag fehlgeschlagen";
            }

            const flags = [
              typeof favorite === "boolean" ? `favorite=${favorite}` : null,
              typeof notes === "string" ? "notes gesetzt" : null,
            ].filter(Boolean).join(", ");

            lines.push(`• @${h}: ${flags || "keine Änderung"}${profileNote}`);
            await recordSyncLog("influencer_updated", `@${h}`, `Update: ${flags || "-"}${wantsProfile ? " + Beschreibung" : ""}`);
          }

          return {
            content: [{
              type: "text",
              text: `✅ Influencer aktualisiert:\n${lines.join("\n")}\n\n` +
                `Hinweis: favorite steuert den Regel-Sync – ohne favorite wird der Account nur auf Abruf (Tier 2) geladen.`,
            }],
          };

        } else if (action === "REMOVE") {
          if (targetHandles.length === 0) {
            throw new Error("Mindestens ein Benutzername (username, usernames, file_path oder raw_text) ist für REMOVE erforderlich.");
          }

          // Deaktivieren nimmt den Account auch aus dem Regel-Sync (favorite=false).
          const { data, error } = await supabase
            .from("x_users")
            .update({ is_active: false, favorite: false })
            .in("username", targetHandles)
            .select("username, screen_name");

          if (error) throw error;

          const deactivated = (data || []).map((u: any) => `@${u.username}`);
          const notFound = targetHandles.filter((h: string) => !data?.some((d: any) => d.username === h)).map((h: string) => `@${h}`);

          const report = [];
          if (deactivated.length > 0) {
            report.push(`🛑 Folgende Influencer wurden deaktiviert (Soft-Delete):\n${deactivated.map((u: string) => `• ${u}`).join("\n")}`);
          }
          if (notFound.length > 0) {
            report.push(`ℹ️ Folgende Handles waren nicht als aktiv in der Datenbank hinterlegt:\n${notFound.map((u: string) => `• ${u}`).join("\n")}`);
          }

          return { content: [{ type: "text", text: report.join("\n\n") || "Keine Änderungen vorgenommen." }] };
        }

        throw new Error("Invalid action");
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // 4. Tool: Discover Ticker Mentions
  server.registerTool(
    "discover_ticker_mentions",
    {
      title: "Discover Ticker Mentions",
      description: "Find tickers that an influencer mentioned for the VERY FIRST TIME ever. " +
        "Laeuft in EINEM Aufruf gegen die First-Mentions-Historie — ohne vorherige Sammlung des Korpus.\n\n" +
        "WHEN TO USE: Wenn die Frage 'was ist neu?' lautet. Das ist ein eigener, ERSTER Schritt, " +
        "keine Ableitung aus einer Vollextraktion. start_date auf das interessierende Fenster setzen.\n" +
        "WHEN NOT TO USE: Fuer Breite UND Rangfolge im Fenster (wie viele verschiedene Autoren nennen einen Ticker) " +
        "nutze `ticker_breadth` mit only_new=true — das liefert beides in einem Aufruf.",
      inputSchema: {
        keywords: z.array(z.string()).optional().describe("Specific tickers to check (e.g. ['NVDA'])"),
        authors: z.array(z.string()).optional().describe("Filter to specific influencers (e.g. ['@serenity'])"),
        start_date: z.string().optional().describe("Time window for discovery (ISO format)"),
        limit: z.number().optional().default(10).describe("Max results (default: 10)"),
      },
    },
    async ({ keywords, authors, start_date, limit }: any) => {
      try {
        let targetAuthors: string[] | null = null;
        if (authors && authors.length > 0) {
          const expandedSet = new Set<string>();
          for (const a of authors) {
            const { allHandles } = await resolveAuthorHandles(a);
            allHandles.forEach(h => expandedSet.add(h.toLowerCase()));
          }
          targetAuthors = Array.from(expandedSet);
        }
        const targetKeywords = keywords && keywords.length > 0 ? keywords : null;

        const { data, error } = await supabase.rpc("get_first_mentions_v2", {
          p_keywords: targetKeywords,
          p_authors: targetAuthors,
          p_start_date: start_date || null,
          p_limit: limit
        });
        if (error) throw error;
        if (!data || data.length === 0) return { content: [{ type: "text", text: "Keine ersten Erwähnungen gefunden." }] };

        const title = targetKeywords ? `Erste Erwähnungen für: ${targetKeywords.join(', ')}` : (targetAuthors ? `Zuletzt entdeckte Ticker von ${targetAuthors.join(', ')}` : "Zuletzt entdeckte (neue) Ticker");

        const formattedResults = data.map((r: any) => {
          const dateStr = r.first_mentioned_at ? new Date(r.first_mentioned_at).toLocaleString('de-DE') : 'Unbekanntes Datum';
          return `* **Ticker:** ${r.keyword}\n  📅 **Erstmals erwähnt:** ${dateStr}\n  👤 **Influencer:** ${r.author}\n  📝 **Post:** "${r.post_content}"`;
        }).join("\n\n");

        return { content: [{ type: "text", text: `### ${title}\n\n${formattedResults}` }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  // 5. Tool: Search X for Ticker Posts (Live-Suche, read-only)
  server.registerTool(
    "search_x_ticker",
    {
      title: "Search X for Ticker Posts (Live)",
      description: "Live-Suche auf X nach einem Cashtag ($TICKER) inkl. Engagement-Metriken. " +
        "Gibt Posts zurück, die NICHT in der lokalen Datenbank liegen. Das Zeitfenster wird automatisch " +
        "geweitet (heute -> 7 -> 30 -> 365 Tage -> all-time), bis `limit` Posts die Filter erfüllen.\n\n" +
        "WHEN TO USE: Für frische, nicht gespeicherte Posts zu einem Ticker (z.B. 'suche $HL, 20 Posts, min. 20 Likes, min. 1000 Follower').\n" +
        "WHEN NOT TO USE: Für Suchen im bereits gespeicherten Influencer-Korpus -> `search_influencer_posts`.\n\n" +
        "Hinweis: X' min_faves/min_retweets-Filter arbeiten mit veralteten Index-Werten, daher wird zusätzlich exakt clientseitig geprüft.",
      inputSchema: {
        ticker: z.string().describe("Ticker ohne $ (z.B. 'HL', 'NVDA'). Wird als Cashtag $TICKER gesucht."),
        limit: z.number().optional().default(20).describe("Ziel-Anzahl Posts (default: 20)"),
        min_likes: z.number().optional().default(0).describe("Mindest-Likes (exakt geprüft)"),
        min_retweets: z.number().optional().default(0).describe("Mindest-Retweets (exakt geprüft)"),
        min_followers: z.number().optional().default(0).describe("Mindest-Follower des Autors (clientseitig geprüft)"),
        max_age_days: z.number().optional().describe("Festes Zeitfenster in Tagen (0 = heute). Ohne Angabe: automatische Leiter heute -> 7 -> 30 -> 365 -> all-time"),
        exclude_replies: z.boolean().optional().default(true).describe("Antworten ausschließen (default: true)"),
        exclude_known: z.boolean().optional().default(true).describe("Posts ausschließen, die schon in der DB liegen (default: true)"),
        max_per_author: z.number().optional().default(3).describe("Max. Treffer pro Autor (default: 3, 0 = unbegrenzt) – verhindert Dominanz einzelner Listen-Poster"),
        max_cashtags: z.number().optional().default(0).describe("Max. Cashtags im Post-Text (0 = unbegrenzt) – filtert Sammel-Posts mit vielen Tickern"),
        exclude_crypto_spam: z.boolean().optional().default(true).describe("Krypto-Token-Promo (Contract-Address im Text) ausschließen (default: true)"),
        sort: z.enum(["engagement", "recent"]).optional().default("engagement").describe("Sortierung: engagement = Likes + 2x Retweets"),
        page_budget: z.number().optional().default(6).describe("Harte Kostengrenze: max. API-Seiten pro Suche (default: 6 = max. ~$0.018)"),
      },
    },
    async (args: any) => {
      try {
        const { text } = await searchTickerOnline(args);
        return { content: [{ type: "text", text }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler bei der Ticker-Suche: ${err.message}` }], isError: true };
      }
    }
  );

  // 6. Tool: Sync Influencer Posts (Manual On-Demand)
  server.registerTool(
    "sync_influencer_posts",
    {
      title: "Sync Influencer Posts",
      description: "Manually triggers a sync for a specific influencer. Fetches tweets and enqueues them for parallel metadata extraction and embeddings.",
      inputSchema: {
        author: z.string().describe("Influencer username (e.g. '@elonmusk')"),
        limit: z.number().optional().default(X_INITIAL_BACKFILL_LIMIT).describe(`Max posts to fetch (default: ${X_INITIAL_BACKFILL_LIMIT})`),
        start_time: z.string().optional().describe("Earliest post date (ISO format) for historical backfill"),
      },
    },
    async ({ author, limit, start_time }: any) => {
      try {
        const cleanName = author.toLowerCase().startsWith("@") ? author.toLowerCase() : `@${author.toLowerCase()}`;
        const username = cleanName.substring(1);
        const syncLimit = limit || X_INITIAL_BACKFILL_LIMIT;

        if (activeSyncControllers.has(cleanName)) {
          return { content: [{ type: "text", text: `Sync für ${cleanName} läuft bereits im Hintergrund.` }] };
        }

        const controller = new AbortController();
        activeSyncControllers.set(cleanName, controller);

        // Run ingestion in background (onlyForward: false allows paginating back up to limit)
        ingestInfluencerTweets(cleanName, username, syncLimit, start_time, controller.signal, false, { source: "backfill", interactive: true })
          .then(count => log.info(`[Sync] Manual sync for ${cleanName} completed: ${count} posts ingested.`))
          .catch(e => log.error(`[Sync] Manual sync for ${cleanName} failed: ${e.message}`))
          .finally(() => activeSyncControllers.delete(cleanName));

        return {
          content: [{
            type: "text",
            text: `🚀 Hintergrund-Sync für ${cleanName} gestartet (Limit: ${syncLimit} Posts). Die Posts werden sofort gespeichert und asynchron analysiert & ge-embeddet.`
          }]
        };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // 6. Tool: Cancel Influencer Sync
  server.registerTool(
    "cancel_influencer_sync",
    {
      title: "Cancel Influencer Sync",
      description: "Aborts an ongoing background sync for a specific influencer.",
      inputSchema: {
        author: z.string().describe("Influencer handle (e.g. '@aleabitoreddit')"),
      },
    },
    async ({ author }: any) => {
      try {
        const cleanName = author.toLowerCase().startsWith("@") ? author.toLowerCase() : `@${author.toLowerCase()}`;
        const controller = activeSyncControllers.get(cleanName);
        if (!controller) {
          return { content: [{ type: "text", text: `Kein aktiver Sync für ${cleanName} gefunden.` }] };
        }

        controller.abort();
        activeSyncControllers.delete(cleanName);
        await recordSyncLog("aborted", cleanName, "Sync manuell durch Benutzer abgebrochen.");

        return { content: [{ type: "text", text: `🛑 Sync für ${cleanName} wurde erfolgreich abgebrochen.` }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // 7. Tool: Manage X OAuth
  server.registerTool(
    "manage_x_auth",
    {
      title: "Manage X OAuth 2.0 Auth",
      description: "Check status of X (Twitter) OAuth 2.0 User Context authorization or get login link.",
      inputSchema: {
        action: z.enum(["STATUS", "GET_LOGIN_URL"]).describe("STATUS = check status, GET_LOGIN_URL = get browser URL"),
      },
    },
    async ({ action }: any) => {
      try {
        const tokens = await getXOAuthTokens();
        const isConnected = !!(tokens && tokens.access_token);

        if (action === "GET_LOGIN_URL") {
          return {
            content: [{
              type: "text",
              text: `🔗 Öffne diesen Link im Browser, um deinen X-Account zu verbinden:\n\nhttp://127.0.0.1:8788/auth/x/login`
            }]
          };
        }

        if (action === "STATUS") {
          const twitterApiIoStatus = isTwitterApiIoAvailable() 
            ? "🟢 aktiv (fremde Profile)" 
            : "🔴 nicht konfiguriert (TWITTER_API_IO_KEY fehlt)";

          const xApiStatus = X_CLIENT_ID
            ? (isConnected && tokens
                ? "🟢 aktiv (eigener Account: OAuth + Bookmarks)"
                : "🟡 OAuth nicht autorisiert – Login nötig")
            : "🔴 nicht konfiguriert (X_CLIENT_ID fehlt)";

          const statusLines = [
            `📊 **Provider-Status**`,
            ``,
            `| Provider | Status |`,
            `|----------|--------|`,
            `| TwitterAPI.io (fremde Profile: User-Resolution, Timeline, Tweets) | ${twitterApiIoStatus} |`,
            `| Offizielle X API (eigener Account: Bookmarks, OAuth) | ${xApiStatus} |`,
          ];

          if (!isConnected || !tokens) {
            statusLines.push(``);
            statusLines.push(`🔴 X OAuth 2.0 ist noch NICHT autorisiert.`);
            statusLines.push(`👉 Öffne: http://127.0.0.1:8788/auth/x/login`);
            statusLines.push(``);
            statusLines.push(`Hinweis: Nur Bookmarks & OAuth benötigen die offizielle X API.`);
            statusLines.push(`Fremde Profile (Influencer-Timeline etc.) laufen über TwitterAPI.io.`);
          } else {
            const expiresInMinutes = Math.round((tokens.expires_at - Date.now()) / 60000);
            const expiryText = expiresInMinutes > 0 ? `in ${expiresInMinutes} Minuten` : "Abgelaufen (Auto-Refresh aktiv)";
            statusLines.push(``);
            statusLines.push(`🟢 OAuth verbunden: @${tokens.username || '?'} (${tokens.name || 'N/A'}) | Token: ${expiryText}`);
          }

          return { content: [{ type: "text", text: statusLines.join("\n") }] };
        }

        throw new Error("Invalid action");
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // 8. Tool: Sync X Bookmarks
  server.registerTool(
    "sync_x_bookmarks",
    {
      title: "Sync X Bookmarks",
      description: "Fetch and save bookmarked posts from your personal X account.",
      inputSchema: {
        limit: z.number().optional().default(50).describe("Max bookmarks to fetch (default: 50)"),
      },
    },
    async ({ limit }: any) => {
      try {
        const tokens = await getXOAuthTokens();
        if (!tokens || !tokens.access_token) {
          throw new Error(
            "❌ Bookmarks-Sync benötigt die offizielle X API für deinen eigenen Account. " +
            "X OAuth ist nicht autorisiert – öffne http://127.0.0.1:8788/auth/x/login im Browser. " +
            "Hinweis: Fremde Profile laufen über TwitterAPI.io (TWITTER_API_IO_KEY)."
          );
        }
        if (!X_CLIENT_ID) {
          throw new Error(
            "❌ X_CLIENT_ID fehlt in der .env. " +
            "Bookmarks & OAuth benötigen die offizielle X API (eigener Account). " +
            "Fremde Profile laufen über TwitterAPI.io (TWITTER_API_IO_KEY)."
          );
        }

        const { access_token, user_id } = await getValidXUserAccessToken();
        if (!user_id) throw new Error("Keine User ID gefunden. Bitte neu autorisieren: http://127.0.0.1:8788/auth/x/login");

        const fetchLimit = Math.min(Math.max(1, limit || 50), 100);
        const url = `https://api.twitter.com/2/users/${user_id}/bookmarks?max_results=${fetchLimit}&tweet.fields=created_at,entities,author_id&expansions=author_id&user.fields=username,name`;

        const res = await throttledXFetch(url, {
          headers: { Authorization: `Bearer ${access_token}` }
        });
        if (!res.ok) throw new Error(`X API Bookmarks fetch failed (${res.status}): ${await res.text()}`);

        const data = await res.json();
        if (!data.data || data.data.length === 0) {
          return { content: [{ type: "text", text: "Keine Lesezeichen in deinem X-Account gefunden." }] };
        }

        const authorMap = new Map<string, string>();
        if (data.includes?.users) {
          for (const u of data.includes.users) authorMap.set(u.id, u.username);
        }

        let savedCount = 0;
        for (const tweet of data.data) {
          const rawAuthor = authorMap.get(tweet.author_id) || "unknown";
          const cleanAuthor = `@${rawAuthor.toLowerCase()}`;

          const { data: existing } = await supabase
            .from("agent_workspace")
            .select("id")
            .eq("artifact_type", "x_bookmark")
            .eq("metadata->>id", tweet.id)
            .single();

          if (!existing) {
            await supabase.from("agent_workspace").insert({
              agent_id: "cco",
              artifact_type: "x_bookmark",
              title: `Bookmark from ${cleanAuthor}`,
              content: tweet.text,
              status: "pending_metadata",
              created_at: tweet.created_at || new Date().toISOString(),
              metadata: {
                id: tweet.id,
                author: cleanAuthor,
                published_at: tweet.created_at,
                source: "x_bookmark"
              }
            });
            savedCount++;
          }
        }

        return { content: [{ type: "text", text: `✅ ${savedCount} neue Lesezeichen aus deinem X-Account importiert (Status: pending_metadata).` }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler beim Bookmark-Sync: ${err.message}` }], isError: true };
      }
    }
  );
}
