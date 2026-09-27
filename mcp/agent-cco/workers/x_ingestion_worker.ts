import {
  supabase,
  log,
  X_DISCOVERY_INTERVAL_SEC,
  twitterApiIoFetch,
  isTwitterApiIoAvailable,
  X_INITIAL_BACKFILL_LIMIT,
  X_LIVENESS_CHECK,
  X_INGESTION_MODE,
  X_SEARCH_INTERVAL_SEC,
  X_SEARCH_OVERLAP_SEC,
  X_SEARCH_MAX_CATCHUP_SEC,
  X_RECONCILE_INTERVAL_SEC,
  X_RECONCILE_LIMIT,
  X_MAX_CREDITS_PER_CYCLE,
  X_RECONCILE_MAX_PAGES_PER_USER,
  X_RECONCILE_STOP_ON_KNOWN_PAGE,
  X_RECONCILE_MAX_LOOKBACK_SEC,
  X_DAILY_RUN_AT,
  X_DAILY_RUN_TZ,
  twitterApiIoStats,
} from "../tools/shared.ts";

export const activeSyncControllers = new Map<string, AbortController>();

// --- TwitterAPI.io Preis-Modell (100.000 Credits = $1) ---
// 15 Credits pro ausgeliefertem Tweet (= $0.15 / 1.000 Tweets)
// 18 Credits pro User beim Batch-Liveness-Check (10 Credits ab 100 Usern pro Call)
export const TWITTERAPI_CREDITS_PER_TWEET = 15;
export const TWITTERAPI_CREDITS_PER_USER_CHECK = 18;

/** Kostenquelle eines TwitterAPI.io-Fetches – macht im STATUS sichtbar, wohin die Credits fließen. */
export type XCreditSource = "search" | "search_rt" | "reconcile" | "timeline" | "liveness" | "backfill" | "tier2" | "other";

export const xIngestionStats = {
  isRunning: false,
  startTime: 0,
  lastRunTime: 0,
  cycleCount: 0,
  totalPostsIngested: 0,
  lastError: null as string | null,
  // Kosten-Telemetrie (Credits, 100.000 Credits = $1)
  lastCycleTweetsFetched: 0,
  lastCycleUsersChecked: 0,
  lastCycleUsersSynced: 0,
  lastCycleCredits: 0,
  totalTweetsFetched: 0,
  totalCredits: 0,
  // Aufschlüsselung nach Quelle (Zyklus + kumuliert). Wichtig: die Zyklus-Summe
  // enthält Abgleich UND Suche – vorher standen die Abgleich-Credits in der
  // Such-Logzeile und haben die Kostenanalyse verfälscht.
  lastCycleCreditsBySource: {} as Record<string, number>,
  totalCreditsBySource: {} as Record<string, number>,
  lastSearchTweetsFetched: 0,
  lastSearchCredits: 0,
  lastReconcileTweetsFetched: 0,
  lastReconcileCredits: 0,
  // Budget-/Guthaben-Bremsen
  budgetStops: 0,
  lastBudgetStop: null as { source: string; credits: number; at: number } | null,
  paymentBlockedUntil: 0,
  paymentErrors402: 0,
  // Such-Modus
  lastSearchSaved: 0,
  lastSearchFailed: 0,
  lastSearchSince: 0,
  lastSearchTruncated: false,
  lastReconcileSaved: 0,
  lastReconcileAt: 0,
  lastReconcileSkipped: 0,
  // Täglicher Betrieb / manueller Auslöser
  lastCycleFinishedAt: 0,
  nextRunAt: 0,
  manualRunPending: false,
  manualRunRequestedAt: 0,
  gapCloserRuns: 0,
  gapCloserSaved: 0,
  coverageFailures: 0,
};

function trackTweetsFetched(n: number, source: XCreditSource = "other") {
  if (!n || n <= 0) return;
  xIngestionStats.lastCycleTweetsFetched += n;
  xIngestionStats.totalTweetsFetched += n;
  const credits = n * TWITTERAPI_CREDITS_PER_TWEET;
  xIngestionStats.lastCycleCredits += credits;
  xIngestionStats.totalCredits += credits;
  xIngestionStats.lastCycleCreditsBySource[source] = (xIngestionStats.lastCycleCreditsBySource[source] || 0) + credits;
  xIngestionStats.totalCreditsBySource[source] = (xIngestionStats.totalCreditsBySource[source] || 0) + credits;
}

// Das Budget gilt pro PHASE, nicht pro Zyklus: Ein Zyklus besteht aus Suche und
// (bei Bedarf) dem Lücken schließenden Timeline-Abgleich. Wäre das Budget global,
// würde ein am Budget abgebrochener Suchlauf den Gap-Closer sofort mitblockieren –
// genau in der Situation, in der er gebraucht wird.
let budgetBaseCredits = 0;

/** Budget-Guard: true, sobald die laufende Phase die Credits-Obergrenze erreicht hat. */
function creditBudgetExceeded(): boolean {
  return X_MAX_CREDITS_PER_CYCLE > 0 &&
    (xIngestionStats.lastCycleCredits - budgetBaseCredits) >= X_MAX_CREDITS_PER_CYCLE;
}

/** Startet eine neue Budget-Phase (Suche / Gap-Closer). */
function resetPhaseBudget() {
  budgetBaseCredits = xIngestionStats.lastCycleCredits;
  budgetStopNotedThisCycle = false;
}

// Pro Zyklus nur einmal warnen – sonst erzeugt der Guard bei 119 Influencern 119 Zeilen.
let budgetStopNotedThisCycle = false;

function noteBudgetStop(source: string) {
  xIngestionStats.budgetStops++;
  xIngestionStats.lastBudgetStop = {
    source,
    credits: xIngestionStats.lastCycleCredits,
    at: Date.now(),
  };
  if (budgetStopNotedThisCycle) return;
  budgetStopNotedThisCycle = true;
  log.warn(
    `[X Ingestion] ⚠️ Credit-Budget erreicht (${xIngestionStats.lastCycleCredits} Credits ≥ ` +
    `${X_MAX_CREDITS_PER_CYCLE}) in ${source} – Zyklus wird vorzeitig beendet ` +
    `(weitere Meldungen unterdrückt).`,
  );
}

// --- Gemeinsame Helfer für Timeline- und Such-Modus ---

/** X-Zeitstempel ("Mon Sep 14 11:22:24 +0000 2026") oder ISO -> Unix-Sekunden. */
export function tweetEpoch(createdAt?: string): number {
  if (!createdAt) return 0;
  const ms = Date.parse(createdAt);
  return Number.isNaN(ms) ? 0 : Math.floor(ms / 1000);
}

