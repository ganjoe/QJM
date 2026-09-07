import { supabase, log, X_DISCOVERY_INTERVAL_SEC, twitterApiIoFetch, isTwitterApiIoAvailable } from "../tools/shared.ts";

export const activeSyncControllers = new Map<string, AbortController>();

export const xIngestionStats = {
  isRunning: false,
  startTime: 0,
  lastRunTime: 0,
  cycleCount: 0,
  totalPostsIngested: 0,
  lastError: null as string | null,
};

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

    while (true) {
      if (signal?.aborted) throw new Error("Sync wurde abgebrochen.");

      const json = await twitterApiIoFetch("user/tweet_timeline", { userId, cursor });
      const tweets: any[] = json.tweets || [];

      if (tweets.length === 0) break;

      const recordsToInsert: any[] = [];

      for (const tweet of tweets) {
        // Forward-sync client-side dedup: stop when we hit already-known territory
        if (params.forwardSync && (knownIds.has(tweet.id) || (sinceId && tweet.id === sinceId))) {
          return; // tweets are reverse-chronological → rest is older, stop entirely
        }

        // Backward-sync time filter: stop once tweets are older than startTime
        if (params.startTime && tweet.createdAt && tweet.createdAt < params.startTime) {
          startTimeReached = true;
          return; // tweets are descending → rest is also older than startTime
        }

        // Extract media URLs from entities (TwitterAPI.io embeds them; no separate media expansion)
        const mediaUrls: string[] = [];
        if (tweet.entities?.urls) {
          for (const u of tweet.entities.urls) {
            if (u.expanded_url) mediaUrls.push(u.expanded_url);
          }
        }
        // Also check for media in tweet-level media_urls if present
        if (tweet.media_urls) {
          for (const m of tweet.media_urls) mediaUrls.push(m);
        }

        recordsToInsert.push({
          agent_id: "cco",
          artifact_type: "x_post",
          content: tweet.text,
          status: "pending_metadata",
          created_at: tweet.createdAt || new Date().toISOString(),
          metadata: {
            external_id: tweet.id,
            author: cleanName,
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
        });
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

      if (json.has_next_page && json.next_cursor) {
        cursor = json.next_cursor;
      } else {
        break;
      }

      if (params.limit && fetchedForBatch >= params.limit) break;
      if (startTimeReached) break;
    }
  }

  // Phase 1: Forward Sync (newest tweets we haven't seen yet)
  await fetchAndSaveBatch({ forwardSync: true, limit: targetLimit });

  // Phase 2: Optional Backward Sync (historical backfill, if not forward-only)
  if (!onlyForward && startTime) {
    await fetchAndSaveBatch({ startTime, limit: targetLimit });
  }

  return totalSaved;
}

/**
 * Stage 1 Periodic Discovery Loop
 */
export async function runXIngestionLoop() {
  xIngestionStats.isRunning = true;
  xIngestionStats.startTime = Date.now();
  await recordSyncLog("started", null, `X-Ingestion-Loop gestartet (Intervall: ${X_DISCOVERY_INTERVAL_SEC}s)`);

  const intervalMs = X_DISCOVERY_INTERVAL_SEC * 1000;

  while (xIngestionAbortController && !xIngestionAbortController.signal.aborted) {
    try {
      xIngestionStats.lastRunTime = Date.now();
      xIngestionStats.cycleCount++;

      const { data: influencers } = await supabase
        .from("x_users")
        .select("username")
        .eq("is_active", true);

      if (influencers && influencers.length > 0) {
        for (const inf of influencers) {
          if (!xIngestionAbortController || xIngestionAbortController.signal.aborted) break;
          const cleanName = `@${inf.username}`;
          if (activeSyncControllers.has(cleanName)) continue;

          const controller = new AbortController();
          activeSyncControllers.set(cleanName, controller);

          try {
            const count = await ingestInfluencerTweets(
              cleanName,
              inf.username,
              undefined,
              undefined,
              controller.signal,
              true
            );
            if (count > 0) {
              await recordSyncLog("ingestion", inf.username, `${count} neue Posts empfangen (Status: pending_metadata)`);
            }
          } catch (e: any) {
            log.error(`[X Ingestion] Fehler bei ${cleanName}: ${e.message}`);
          } finally {
            activeSyncControllers.delete(cleanName);
          }
        }
      }

      // Interruptible wait
      let waited = 0;
      while (waited < intervalMs && xIngestionAbortController && !xIngestionAbortController.signal.aborted) {
        const chunk = Math.min(2000, intervalMs - waited);
        await new Promise((r) => setTimeout(r, chunk));
        waited += chunk;
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
