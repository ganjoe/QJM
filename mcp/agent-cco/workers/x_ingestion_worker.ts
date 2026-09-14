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
} from "../tools/shared.ts";

export const activeSyncControllers = new Map<string, AbortController>();

// --- TwitterAPI.io Preis-Modell (100.000 Credits = $1) ---
// 15 Credits pro ausgeliefertem Tweet (= $0.15 / 1.000 Tweets)
// 18 Credits pro User beim Batch-Liveness-Check (10 Credits ab 100 Usern pro Call)
export const TWITTERAPI_CREDITS_PER_TWEET = 15;
export const TWITTERAPI_CREDITS_PER_USER_CHECK = 18;

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
  // Such-Modus
  lastSearchSaved: 0,
  lastSearchFailed: 0,
  lastSearchSince: 0,
  lastReconcileSaved: 0,
  lastReconcileAt: 0,
};

function trackTweetsFetched(n: number) {
  if (!n || n <= 0) return;
  xIngestionStats.lastCycleTweetsFetched += n;
  xIngestionStats.totalTweetsFetched += n;
  const credits = n * TWITTERAPI_CREDITS_PER_TWEET;
  xIngestionStats.lastCycleCredits += credits;
  xIngestionStats.totalCredits += credits;
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
  onlyForward: boolean = true
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

  async function fetchAndSaveBatch(params: { forwardSync?: boolean; startTime?: string; limit?: number }) {
    let cursor: string | undefined;
    let fetchedForBatch = 0;
    let startTimeReached = false;
    // startTime kann ISO ("2026-09-14T11:00:00Z") oder ein anderes Format sein.
    // Der Tweet-Zeitstempel kommt im X-Format ("Mon Sep 14 ... 2026") – ein reiner
    // String-Vergleich wäre falsch, deshalb beide Seiten als Epoch vergleichen.
    const startEpoch = params.startTime ? tweetEpoch(params.startTime) : 0;

    while (true) {
      if (signal?.aborted) throw new Error("Sync wurde abgebrochen.");

      const json = await twitterApiIoFetch("user/tweet_timeline", { userId, cursor });
      const tweets: any[] = json.tweets || (Array.isArray(json.data) ? json.data : json.data?.tweets) || [];

      trackTweetsFetched(tweets.length);

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
      for (const rec of recordsToInsert) {
        const { data: existing } = await supabase
          .from("agent_workspace")
          .select("id")
          .eq("artifact_type", "x_post")
          .eq("metadata->>external_id", rec.metadata.external_id)
          .single();

        if (!existing) {
          const { error: insErr } = await supabase.from("agent_workspace").insert(rec);
          if (!insErr) {
            totalSaved++;
            xIngestionStats.totalPostsIngested++;
            if (params.forwardSync) knownIds.add(rec.metadata.external_id);
          }
        } else if (params.forwardSync) {
          knownIds.add(rec.metadata.external_id);
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
    }
  }

  if (onlyForward) {
    // Mode 1: Routine Forward Sync (Discovery Loop) – fetch newest tweets until known territory
    await fetchAndSaveBatch({ forwardSync: true, limit: targetLimit });
  } else {
    // Mode 2: Initial Backfill or Historical Backfill – paginate backwards until limit or startTime is reached
    const backfillLimit = targetLimit || X_INITIAL_BACKFILL_LIMIT;
    await fetchAndSaveBatch({ forwardSync: false, startTime, limit: backfillLimit });
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
  }

  return result;
}

/** Ein Timeline-Sweep über alle aktiven Influencer (optional Liveness-gefiltert). */
async function runTimelineSweep(): Promise<void> {
  const { data: influencers } = await supabase
    .from("x_users")
    .select("username, x_id, statuses_count")
    .eq("is_active", true);

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
    const cleanName = `@${inf.username}`;
    if (activeSyncControllers.has(cleanName)) continue;

    const controller = new AbortController();
    activeSyncControllers.set(cleanName, controller);

    try {
      const count = await ingestInfluencerTweets(cleanName, inf.username, undefined, undefined, controller.signal, true);
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
const X_SEARCH_MAX_PAGES = 10;
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

/** Eine Such-Query (Originale oder Retweets) inkl. Pagination. */
async function searchChunk(users: any[], sinceEpoch: number, onlyRetweets: boolean): Promise<number> {
  const handles = users.map((u) => `from:${u.username}`).join(" OR ");
  // Wichtig: -filter:replies, sonst liefert die Suche auch Antworten (bläht Volumen
  // und Kosten auf und passt nicht zum bisherigen Korpus).
  // Retweets kommen nur mit filter:nativeretweets (beide Filter lassen sich nicht kombinieren).
  const query = `(${handles}) since_time:${sinceEpoch} ` +
    (onlyRetweets ? "filter:nativeretweets" : "-filter:replies");
  const byHandle = new Map<string, any>(users.map((u) => [String(u.username).toLowerCase(), u]));

  let saved = 0;
  let cursor: string | undefined;

  for (let page = 0; page < X_SEARCH_MAX_PAGES; page++) {
    const json = await twitterApiIoFetch("tweet/advanced_search", { query, queryType: "Latest", cursor });
    const tweets: any[] = json.tweets || [];
    trackTweetsFetched(tweets.length);
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
    if (reachedOld) break;

    const hasNext = Boolean(json.has_next_page ?? json.data?.has_next_page);
    const next = json.next_cursor ?? json.data?.next_cursor;
    if (!hasNext || !next) break;
    cursor = next;
  }

  return saved;
}

/** Stündlicher (konfigurierbarer) Suchlauf über alle aktiven Influencer. */
export async function runSearchDiscovery(): Promise<{ saved: number; failed: number; since: number }> {
  const state = await loadIngestionState();
  const now = Math.floor(Date.now() / 1000);
  let since = state.search_since > 0
    ? Math.max(0, state.search_since - X_SEARCH_OVERLAP_SEC)
    : now - X_SEARCH_BOOTSTRAP_SEC;

  // Zu großer Rückstand (z.B. Container war lange aus): nicht die Suche endlos
  // paginieren lassen – das erledigt der Timeline-Abgleich, die Suche macht nur das letzte Fenster.
  if (now - since > X_SEARCH_MAX_CATCHUP_SEC) {
    log.warn(
      `[X Ingestion] Such-Cursor ist ${Math.round((now - since) / 3600)}h alt – ` +
      `überspringe den Großteil (übernimmt der Timeline-Abgleich), Suche startet bei der letzten Stunde.`,
    );
    since = now - X_SEARCH_BOOTSTRAP_SEC;
  }

  xIngestionStats.lastSearchSince = since;

  const { data: users } = await supabase
    .from("x_users")
    .select("username, x_id, screen_name")
    .eq("is_active", true);

  let saved = 0;
  let failed = 0;
  const chunks = chunkUsersForSearch((users || []) as any[]);

  for (const chunk of chunks) {
    if (!xIngestionAbortController || xIngestionAbortController.signal.aborted) break;
    for (const onlyRetweets of [false, true]) {
      try {
        saved += await searchChunk(chunk, since, onlyRetweets);
      } catch (e: any) {
        failed++;
        log.error(`[X Ingestion] Suche fehlgeschlagen (${onlyRetweets ? "RT" : "Original"}, ${chunk.length} Handles): ${e.message}`);
      }
    }
  }

  // Cursor nur fortschreiben, wenn alle Chunks erfolgreich waren –
  // sonst holt der nächste Lauf das Fenster erneut nach.
  if (failed === 0) {
    await saveIngestionState({ ...state, search_since: now });
  }

  xIngestionStats.lastSearchSaved = saved;
  xIngestionStats.lastSearchFailed = failed;

  log.info(
    `[X Ingestion] Suche: ${saved} neue Posts seit ${new Date(since * 1000).toISOString()} ` +
    `(${chunks.length} Chunks x 2 Queries, ${failed} Fehler, ` +
    `${xIngestionStats.lastCycleTweetsFetched} Tweets geladen, ~$${(xIngestionStats.lastCycleCredits / 100000).toFixed(4)})`,
  );

  return { saved, failed, since };
}

/**
 * Wöchentlicher Sicherheitsnetz-Abgleich über die Timeline.
 * Fängt alles ab, was der Suchindex nicht liefert (vereinzelte Posts/Replies).
 * Läuft im Backfill-Modus (zeitbasiert), stoppt also NICHT an bekannten IDs.
 */
export async function runTimelineReconciliation(forceSinceMs?: number): Promise<number> {
  const state = await loadIngestionState();
  const nowMs = Date.now();
  let sinceMs = state.last_reconcile > 0
    ? state.last_reconcile
    : nowMs - X_RECONCILE_INTERVAL_SEC * 1000;
  // Bei einem größeren Such-Rückstand muss der Abgleich weiter zurückreichen
  if (forceSinceMs && forceSinceMs > 0 && forceSinceMs < sinceMs) sinceMs = forceSinceMs;
  const startTime = new Date(sinceMs).toISOString();

  const { data: users } = await supabase
    .from("x_users")
    .select("username, x_id, screen_name")
    .eq("is_active", true);

  let saved = 0;
  let failed = 0;

  log.info(`[X Ingestion] Timeline-Abgleich startet (seit ${startTime}, ${(users || []).length} Influencer)...`);

  for (const inf of (users || []) as any[]) {
    if (!xIngestionAbortController || xIngestionAbortController.signal.aborted) break;
    const cleanName = `@${inf.username}`;
    if (activeSyncControllers.has(cleanName)) continue;

    const controller = new AbortController();
    activeSyncControllers.set(cleanName, controller);
    try {
      saved += await ingestInfluencerTweets(cleanName, inf.username, X_RECONCILE_LIMIT, startTime, controller.signal, false);
    } catch (e: any) {
      failed++;
      log.warn(`[X Ingestion] Abgleich für ${cleanName} fehlgeschlagen: ${e.message}`);
    } finally {
      activeSyncControllers.delete(cleanName);
    }
  }

  if (failed === 0) {
    await saveIngestionState({ ...state, last_reconcile: nowMs });
  } else {
    log.warn(`[X Ingestion] Abgleich mit ${failed} Fehlern – Zeitstempel bleibt stehen, nächster Lauf holt nach.`);
  }

  xIngestionStats.lastReconcileSaved = saved;
  xIngestionStats.lastReconcileAt = Date.now();
  log.info(`[X Ingestion] Timeline-Abgleich fertig: ${saved} neue Posts (${failed} Fehler, ~$${(xIngestionStats.lastCycleCredits / 100000).toFixed(4)})`);
  return saved;
}

function resetCycleStats() {
  xIngestionStats.lastCycleTweetsFetched = 0;
  xIngestionStats.lastCycleUsersChecked = 0;
  xIngestionStats.lastCycleUsersSynced = 0;
  xIngestionStats.lastCycleCredits = 0;
}

async function interruptibleWait(ms: number) {
  let waited = 0;
  while (waited < ms && xIngestionAbortController && !xIngestionAbortController.signal.aborted) {
    const chunk = Math.min(2000, ms - waited);
    await new Promise((r) => setTimeout(r, chunk));
    waited += chunk;
  }
}

/**
 * Stage 1 Periodic Discovery Loop
 */
export async function runXIngestionLoop() {
  xIngestionStats.isRunning = true;
  xIngestionStats.startTime = Date.now();
  const mode = X_INGESTION_MODE === "timeline" ? "timeline" : "search";
  await recordSyncLog(
    "started",
    null,
    mode === "search"
      ? `X-Ingestion-Loop gestartet (Modus: Suche, Intervall: ${X_SEARCH_INTERVAL_SEC}s, Timeline-Abgleich alle ${Math.round(X_RECONCILE_INTERVAL_SEC / 3600)}h)`
      : `X-Ingestion-Loop gestartet (Modus: Timeline, Intervall: ${X_DISCOVERY_INTERVAL_SEC}s, Liveness-Check: ${X_LIVENESS_CHECK ? "an" : "aus"})`,
  );

  while (xIngestionAbortController && !xIngestionAbortController.signal.aborted) {
    try {
      xIngestionStats.lastRunTime = Date.now();
      xIngestionStats.cycleCount++;
      resetCycleStats();

      if (mode === "search") {
        // 1) Fälliger wöchentlicher Sicherheitsnetz-Abgleich
        //    (oder wenn der Such-Cursor zu weit zurückliegt)
        const state = await loadIngestionState();
        const reconcileDue = Date.now() - (state.last_reconcile || 0) >= X_RECONCILE_INTERVAL_SEC * 1000;
        const searchGapSec = state.search_since > 0 ? Math.floor(Date.now() / 1000) - state.search_since : 0;
        if (reconcileDue) {
          await runTimelineReconciliation();
        } else if (searchGapSec > X_SEARCH_MAX_CATCHUP_SEC) {
          await runTimelineReconciliation(state.search_since * 1000);
        }
        // 2) Günstige Suche über alle Influencer
        await runSearchDiscovery();
      } else {
        await runTimelineSweep();
        log.info(
          `[X Ingestion] Zyklus ${xIngestionStats.cycleCount} fertig: ` +
          `${xIngestionStats.lastCycleUsersSynced} gesynct, ` +
          `${xIngestionStats.lastCycleTweetsFetched} Tweets geladen, ` +
          `~${xIngestionStats.lastCycleCredits} Credits (~$${(xIngestionStats.lastCycleCredits / 100000).toFixed(4)})`,
        );
      }

      await interruptibleWait((mode === "search" ? X_SEARCH_INTERVAL_SEC : X_DISCOVERY_INTERVAL_SEC) * 1000);
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