/** Baut den agent_workspace-Record für einen Tweet (identisch für Timeline und Suche). */
export function buildPostRecord(tweet: any, authorHandle: string, screenName: string) {
  const mediaUrls: string[] = [];
  if (tweet.entities?.urls) {
    for (const u of tweet.entities.urls) {
      if (u.expanded_url) mediaUrls.push(u.expanded_url);
    }
  }
  if (tweet.media_urls) {
    for (const m of tweet.media_urls) mediaUrls.push(m);
  }

  return {
    agent_id: "cco",
    artifact_type: "x_post",
    content: tweet.text,
    status: "pending_metadata",
    created_at: tweet.createdAt || new Date().toISOString(),
    metadata: {
      external_id: tweet.id,
      author: authorHandle,
      screen_name: screenName,
      published_at: tweet.createdAt,
      media_urls: mediaUrls,
      public_metrics: {
        retweet_count: tweet.retweetCount || 0,
        reply_count: tweet.replyCount || 0,
        like_count: tweet.likeCount || 0,
        quote_count: tweet.quoteCount || 0,
        bookmark_count: tweet.bookmarkCount || 0,
      },
      stage: "ingested",
    },
  };
}

/** Speichert Records mit external_id-Dedup und zählt die tatsächlich neuen. */
export async function savePostRecords(records: any[]): Promise<number> {
  let saved = 0;
  for (const rec of records) {
    const { data: existing } = await supabase
      .from("agent_workspace")
      .select("id")
      .eq("artifact_type", "x_post")
      .eq("metadata->>external_id", rec.metadata.external_id)
      .single();

    if (existing) continue;

    const { error: insErr } = await supabase.from("agent_workspace").insert(rec);
    if (insErr) {
      log.warn(`[X Ingestion] Insert ${rec.metadata.external_id} fehlgeschlagen: ${insErr.message}`);
    } else {
      saved++;
      xIngestionStats.totalPostsIngested++;
    }
  }
  return saved;
}

/** Persistenter Sync-Zustand (Such-Cursor + letzter Timeline-Abgleich). */
interface IngestionState {
  search_since: number;      // Unix-Sekunden des letzten erfolgreichen Suchlaufs
  last_reconcile: number;    // Unix-Millisekunden des letzten Timeline-Abgleichs
}
const INGESTION_STATE_KEY = "x_ingestion_state";

async function loadIngestionState(): Promise<IngestionState> {
  try {
    const { data } = await supabase
      .from("system_settings")
      .select("value")
      .eq("key", INGESTION_STATE_KEY)
      .single();
    return {
      search_since: Number(data?.value?.search_since) || 0,
      last_reconcile: Number(data?.value?.last_reconcile) || 0,
    };
  } catch (_e) {
    return { search_since: 0, last_reconcile: 0 };
  }
}

async function saveIngestionState(state: IngestionState): Promise<void> {
  const { error } = await supabase
    .from("system_settings")
    .upsert({ key: INGESTION_STATE_KEY, value: state }, { onConflict: "key" });
  if (error) log.warn(`[X Ingestion] State konnte nicht gespeichert werden: ${error.message}`);
}

let xIngestionAbortController: AbortController | null = null;

// =====================================================================
// Laufzeitplan: täglich um X_DAILY_RUN_AT (Zeitzone X_DAILY_RUN_TZ)
// oder alter Intervallbetrieb, wenn X_DAILY_RUN_AT leer ist.
// =====================================================================
let manualRunRequested = false;

/** Fordert sofort einen Ingestion-Zyklus an (MCP-Tool manage_sync_pipeline SYNC_NOW). */
export function requestXIngestionRun(): { scheduled: boolean; running: boolean } {
  const wasPending = manualRunRequested;
  manualRunRequested = true;
  xIngestionStats.manualRunPending = true;
  xIngestionStats.manualRunRequestedAt = Date.now();
  return { scheduled: !wasPending, running: xIngestionStats.isRunning };
}

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** Lokale Wanduhr (Datum + Stunde/Minute/Sekunde) in der konfigurierten Zeitzone. */
function localClockParts(nowMs: number): { date: string; hour: number; minute: number; second: number } {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: X_DAILY_RUN_TZ,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(nowMs));
  const m: Record<string, string> = {};
  for (const p of parts) m[p.type] = p.value;
  return {
    date: `${m.year}-${m.month}-${m.day}`,
    hour: Number(m.hour) % 24,
    minute: Number(m.minute),
    second: Number(m.second),
  };
}

/** UTC-Offset der Zeitzone zu einem Zeitpunkt (z.B. +2h für Europe/Berlin im Sommer). */
function tzOffsetMs(instantMs: number): number {
  const c = localClockParts(instantMs);
  const asIfUtc = Date.parse(`${c.date}T${pad2(c.hour)}:${pad2(c.minute)}:${pad2(c.second)}Z`);
  return asIfUtc - instantMs;
}

/** Wanduhrzeit (lokales Datum + HH:MM) als UTC-Zeitstempel. */
function localWallClockToUtcMs(date: string, hour: number, minute: number): number {
  const naive = Date.parse(`${date}T${pad2(hour)}:${pad2(minute)}:00Z`);
  return naive - tzOffsetMs(naive);
}

function parseDailyRunAt(): { hour: number; minute: number } | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(X_DAILY_RUN_AT.trim());
  if (!m) return null;
  const hour = Number(m[1]);
  const minute = Number(m[2]);
  if (hour > 23 || minute > 59) return null;
  return { hour, minute };
}

/** Letzter planmäßiger Laufzeitpunkt (<= jetzt) in UTC-Millisekunden. */
function lastDailyTargetMs(nowMs: number): number | null {
  const at = parseDailyRunAt();
  if (!at) return null;
  const today = localClockParts(nowMs);
  const todayTarget = localWallClockToUtcMs(today.date, at.hour, at.minute);
  if (todayTarget <= nowMs) return todayTarget;
  // sonst: gestriger Zielzeitpunkt
  const yesterdayDate = new Date(Date.parse(`${today.date}T00:00:00Z`) - 86400000).toISOString().slice(0, 10);
  return localWallClockToUtcMs(yesterdayDate, at.hour, at.minute);
}

/** Nächster planmäßiger Laufzeitpunkt (> jetzt). */
export function nextDailyTargetMs(nowMs: number): number | null {
  const at = parseDailyRunAt();
  if (!at) return null;
  const today = localClockParts(nowMs);
  const todayTarget = localWallClockToUtcMs(today.date, at.hour, at.minute);
  if (todayTarget > nowMs) return todayTarget;
  const tomorrowDate = new Date(Date.parse(`${today.date}T00:00:00Z`) + 86400000).toISOString().slice(0, 10);
  return localWallClockToUtcMs(tomorrowDate, at.hour, at.minute);
}

/** true, wenn seit dem letzten Lauf ein planmäßiger Zeitpunkt durchgelaufen ist. */
function isDailyRunDue(nowMs: number, lastRunMs: number): boolean {
  const target = lastDailyTargetMs(nowMs);
  if (target === null) return false;
  if (!lastRunMs) return true;
  return lastRunMs < target;
}

