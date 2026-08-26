import { supabase, log, throttledXFetch, X_BEARER_TOKEN, X_DISCOVERY_INTERVAL_SEC } from "../tools/shared.ts";

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

// Resolve X User ID (Cached in x_users)
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

  const res = await throttledXFetch(`https://api.twitter.com/2/users/by/username/${cleanName}`, {
    headers: { Authorization: `Bearer ${X_BEARER_TOKEN}` },
  });
  if (!res.ok) throw new Error(`X API failed to resolve user @${cleanName}: HTTP ${res.status}`);
  const data = await res.json();
  if (!data.data?.id) throw new Error(`User @${cleanName} not found on X.`);

  const userId = data.data.id;
  const screenName = data.data.name;

  await supabase.from("x_users").upsert({ username: cleanName, x_id: userId, screen_name: screenName });

  return { id: userId, name: screenName };
}

/**
 * Stage 1 Fast Ingestion for a single influencer.
 * Fetches tweets and saves raw records immediately with status = 'pending_metadata'.
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

  // Find latest saved post ID to continue forward sync
  const { data: latestRecord } = await supabase
    .from("agent_workspace")
    .select("metadata")
    .eq("artifact_type", "x_post")
    .or(`metadata->>author.eq.${cleanName},metadata->>author.eq.${handleNoAt},metadata->>author.ilike.%${handleNoAt}%`)
    .order("created_at", { ascending: false })
    .limit(1)
    .single();

  const sinceId = latestRecord?.metadata?.id || undefined;
  let totalSaved = 0;

  async function fetchAndSaveBatch(params: { sinceId?: string; untilId?: string; startTime?: string; limit?: number }) {
    let paginationToken: string | undefined;
    let fetchedForBatch = 0;

    while (true) {
      if (signal?.aborted) throw new Error("Sync wurde abgebrochen.");

      const maxResults = params.limit ? Math.min(params.limit - fetchedForBatch, 100) : 100;
      if (maxResults <= 0) break;

      const url = new URL(`https://api.twitter.com/2/users/${userId}/tweets`);
      url.searchParams.set("tweet.fields", "created_at,text,author_id,public_metrics,entities,attachments");
      url.searchParams.set("expansions", "attachments.media_keys");
      url.searchParams.set("media.fields", "url,preview_image_url,type");
      url.searchParams.set("max_results", maxResults.toString());

      if (params.sinceId) url.searchParams.set("since_id", params.sinceId);
      if (params.untilId) url.searchParams.set("until_id", params.untilId);
      if (params.startTime) url.searchParams.set("start_time", params.startTime);
      if (paginationToken) url.searchParams.set("pagination_token", paginationToken);

      const res = await throttledXFetch(url.toString(), {
        headers: { Authorization: `Bearer ${X_BEARER_TOKEN}` },
        signal,
      });

      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`X API fetch failed (${res.status}): ${errText}`);
      }

      const json = await res.json();
      const tweets = json.data || [];
      const mediaMap = new Map<string, any>();
      if (json.includes?.media) {
        for (const m of json.includes.media) {
          mediaMap.set(m.media_key, m);
        }
      }

      if (tweets.length === 0) break;

      // Prepare raw records for fast insertion
      const recordsToInsert = tweets.map((tweet: any) => {
        const mediaUrls: string[] = [];
        if (tweet.attachments?.media_keys) {
          for (const k of tweet.attachments.media_keys) {
            const m = mediaMap.get(k);
            if (m?.url) mediaUrls.push(m.url);
            else if (m?.preview_image_url) mediaUrls.push(m.preview_image_url);
          }
        }

        return {
          agent_id: "cco",
          artifact_type: "x_post",
          title: `X Post by ${cleanName} (${tweet.id})`,
          content: tweet.text,
          status: "pending_metadata", // Stage 1 output status
          created_at: tweet.created_at || new Date().toISOString(),
          metadata: {
            id: tweet.id,
            author: cleanName,
            screen_name: screenName,
            published_at: tweet.created_at,
            media_urls: mediaUrls,
            public_metrics: tweet.public_metrics || {},
            stage: "ingested",
          },
        };
      });

      // Upsert into agent_workspace by checking existing external id
      for (const rec of recordsToInsert) {
        const { data: existing } = await supabase
          .from("agent_workspace")
          .select("id")
          .eq("artifact_type", "x_post")
          .eq("metadata->>id", rec.metadata.id)
          .single();

        if (!existing) {
          const { error: insErr } = await supabase.from("agent_workspace").insert(rec);
          if (!insErr) {
            totalSaved++;
            xIngestionStats.totalPostsIngested++;
          }
        }
      }

      fetchedForBatch += tweets.length;
      paginationToken = json.meta?.next_token;
      if (!paginationToken || (params.limit && fetchedForBatch >= params.limit)) break;
    }
  }

  // Phase 1: Forward Sync (Newest tweets since last recorded tweet)
  await fetchAndSaveBatch({ sinceId, limit: targetLimit });

  // Phase 2: Optional Backward Sync (if not in forward-only mode)
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
