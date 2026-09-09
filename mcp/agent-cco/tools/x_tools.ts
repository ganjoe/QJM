import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import {
  supabase,
  log,
  getEmbedding,
  getEmbeddingsBatch,
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
    return data.map((t: any, i: number) => {
      let snippet = t.content || "";
      if (snippet.length > 150) {
        snippet = snippet.substring(0, 150) + "...";
      }
      const author = t.metadata?.author || "Unknown";
      return `[${i + 1}] ID: ${t.id} | Date: ${new Date(t.created_at).toLocaleDateString()} | Author: ${author}\nSnippet: ${snippet}`;
    }).join("\n\n");
  }
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
        query: z.string().optional().describe("Search query (ticker, topic, keyword). Leave empty to list recent posts."),
        limit: z.number().optional().default(200).describe("Max results (default: 200)"),
        threshold: z.number().optional().default(0.5).describe("Similarity threshold for semantic search (default: 0.5)"),
        artifact_type: z.string().optional().describe("Filter by artifact type (default: 'x_post')"),
        days_back: z.number().optional().describe("Filter posts from the last X days"),
        return_mode: z.enum(["ids_only", "snippets", "full_text"]).optional().default("snippets").describe("Return format for READ (default: snippets)"),
        ids: z.array(z.string()).optional().describe("Array of post IDs (for READ_IDS only)"),
        authors: z.array(z.string()).optional().describe("Filter to specific influencers/authors (e.g. ['@serenity'])"),
        ...(GLOBAL_BRAIN_ACCESS ? { owner: z.string().optional().describe("Filter by agent ID.") } : {}),
      },
    },
    async ({ action, query, limit, threshold, artifact_type, days_back, return_mode, ids, authors, owner }: any) => {
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
        const isLikelyExact = actual_query === "" || /^[A-Z0-9$.#]{1,10}$/i.test(actual_query) || actual_query.startsWith("@");

        let expandedAuthors: string[] | null = null;
        const expandedHandlesSet = new Set<string>();
        if (authors && authors.length > 0) {
          for (const author of authors) {
            const { allHandles } = await resolveAuthorHandles(author);
            allHandles.forEach(h => expandedHandlesSet.add(h.toLowerCase()));
          }
          expandedAuthors = Array.from(expandedHandlesSet);
        }

        let data: any[];

        if (isLikelyExact) {
          const { data: exactData, error } = await supabase.rpc("exact_search_workspace", {
            p_exact_keyword: actual_query === "" ? null : actual_query,
            match_count: limit,
            p_agent_id: p_agent_id,
            p_artifact_type: artifact_type || null,
            p_days_back: days_back || null,
            p_authors: expandedAuthors,
          });
          if (error) throw error;
          data = exactData || [];
        } else {
          const qEmb = await getEmbedding(actual_query);
          const { data: semData, error } = await supabase.rpc("semantic_search_workspace", {
            query_embedding: qEmb,
            match_threshold: threshold,
            match_count: limit,
            p_agent_id: p_agent_id,
            p_artifact_type: artifact_type || null,
            p_days_back: days_back || null,
            p_authors: expandedAuthors,
          });
          if (error) throw error;
          data = semData || [];
        }

        if (expandedAuthors && expandedAuthors.length > 0) {
          data = data.filter((t: any) => {
            const postAuthor = (t.metadata?.author || "").toLowerCase();
            const cleanPostAuthor = postAuthor.startsWith("@") ? postAuthor : `@${postAuthor}`;
            return expandedHandlesSet.has(postAuthor) || expandedHandlesSet.has(cleanPostAuthor);
          });
        }

        if (!data || data.length === 0) return { content: [{ type: "text", text: "Keine Ergebnisse gefunden." }] };

        return { content: [{ type: "text", text: formatSearchResults(data, return_mode) }] };
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
        "WHEN NOT TO USE: To search across posts for specific keywords, topics, or stock tickers, use `search_influencer_posts`.",
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

          const json = await twitterApiIoFetch("tweets", { tweet_ids: id });
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
      description: "List, add, or remove influencers from the database. Supports single handles, batch arrays, multiline text, or local file imports.",
      inputSchema: {
        action: z.enum(["LIST", "ADD", "REMOVE"]).describe("The action to perform"),
        username: z.string().optional().describe("A single X username or comma-/newline-separated usernames (e.g. '@RayDalio, @fundstrat' or '@RayDalio')"),
        usernames: z.array(z.string()).optional().describe("An array of X usernames for batch operations (e.g. ['@RayDalio', '@fundstrat'])"),
        file_path: z.string().optional().describe("Optional path to a text file containing influencer handles (e.g. /home/daniel/QJM/dsh_playground/fintwit/01_makro_strategen.txt)"),
        raw_text: z.string().optional().describe("Raw text containing comma-, space- or newline-separated handles"),
        notes: z.string().optional().describe("Optional notes/category about the influencer(s) (for ADD)"),
      },
    },
    async ({ action, username, usernames, file_path, raw_text, notes }: any) => {
      try {
        if (action === "LIST") {
          const { data, error } = await supabase
            .from("x_users")
            .select("username, screen_name, notes")
            .eq("is_active", true)
            .order("username");
          if (error) throw error;
          if (!data || data.length === 0) {
            return { content: [{ type: "text", text: `Keine aktiven Influencer in der Datenbank gefunden.` }] };
          }
          const formatted = data.map((i: any, idx: number) => `${idx + 1}. @${i.username} (${i.screen_name || 'N/A'}) - ${i.notes || ''}`).join("\n");
          return { content: [{ type: "text", text: `Hier sind alle überwachten Influencer (${data.length}):\n\n${formatted}` }] };
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
            const json = await twitterApiIoFetch("user/info", { userName: cleanName });
            if (!json.id) {
              throw new Error("User nicht auf X gefunden (TwitterAPI.io)");
            }
            const userId = json.id;
            const screenName = json.name || cleanName;
            const { error: upsertError } = await supabase.from("x_users").upsert({
              username: cleanName,
              x_id: userId,
              screen_name: screenName,
              notes: notes || null,
              is_active: true,
            }, { onConflict: "username" });

            if (upsertError) throw upsertError;

            // Asynchronously generate profile embedding without blocking user onboarding
            const embedText = `username: ${cleanName} screen_name: ${screenName} notes: ${notes || ''}`;
            getEmbeddingsBatch([embedText])
              .then((emb: any) => {
                if (emb && emb[0]) {
                  supabase.from("x_users").update({ embedding: emb[0] }).eq("username", cleanName).then();
                }
              })
              .catch((e: any) => log.debug(`Optional profile embedding for @${cleanName}: ${e.message}`));

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
                  text: `✅ Influencer @${res.cleanName} (${res.screenName}) wurde erfolgreich hinzugefügt und der Initial-Sync (${X_INITIAL_BACKFILL_LIMIT} Posts) wurde gestartet.`
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
            `• **Initial-Backfill:** ${X_INITIAL_BACKFILL_LIMIT} Posts pro Account (Parallelität: ${X_INITIAL_SYNC_CONCURRENCY})\n\n` +
            `Die Accounts werden nun schrittweise aufgelöst, in der Datenbank gespeichert und synchronisiert.`;

          if (alreadyTracked.length > 0) {
            responseText += `\n\nBereits aktive Accounts:\n${alreadyTracked.map(u => `• ${u}`).join("\n")}`;
          }

          return { content: [{ type: "text", text: responseText }] };

        } else if (action === "REMOVE") {
          if (targetHandles.length === 0) {
            throw new Error("Mindestens ein Benutzername (username, usernames, file_path oder raw_text) ist für REMOVE erforderlich.");
          }

          const { data, error } = await supabase
            .from("x_users")
            .update({ is_active: false })
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
      description: "Find tickers that an influencer mentioned for the VERY FIRST TIME ever.",
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

  // 5. Tool: Sync Influencer Posts (Manual On-Demand)
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
        ingestInfluencerTweets(cleanName, username, syncLimit, start_time, controller.signal, false)
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