// Helper: Log sync action to x_sync_logs table
export async function recordSyncLog(actionType: string, username: string | null, message: string) {
  log.info(`[X Sync Log] [${actionType}] ${username || 'system'}: ${message}`);
  try {
    await supabase.from("x_sync_logs").insert({ action_type: actionType, username, message });
  } catch (e: any) {
    log.error(`Failed to write sync log: ${e.message}`);
  }
}

// Resolve X User ID (Cached in x_users) – via TwitterAPI.io
export async function getXUserId(username: string): Promise<{ id: string; name: string }> {
  const cleanName = username.startsWith("@") ? username.substring(1) : username;

  const { data: cachedUser } = await supabase
    .from("x_users")
    .select("x_id, screen_name")
    .eq("username", cleanName)
    .single();

  if (cachedUser?.x_id && cachedUser?.screen_name) {
    return { id: cachedUser.x_id, name: cachedUser.screen_name };
  }

  if (!isTwitterApiIoAvailable()) {
    throw new Error(
      "❌ TwitterAPI.io ist nicht konfiguriert (TWITTER_API_IO_KEY fehlt). " +
      "Fremde Profile können nicht aufgelöst werden. " +
      "Prüfe den Status mit 'manage_sync_pipeline STATUS'.",
    );
  }

  const json = await twitterApiIoFetch("user/info", { userName: cleanName });
  if (!json.id) throw new Error(`User @${cleanName} nicht auf X gefunden (TwitterAPI.io).`);

  const userId: string = json.id;
  const screenName: string = json.name;

  await supabase.from("x_users").upsert({ username: cleanName, x_id: userId, screen_name: screenName });

  return { id: userId, name: screenName };
}

/**
 * Stage 1 Fast Ingestion for a single influencer.
 * Fetches tweets via TwitterAPI.io and saves raw records immediately with status = 'pending_metadata'.
 *
 * TwitterAPI.io caveats vs official API:
 * - 20 tweets/page (not 100)
 * - No server-side since_id/start_time filters → client-side dedup
 * - Pagination via next_cursor (not next_token)
 * - createdAt/retweetCount etc. in camelCase
 * - Author & entities embedded in each tweet object
 */
export async function ingestInfluencerTweets(
  cleanName: string,
  username: string,
  targetLimit?: number,
  startTime?: string,
  signal?: AbortSignal,
  onlyForward: boolean = true,
  options?: { source?: XCreditSource; maxPages?: number; stopOnKnownPage?: boolean; interactive?: boolean },
): Promise<number> {
  const { id: userId, name: screenName } = await getXUserId(username);
  const handleNoAt = cleanName.replace(/^@/, "");

  // Find latest saved post ID to establish forward-sync baseline
  const { data: latestRecord } = await supabase
    .from("agent_workspace")
    .select("metadata")
    .eq("artifact_type", "x_post")
    .or(`metadata->>author.eq.${cleanName},metadata->>author.eq.${handleNoAt},metadata->>author.ilike.%${handleNoAt}%`)
    .order("created_at", { ascending: false })
    .limit(1)
    .single();

  const sinceId = latestRecord?.metadata?.external_id || undefined;
  let totalSaved = 0;

  // Pre-load known external IDs for client-side dedup (TwitterAPI.io has no since_id param)
  const knownIds = new Set<string>();
  if (sinceId) {
    const { data: recentPosts } = await supabase
      .from("agent_workspace")
      .select("metadata")
      .eq("artifact_type", "x_post")
      .or(`metadata->>author.eq.${cleanName},metadata->>author.eq.${handleNoAt},metadata->>author.ilike.%${handleNoAt}%`)
      .order("created_at", { ascending: false })
      .limit(100);
    if (recentPosts) {
      for (const p of recentPosts) {
        if (p.metadata?.external_id) knownIds.add(p.metadata.external_id);
      }
    }
  }

  async function fetchAndSaveBatch(params: {
    forwardSync?: boolean;
    startTime?: string;
    limit?: number;
    source?: XCreditSource;
    maxPages?: number;
    stopOnKnownPage?: boolean;
    interactive?: boolean;
  }) {
    let cursor: string | undefined;
    let fetchedForBatch = 0;
    let startTimeReached = false;
    let pageCount = 0;
    const source: XCreditSource = params.source || "other";
    // startTime kann ISO ("2026-09-14T11:00:00Z") oder ein anderes Format sein.
    // Der Tweet-Zeitstempel kommt im X-Format ("Mon Sep 14 ... 2026") – ein reiner
    // String-Vergleich wäre falsch, deshalb beide Seiten als Epoch vergleichen.
    const startEpoch = params.startTime ? tweetEpoch(params.startTime) : 0;

    while (true) {
      if (signal?.aborted) throw new Error("Sync wurde abgebrochen.");
      // Kostenbremse: pro Zyklus ist bei X_MAX_CREDITS_PER_CYCLE Schluss.
      // Die bereits bezogene Seite ist bezahlt, weitere Seiten werden nicht mehr geholt.
      if (creditBudgetExceeded()) {
        noteBudgetStop(source);
        break;
      }

      const json = await twitterApiIoFetch("user/tweet_timeline", { userId, cursor }, { interactive: params.interactive === true });
      const tweets: any[] = json.tweets || (Array.isArray(json.data) ? json.data : json.data?.tweets) || [];

      trackTweetsFetched(tweets.length, source);
      pageCount++;

      if (tweets.length === 0) break;

      const recordsToInsert: any[] = [];
      // WICHTIG: Bei bekanntem Tweet darf NICHT per `return` abgebrochen werden –
      // sonst gehen die bereits eingesammelten NEUEN Posts dieser Seite verloren
      // (die Seite enthält im Normalfall neue UND bekannte Posts).
      let reachedKnownId = false;

      for (const tweet of tweets) {
        // Forward-sync client-side dedup: stop when we hit already-known territory
        if (params.forwardSync && (knownIds.has(tweet.id) || (sinceId && tweet.id === sinceId))) {
          reachedKnownId = true;
          break; // tweets are reverse-chronological → rest is older; collected records werden gespeichert
        }

        // Backward-sync time filter: stop once tweets are older than startTime
        if (startEpoch && tweetEpoch(tweet.createdAt) && tweetEpoch(tweet.createdAt) < startEpoch) {
          startTimeReached = true;
          break; // tweets are descending → rest is also older than startTime
        }

        recordsToInsert.push(buildPostRecord(tweet, cleanName, screenName));
      }

      // Upsert into agent_workspace, checking existing external id
      let savedThisPage = 0;
      let knownThisPage = 0;
      for (const rec of recordsToInsert) {
        const externalId = rec.metadata.external_id;
        if (knownIds.has(externalId)) {
          knownThisPage++;
          continue;
        }

        const { data: existing } = await supabase
          .from("agent_workspace")
          .select("id")
          .eq("artifact_type", "x_post")
          .eq("metadata->>external_id", externalId)
          .single();

        if (!existing) {
          const { error: insErr } = await supabase.from("agent_workspace").insert(rec);
          if (!insErr) {
            savedThisPage++;
            totalSaved++;
            xIngestionStats.totalPostsIngested++;
            if (params.forwardSync) knownIds.add(externalId);
          }
        } else {
          knownThisPage++;
          if (params.forwardSync) knownIds.add(externalId);
        }
      }

      fetchedForBatch += tweets.length;

      const hasNextPage = Boolean(json.has_next_page ?? json.data?.has_next_page);
      const nextCursor = json.next_cursor ?? json.data?.next_cursor;

      if (hasNextPage && nextCursor) {
        cursor = nextCursor;
      } else {
        break;
      }

      if (params.limit && fetchedForBatch >= params.limit) break;
      if (reachedKnownId) break; // bekannter Tweet erreicht → keine älteren Seiten mehr nötig
      if (startTimeReached) break;
      // Seiten-Obergrenze pro Influencer (Kostenschutz beim Sicherheitsnetz-Abgleich).
      if (params.maxPages && pageCount >= params.maxPages) break;
      // Abgleich-Modus: eine Seite, die ausschließlich bereits bekannte Posts enthält,
      // bedeutet "das hat die kontinuierliche Suche schon abgedeckt" → tiefere Seiten
      // wären reine Duplikate zum vollen Tweet-Preis (gemessen: 87% Verschwendung).
      if (params.stopOnKnownPage && knownThisPage > 0 && savedThisPage === 0) {
        log.debug(`[X Ingestion] ${cleanName}: Seite ${pageCount} vollständig bekannt – Paginierung gestoppt.`);
        break;
      }
    }
  }

  if (onlyForward) {
    // Mode 1: Routine Forward Sync (Discovery Loop) – fetch newest tweets until known territory
    await fetchAndSaveBatch({ forwardSync: true, limit: targetLimit, source: options?.source || "timeline" });
  } else {
    // Mode 2: Initial Backfill or Historical Backfill – paginate backwards until limit or startTime is reached
    const backfillLimit = targetLimit || X_INITIAL_BACKFILL_LIMIT;
    await fetchAndSaveBatch({
      forwardSync: false,
      startTime,
      limit: backfillLimit,
      source: options?.source || "other",
      maxPages: options?.maxPages,
      stopOnKnownPage: options?.stopOnKnownPage,
      interactive: options?.interactive,
    });
  }

  return totalSaved;
}

/**
 * Billiger Liveness-Check ("Hat der Influencer seit dem letzten Sync gepostet?").
 *
 * Statt für JEDEN Influencer eine Timeline-Page zu ziehen (≈300 Credits = $0.003),
 * holen wir in EINEM Batch-Call nur die User-Profile (18 Credits/User) und
 * vergleichen `statusesCount`. Nur bei Änderung wird die teure Timeline geladen.
 *
 * Das ist verlustfrei: postet ein Account mehr als eine Page seit dem letzten Sync,
 * paginiert ingestInfluencerTweets bis zum letzten bekannten Tweet zurück.
 */
export async function fetchUserStatusCounts(
  users: Array<{ username: string; x_id: string }>,
): Promise<Map<string, number>> {
  const result = new Map<string, number>();
  const CHUNK_SIZE = 100; // ab 100 Usern/Call sinkt der Preis auf 10 Credits/User

  for (let i = 0; i < users.length; i += CHUNK_SIZE) {
    if (creditBudgetExceeded()) {
      noteBudgetStop("liveness");
      break;
    }
    const chunk = users.slice(i, i + CHUNK_SIZE);
    const json = await twitterApiIoFetch("user/batch_info_by_ids", {
      userIds: chunk.map((u) => u.x_id).join(","),
    });
    const list: any[] = json.users || json.data?.users || [];
    for (const u of list) {
      if (u?.id !== undefined && typeof u.statusesCount === "number") {
        result.set(String(u.id), u.statusesCount);
      }
    }
    xIngestionStats.lastCycleUsersChecked += chunk.length;
    const credits = chunk.length * TWITTERAPI_CREDITS_PER_USER_CHECK;
    xIngestionStats.lastCycleCredits += credits;
    xIngestionStats.totalCredits += credits;
    for (const bucket of ["lastCycleCreditsBySource", "totalCreditsBySource"] as const) {
      xIngestionStats[bucket]["liveness"] = (xIngestionStats[bucket]["liveness"] || 0) + credits;
    }
  }

  return result;
}

/** Ein Timeline-Sweep über alle aktiven Influencer (optional Liveness-gefiltert). */
async function runTimelineSweep(): Promise<void> {
  // Nur Favoriten laufen im Regel-Sync (Geld-Gate).
  const { data: influencers } = await supabase
    .from("x_users")
    .select("username, x_id, statuses_count")
    .eq("is_active", true)
    .eq("favorite", true);

  if (!influencers || influencers.length === 0) return;

  let toSync: any[] = influencers;
  let livenessCounts: Map<string, number> | null = null;

  // Stufe 1: günstiger Liveness-Check – nur geänderte Accounts werden gesynct
  if (X_LIVENESS_CHECK) {
    try {
      livenessCounts = await fetchUserStatusCounts(influencers as any[]);
      toSync = influencers.filter((u: any) => {
        const current = livenessCounts!.get(String(u.x_id));
        if (current === undefined) return true; // kein Profil erhalten -> sicherheitshalber syncen
        if (u.statuses_count === null || u.statuses_count === undefined) return true; // Baseline fehlt
        return current !== u.statuses_count;
      });
      log.info(
        `[X Ingestion] Liveness: ${toSync.length}/${influencers.length} Influencer mit neuen Posts ` +
        `(Checks: ${xIngestionStats.lastCycleUsersChecked}, Credits bisher: ${xIngestionStats.lastCycleCredits})`,
      );
    } catch (e: any) {
      log.warn(`[X Ingestion] Liveness-Check fehlgeschlagen (${e.message}) – Fallback: alle Influencer syncen.`);
      livenessCounts = null;
      toSync = influencers as any[];
    }
  }

  for (const inf of toSync) {
    if (!xIngestionAbortController || xIngestionAbortController.signal.aborted) break;
    if (creditBudgetExceeded()) {
      noteBudgetStop("timeline_sweep");
      break;
    }
    const cleanName = `@${inf.username}`;
    if (activeSyncControllers.has(cleanName)) continue;

    const controller = new AbortController();
    activeSyncControllers.set(cleanName, controller);

    try {
      const count = await ingestInfluencerTweets(cleanName, inf.username, undefined, undefined, controller.signal, true, { source: "timeline" });
      xIngestionStats.lastCycleUsersSynced++;
      if (count > 0) {
        await recordSyncLog("ingestion", inf.username, `${count} neue Posts empfangen (Status: pending_metadata)`);
      }
      // statusesCount erst NACH erfolgreichem Sync fortschreiben
      const current = livenessCounts?.get(String(inf.x_id));
      if (current !== undefined && current !== inf.statuses_count) {
        const { error: countErr } = await supabase
          .from("x_users")
          .update({ statuses_count: current })
          .eq("username", inf.username);
        if (countErr) {
          log.warn(`[X Ingestion] statuses_count-Update für @${inf.username} fehlgeschlagen: ${countErr.message}`);
        }
      }
    } catch (e: any) {
      log.error(`[X Ingestion] Fehler bei ${cleanName}: ${e.message}`);
    } finally {
      activeSyncControllers.delete(cleanName);
    }
  }

  log.info(
    `[X Ingestion] Timeline-Sweep ${xIngestionStats.cycleCount} fertig: ` +
    `${xIngestionStats.lastCycleUsersSynced}/${influencers.length} gesynct, ` +
    `${xIngestionStats.lastCycleTweetsFetched} Tweets geladen, ` +
    `~${xIngestionStats.lastCycleCredits} Credits (~$${(xIngestionStats.lastCycleCredits / 100000).toFixed(4)})`,
  );
}

// =====================================================================
// Such-Modus: advanced_search + since_time
// Abrechnung pro GELIEFERTEM Tweet (15 Credits) statt pro 20er-Timeline-Page.
// Retweets liefert die Suche nur mit dem Filter "filter:nativeretweets",
// deshalb pro Chunk zwei Queries (Originale + Retweets).
// =====================================================================
const X_SEARCH_QUERY_CHAR_LIMIT = 440; // harte API-Grenze ist 512 Zeichen
// Täglicher Betrieb liest ein ~24h-Fenster: pro Chunk/Query können das 10-15
// Seiten sein (20 Tweets/Seite). 30 Seiten decken auch einen 2-Tage-Rückstand;
// die harte Kostengrenze bleibt das Credit-Budget pro Phase.
const X_SEARCH_MAX_PAGES = 30;
const X_SEARCH_BOOTSTRAP_SEC = 3600;   // ohne gespeicherten Cursor: letzte Stunde

/** Teilt Influencer in Query-Chunks, die unter dem Zeichenlimit bleiben. */
export function chunkUsersForSearch<T extends { username: string }>(users: T[]): T[][] {
  const chunks: T[][] = [];
  let current: T[] = [];
  let len = 0;

  for (const u of users) {
    const part = (current.length > 0 ? " OR " : "") + `from:${u.username}`;
    if (current.length > 0 && len + part.length > X_SEARCH_QUERY_CHAR_LIMIT) {
      chunks.push(current);
      current = [];
      len = 0;
    }
    const add = (current.length > 0 ? " OR " : "") + `from:${u.username}`;
    current.push(u);
    len += add.length;
  }
  if (current.length > 0) chunks.push(current);
  return chunks;
}

/**
 * Eine Such-Query (Originale oder Retweets) inkl. Pagination.
 * `truncated: true` heißt: Es gäbe weitere Seiten, wir haben aber bei
 * X_SEARCH_MAX_PAGES bzw. am Credit-Budget abgebrochen – das Fenster ist NICHT
 * vollständig gelesen und muss vom Timeline-Abgleich nachgeholt werden.
 */
async function searchChunk(
  users: any[],
  sinceEpoch: number,
  onlyRetweets: boolean,
): Promise<{ saved: number; truncated: boolean }> {
  const handles = users.map((u) => `from:${u.username}`).join(" OR ");
  // Wichtig: -filter:replies, sonst liefert die Suche auch Antworten (bläht Volumen
  // und Kosten auf und passt nicht zum bisherigen Korpus).
  // Retweets kommen nur mit filter:nativeretweets (beide Filter lassen sich nicht kombinieren).
  const query = `(${handles}) since_time:${sinceEpoch} ` +
    (onlyRetweets ? "filter:nativeretweets" : "-filter:replies");
  const byHandle = new Map<string, any>(users.map((u) => [String(u.username).toLowerCase(), u]));

  let saved = 0;
  let truncated = false;
  let cursor: string | undefined;
  const source: XCreditSource = onlyRetweets ? "search_rt" : "search";

  for (let page = 0; page < X_SEARCH_MAX_PAGES; page++) {
    // Kostenbremse: keine weitere Seite, sobald das Zyklus-Budget erreicht ist.
    if (creditBudgetExceeded()) {
      noteBudgetStop(source);
      truncated = true;
      break;
    }
    const json = await twitterApiIoFetch("tweet/advanced_search", { query, queryType: "Latest", cursor });
    const tweets: any[] = json.tweets || [];
    trackTweetsFetched(tweets.length, source);
    if (tweets.length === 0) break;

    const records: any[] = [];
    let reachedOld = false;
    for (const t of tweets) {
      if (tweetEpoch(t.createdAt) && tweetEpoch(t.createdAt) < sinceEpoch) {
        reachedOld = true;
        break;
      }
      const uname = String(t.author?.userName || "").toLowerCase();
      const inf = byHandle.get(uname);
      records.push(buildPostRecord(
        t,
        inf ? `@${inf.username}` : `@${uname}`,
        inf?.screen_name || t.author?.name || uname,
      ));
    }

    saved += await savePostRecords(records);
    if (reachedOld) break; // Fensteranfang erreicht → vollständig

    const hasNext = Boolean(json.has_next_page ?? json.data?.has_next_page);
    const next = json.next_cursor ?? json.data?.next_cursor;
    if (!hasNext || !next) break; // Index hat nichts mehr → vollständig
    if (page === X_SEARCH_MAX_PAGES - 1) {
      // Es gäbe weitere Seiten, aber das Seitenlimit ist erreicht.
      truncated = true;
      break;
    }
    cursor = next;
  }

  return { saved, truncated };
}

/**
 * Suchlauf über alle aktiven Influencer für das Fenster [search_since - Overlap, jetzt].
 *
 * Schreibt den Cursor NICHT selbst fort: der Aufrufer entscheidet anhand von
 * `failed`/`truncated`, ob das Fenster wirklich vollständig gelesen wurde
 * (sonst übernimmt der Timeline-Abgleich als Lücken-Schließer).
 */
export async function runSearchDiscovery(): Promise<{
  saved: number;
  failed: number;
  truncated: boolean;
  since: number;
  sinceRequested: number;
  until: number;
  budgetHit: boolean;
  paymentBlocked: boolean;
  windowTruncated: boolean;
}> {
  const state = await loadIngestionState();
  const now = Math.floor(Date.now() / 1000);
  let since = state.search_since > 0
    ? Math.max(0, state.search_since - X_SEARCH_OVERLAP_SEC)
    : now - X_SEARCH_BOOTSTRAP_SEC;
  const sinceRequested = since; // vor einer Deckelung – für den Lücken-Schließer
  let windowTruncated = false;

  // Sehr großer Rückstand (Container war lange aus): Das Fenster wird nicht
  // verworfen, sondern gedeckelt – der Rest läuft über den Timeline-Abgleich.
  if (now - since > X_SEARCH_MAX_CATCHUP_SEC) {
    log.warn(
      `[X Ingestion] Such-Cursor ist ${Math.round((now - since) / 3600)}h alt – Suche deckelt das ` +
      `Fenster auf ${Math.round(X_SEARCH_MAX_CATCHUP_SEC / 3600)}h; der ältere Teil läuft über den Timeline-Abgleich.`,
    );
    since = now - X_SEARCH_MAX_CATCHUP_SEC;
    windowTruncated = true;
  }

  xIngestionStats.lastSearchSince = since;

  // Nur Favoriten laufen im Regel-Sync (Geld-Gate). Nicht-Favoriten bleiben
  // durchsuchbar und können per Tier 2 gezielt live nachgeladen werden.
  const { data: users } = await supabase
    .from("x_users")
    .select("username, x_id, screen_name")
    .eq("is_active", true)
    .eq("favorite", true);

  let saved = 0;
  let failed = 0;
  const chunks = chunkUsersForSearch((users || []) as any[]);
  const creditsBefore = xIngestionStats.lastCycleCredits;
  const tweetsBefore = xIngestionStats.lastCycleTweetsFetched;
  const budgetStopsBefore = xIngestionStats.budgetStops;
  let paymentBlocked = false;

  let truncated = windowTruncated;

  for (const chunk of chunks) {
    if (!xIngestionAbortController || xIngestionAbortController.signal.aborted) {
      truncated = true;
      break;
    }
    // Guthaben-Sperre (402): nicht 12 Requests gegen eine leere Kasse feuern.
    if (paymentBlocked) break;
    for (const onlyRetweets of [false, true]) {
      try {
        const res = await searchChunk(chunk, since, onlyRetweets);
        saved += res.saved;
        if (res.truncated) truncated = true;
      } catch (e: any) {
        failed++;
        truncated = true; // fehlender Chunk = unvollständiges Fenster
        if (e.message.includes("402") || e.message.includes("pausiert bis")) {
          paymentBlocked = true;
          break;
        }
        log.error(`[X Ingestion] Suche fehlgeschlagen (${onlyRetweets ? "RT" : "Original"}, ${chunk.length} Handles): ${e.message}`);
      }
    }
  }

  const budgetHit = xIngestionStats.budgetStops > budgetStopsBefore;

  xIngestionStats.lastSearchSaved = saved;
  xIngestionStats.lastSearchFailed = failed;
  xIngestionStats.lastSearchCredits = xIngestionStats.lastCycleCredits - creditsBefore;
  xIngestionStats.lastSearchTweetsFetched = xIngestionStats.lastCycleTweetsFetched - tweetsBefore;
  xIngestionStats.lastSearchTruncated = truncated;

  log.info(
    `[X Ingestion] Suche: ${saved} neue Posts seit ${new Date(since * 1000).toISOString()} ` +
    `(${chunks.length} Chunks x 2 Queries, ${failed} Fehler, ` +
    `${xIngestionStats.lastSearchTweetsFetched} Tweets geladen, ~$${(xIngestionStats.lastSearchCredits / 100000).toFixed(4)}` +
    `${truncated ? " | ⚠️ Fenster NICHT vollständig" : " | ✅ Fenster vollständig"}` +
    `${paymentBlocked ? " | ⛔ Guthaben-Sperre aktiv (402)" : ""}${budgetHit ? " | ⚠️ Budget-Stopp" : ""})`,
  );

  return { saved, failed, truncated, since, sinceRequested, until: now, budgetHit, paymentBlocked, windowTruncated };
}

/**
 * Sicherheitsnetz-Abgleich über die Timeline.
 * Fängt alles ab, was der Suchindex nicht liefert (vereinzelte Posts/Replies).
 *
 * KOSTEN (gemessen 2026-09-21): Der alte Modus paginierte pro Influencer das
 * komplette Fenster durch (X_RECONCILE_LIMIT = 2000) und bezog 9.961 Tweets für
 * 1.256 neue Posts – 87% waren Duplikate zum vollen Preis (~$1.49 pro Lauf,
 * 37% des gesamten Credit-Verbrauchs). Deshalb jetzt:
 *   - Fenster auf X_RECONCILE_MAX_LOOKBACK_SEC begrenzt (Default 3 Tage),
 *   - max. X_RECONCILE_MAX_PAGES_PER_USER Seiten pro Influencer,
 *   - Abbruch, sobald eine Seite nur bekannte Posts enthält (Suchabdeckung erreicht).
 */
export async function runTimelineReconciliation(
  forceSinceMs?: number,
): Promise<{ saved: number; failed: number; paymentBlocked: boolean; budgetHit: boolean; sinceMs: number }> {
  const state = await loadIngestionState();
  const nowMs = Date.now();
  let sinceMs = state.last_reconcile > 0
    ? state.last_reconcile
    : nowMs - X_RECONCILE_INTERVAL_SEC * 1000;
  // Bei einem größeren Such-Rückstand muss der Abgleich weiter zurückreichen
  if (forceSinceMs && forceSinceMs > 0 && forceSinceMs < sinceMs) sinceMs = forceSinceMs;
  // ... aber nie weiter als das Lookback-Limit: ältere Fenster bestehen fast nur
  // noch aus bereits bezahlten Duplikaten (die Suche deckt das Tagesgeschäft ab).
  const lookbackFloor = nowMs - X_RECONCILE_MAX_LOOKBACK_SEC * 1000;
  if (sinceMs < lookbackFloor) {
    log.info(
      `[X Ingestion] Abgleich-Fenster wird auf ${Math.round(X_RECONCILE_MAX_LOOKBACK_SEC / 3600)}h begrenzt ` +
      `(statt ${Math.round((nowMs - sinceMs) / 3600000)}h) – ältere Zeiträume sind bereits abgedeckt.`,
    );
    sinceMs = lookbackFloor;
  }
  const startTime = new Date(sinceMs).toISOString();

  const { data: users } = await supabase
    .from("x_users")
    .select("username, x_id, screen_name")
    .eq("is_active", true)
    .eq("favorite", true);

  let saved = 0;
  let failed = 0;
  let skipped = 0;
  const creditsBefore = xIngestionStats.lastCycleCredits;
  const tweetsBefore = xIngestionStats.lastCycleTweetsFetched;
  const budgetStopsBefore = xIngestionStats.budgetStops;
  let paymentBlocked = false;

  log.info(`[X Ingestion] Timeline-Abgleich startet (seit ${startTime}, ${(users || []).length} Influencer)...`);

  for (const inf of (users || []) as any[]) {
    if (!xIngestionAbortController || xIngestionAbortController.signal.aborted) break;
    // Guthaben-Sperre (402): sofort raus, nicht 119x gegen eine leere Kasse laufen.
    if (paymentBlocked) {
      skipped++;
      continue;
    }
    if (creditBudgetExceeded()) {
      noteBudgetStop("reconcile");
      skipped++;
      continue;
    }
    const cleanName = `@${inf.username}`;
    if (activeSyncControllers.has(cleanName)) continue;

    const controller = new AbortController();
    activeSyncControllers.set(cleanName, controller);
    try {
      saved += await ingestInfluencerTweets(
        cleanName,
        inf.username,
        X_RECONCILE_LIMIT,
        startTime,
        controller.signal,
        false,
        {
          source: "reconcile",
          maxPages: X_RECONCILE_MAX_PAGES_PER_USER,
          stopOnKnownPage: X_RECONCILE_STOP_ON_KNOWN_PAGE,
        },
      );
    } catch (e: any) {
      failed++;
      if (e.message.includes("402") || e.message.includes("pausiert bis")) {
        paymentBlocked = true;
        log.warn(`[X Ingestion] Abgleich abgebrochen: Guthaben-Sperre aktiv (402).`);
      } else {
        log.warn(`[X Ingestion] Abgleich für ${cleanName} fehlgeschlagen: ${e.message}`);
      }
    } finally {
      activeSyncControllers.delete(cleanName);
    }
  }

  const budgetHit = xIngestionStats.budgetStops > budgetStopsBefore;

  // Zeitstempel fortschreiben, sobald der Lauf sauber durch ist ODER am Budget
  // abgebrochen wurde. Bei einem Budget-Stopp wäre ein erneuter Lauf über dasselbe
  // (zu teure) Fenster eine Kostenfalle. Fehler- und 402-Läufe bleiben dagegen
  // stehen: der nächste Zyklus holt sie nach – ohne Guthaben kostet das nichts.
  if ((failed === 0 && !paymentBlocked) || budgetHit) {
    await saveIngestionState({ ...state, last_reconcile: nowMs });
  } else {
    log.warn(`[X Ingestion] Abgleich mit ${failed} Fehlern – Zeitstempel bleibt stehen, nächster Lauf holt nach.`);
  }

  xIngestionStats.lastReconcileSaved = saved;
  xIngestionStats.lastReconcileAt = Date.now();
  xIngestionStats.lastReconcileSkipped = skipped;
  xIngestionStats.lastReconcileCredits = xIngestionStats.lastCycleCredits - creditsBefore;
  xIngestionStats.lastReconcileTweetsFetched = xIngestionStats.lastCycleTweetsFetched - tweetsBefore;
  log.info(
    `[X Ingestion] Timeline-Abgleich fertig: ${saved} neue Posts (${failed} Fehler` +
    `${skipped ? `, ${skipped} übersprungen` : ""}, ` +
    `${xIngestionStats.lastReconcileTweetsFetched} Tweets geladen, ` +
    `~$${(xIngestionStats.lastReconcileCredits / 100000).toFixed(4)}` +
    `${paymentBlocked ? " | ⛔ Guthaben-Sperre (402)" : ""}${budgetHit ? " | ⚠️ Budget-Stopp" : ""})`,
  );
  return { saved, failed, paymentBlocked, budgetHit, sinceMs };
}

function resetCycleStats() {
  xIngestionStats.lastCycleTweetsFetched = 0;
  xIngestionStats.lastCycleUsersChecked = 0;
  xIngestionStats.lastCycleUsersSynced = 0;
  xIngestionStats.lastCycleCredits = 0;
  xIngestionStats.lastCycleCreditsBySource = {};
  xIngestionStats.lastSearchTweetsFetched = 0;
  xIngestionStats.lastSearchCredits = 0;
  xIngestionStats.lastReconcileTweetsFetched = 0;
  xIngestionStats.lastReconcileCredits = 0;
  xIngestionStats.lastReconcileSkipped = 0;
  xIngestionStats.lastSearchTruncated = false;
  budgetStopNotedThisCycle = false;
  // Guthaben-Sperre aus shared.ts spiegeln, damit sie im STATUS sichtbar ist.
  xIngestionStats.paymentBlockedUntil = twitterApiIoStats.blockedUntil;
  xIngestionStats.paymentErrors402 = twitterApiIoStats.paymentErrors402;
}

async function interruptibleWait(ms: number) {
  let waited = 0;
  while (waited < ms && xIngestionAbortController && !xIngestionAbortController.signal.aborted) {
    if (manualRunRequested) return; // manueller Lauf → sofort aufwachen
    const chunk = Math.min(2000, ms - waited);
    await new Promise((r) => setTimeout(r, chunk));
    waited += chunk;
  }
}

/** Wie lange bis zum nächsten planmäßigen Lauf (oder Intervall)? */
function nextWaitMs(mode: "search" | "timeline"): number {
  if (mode === "search" && parseDailyRunAt()) {
    const next = nextDailyTargetMs(Date.now());
    if (next) return Math.max(1000, next - Date.now());
  }
  return (mode === "search" ? X_SEARCH_INTERVAL_SEC : X_DISCOVERY_INTERVAL_SEC) * 1000;
}

/** true, wenn der tägliche Slot seit dem letzten erfolgreichen Lauf durchgelaufen ist. */
async function isDailyDue(): Promise<boolean> {
  const state = await loadIngestionState();
  xIngestionStats.nextRunAt = nextDailyTargetMs(Date.now()) || 0;
  return isDailyRunDue(Date.now(), state.search_since > 0 ? state.search_since * 1000 : 0);
}

/**
 * Stage 1 Periodic Discovery Loop
 */
export async function runXIngestionLoop() {
  xIngestionStats.isRunning = true;
  xIngestionStats.startTime = Date.now();
  const mode = X_INGESTION_MODE === "timeline" ? "timeline" : "search";
  const dailyAt = parseDailyRunAt();
  const dailyMode = mode === "search" && dailyAt !== null;
  await recordSyncLog(
    "started",
    null,
    mode === "search"
      ? dailyMode
        ? `X-Ingestion-Loop gestartet (Modus: Suche, TÄGLICH um ${pad2(dailyAt!.hour)}:${pad2(dailyAt!.minute)} ${X_DAILY_RUN_TZ} – verpasste Slots werden nachgeholt; manuell über SYNC_NOW; Timeline-Abgleich alle ${Math.round(X_RECONCILE_INTERVAL_SEC / 3600)}h)`
        : `X-Ingestion-Loop gestartet (Modus: Suche, Intervall: ${X_SEARCH_INTERVAL_SEC}s, Timeline-Abgleich alle ${Math.round(X_RECONCILE_INTERVAL_SEC / 3600)}h)`
      : `X-Ingestion-Loop gestartet (Modus: Timeline, Intervall: ${X_DISCOVERY_INTERVAL_SEC}s, Liveness-Check: ${X_LIVENESS_CHECK ? "an" : "aus"})`,
  );

  while (xIngestionAbortController && !xIngestionAbortController.signal.aborted) {
    try {
      // --- Laufzeitplan (nur Such-Modus) ---
      if (dailyMode) {
        const due = manualRunRequested || await isDailyDue();
        xIngestionStats.manualRunPending = manualRunRequested;
        if (!due) {
          await interruptibleWait(nextWaitMs("search"));
          continue;
        }
        log.info(
          `[X Ingestion] Lauf startet (${manualRunRequested ? "manuell angefordert" : "täglicher Slot " + X_DAILY_RUN_AT + " " + X_DAILY_RUN_TZ}).`,
        );
      } else if (mode === "search") {
        xIngestionStats.nextRunAt = Date.now() + X_SEARCH_INTERVAL_SEC * 1000;
      }
      manualRunRequested = false;
      xIngestionStats.manualRunPending = false;

      xIngestionStats.lastRunTime = Date.now();
      xIngestionStats.cycleCount++;
      resetCycleStats();
      resetPhaseBudget();

      if (mode === "search") {
        // Guthaben-Sperre (402): Zyklus komplett überspringen statt gegen eine leere
        // Kasse zu laufen. Danach normal weiter (die Sperre läuft von selbst ab).
        if (twitterApiIoStats.blockedUntil > Date.now()) {
          log.warn(
            `[X Ingestion] Zyklus ${xIngestionStats.cycleCount} übersprungen – TwitterAPI.io-Guthaben ` +
            `aufgebraucht, Sperre bis ${new Date(twitterApiIoStats.blockedUntil).toISOString()}.`,
          );
          xIngestionStats.paymentBlockedUntil = twitterApiIoStats.blockedUntil;
          await interruptibleWait(Math.min(X_SEARCH_INTERVAL_SEC * 1000, 600000));
          continue;
        }
        // 1) Fälliger Sicherheitsnetz-Abgleich (Intervall über X_RECONCILE_INTERVAL_SEC)
        const state = await loadIngestionState();
        const reconcileDue = Date.now() - (state.last_reconcile || 0) >= X_RECONCILE_INTERVAL_SEC * 1000;
        if (reconcileDue) {
          await runTimelineReconciliation();
          resetPhaseBudget();
        }

        // 2) Suche über das offene Fenster
        const search = await runSearchDiscovery();
        let covered = search.failed === 0 && !search.truncated && !search.paymentBlocked;

        // 3) Lücken-Schließer: Wurde das Fenster NICHT vollständig gelesen (Fehler,
        //    Seitenlimit, Budget), holt der Timeline-Abgleich genau dieses Fenster
        //    nach. Erst danach darf der Such-Cursor weiterrücken – so entstehen
        //    keine stillen Lücken.
        if (!covered) {
          const gapSinceMs = (search.windowTruncated ? search.sinceRequested : search.since) * 1000;
          log.warn(
            `[X Ingestion] Suchfenster unvollständig (Fehler=${search.failed}, ` +
            `Seitenlimit=${search.truncated}) – Timeline-Abgleich schließt ` +
            `${new Date(gapSinceMs).toISOString()}..jetzt.`,
          );
          resetPhaseBudget();
          const rec = await runTimelineReconciliation(gapSinceMs);
          xIngestionStats.gapCloserRuns++;
          xIngestionStats.gapCloserSaved += rec.saved;
          // Nur ein VOLLSTÄNDIG durchgelaufener Abgleich deckt das Fenster ab –
          // ein am Budget gestoppter Lauf hat die restlichen Accounts nie geprüft.
          covered = rec.failed === 0 && !rec.paymentBlocked && !rec.budgetHit;
          if (!covered) {
            xIngestionStats.coverageFailures++;
            log.error(
              `[X Ingestion] ⚠️ Lücke NICHT geschlossen (Abgleich: ${rec.failed} Fehler` +
              `${rec.paymentBlocked ? ", Guthaben leer" : ""}, Budget=${rec.budgetHit}) – ` +
              `Cursor bleibt stehen, der nächste Lauf holt das Fenster erneut.`,
            );
          }
        }

        // 4) Cursor erst fortschreiben, wenn das Fenster abgedeckt ist.
        const freshState = await loadIngestionState();
        if (covered) {
          await saveIngestionState({ ...freshState, search_since: search.until });
        }
      } else {
        await runTimelineSweep();
        log.info(
          `[X Ingestion] Zyklus ${xIngestionStats.cycleCount} fertig: ` +
          `${xIngestionStats.lastCycleUsersSynced} gesynct, ` +
          `${xIngestionStats.lastCycleTweetsFetched} Tweets geladen, ` +
          `~${xIngestionStats.lastCycleCredits} Credits (~$${(xIngestionStats.lastCycleCredits / 100000).toFixed(4)})`,
        );
      }

      // Zyklus-Kostenbilanz (Quellen getrennt – sonst verfälscht ein Abgleich die Suchzeile).
      if (mode === "search") {
        const bySource = xIngestionStats.lastCycleCreditsBySource;
        const parts = Object.entries(bySource)
          .filter(([, c]) => c > 0)
          .map(([src, c]) => `${src}=${c}`)
          .join(", ");
        log.info(
          `[X Ingestion] Zyklus ${xIngestionStats.cycleCount} Kosten: ${xIngestionStats.lastCycleCredits} Credits ` +
          `(~$${(xIngestionStats.lastCycleCredits / 100000).toFixed(4)})${parts ? ` [${parts}]` : ""}`,
        );
      }

      xIngestionStats.lastCycleFinishedAt = Date.now();
      if (dailyMode) {
        // Bis zum nächsten Slot schlafen (manueller Lauf weckt sofort auf).
        xIngestionStats.nextRunAt = nextDailyTargetMs(Date.now()) || 0;
        await interruptibleWait(nextWaitMs("search"));
      } else {
        await interruptibleWait((mode === "search" ? X_SEARCH_INTERVAL_SEC : X_DISCOVERY_INTERVAL_SEC) * 1000);
      }
    } catch (err: any) {
      if (err.name === "AbortError") break;
      xIngestionStats.lastError = err.message;
      log.error(`[X Ingestion Loop Error]: ${err.message}`);
      await new Promise((r) => setTimeout(r, 15000));
    }
  }

  xIngestionStats.isRunning = false;
  await recordSyncLog("stopped", null, "X-Ingestion-Loop gestoppt");
}

export function startXIngestion() {
  if (xIngestionStats.isRunning) return;
  xIngestionAbortController = new AbortController();
  runXIngestionLoop().catch((err) => log.error(`X Ingestion fatal error: ${err.message}`));
}

export function stopXIngestion() {
  if (xIngestionAbortController) {
    xIngestionAbortController.abort();
    xIngestionAbortController = null;
  }
}
