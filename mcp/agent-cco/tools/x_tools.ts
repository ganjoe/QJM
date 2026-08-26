import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import {
  supabase,
  log,
  getEmbedding,
  getEmbeddingsBatch,
  X_BEARER_TOKEN,
  AGENT_ID,
  GLOBAL_BRAIN_ACCESS,
  resolveAuthorHandles,
  getXOAuthTokens,
  getValidXUserAccessToken,
  throttledXFetch,
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
      description: "Search the database for influencer posts using hybrid semantic or exact keyword search.",
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
      title: "Show X Content",
      description: "View X/Twitter content. DATABASE lists stored posts chronologically. ONLINE fetches a single tweet live from the X API.",
      inputSchema: {
        action: z.enum(["DATABASE", "ONLINE"]).describe("DATABASE = list stored posts. ONLINE = fetch a single tweet live."),
        username: z.string().optional().describe("Filter by influencer handle (for DATABASE, e.g. '@elonmusk')"),
        limit: z.number().optional().default(10).describe("Max posts to show (for DATABASE, default: 10)"),
        days_back: z.number().optional().describe("Filter posts from the last X days (for DATABASE)"),
        tweet_id: z.string().optional().describe("Tweet ID or URL to fetch (for ONLINE)"),
      },
    },
    async ({ action, username, limit, days_back, tweet_id }: any) => {
      try {
        if (action === "DATABASE") {
          let query = supabase
            .from("agent_workspace")
            .select("id, content, metadata, created_at")
            .eq("artifact_type", "x_post")
            .order("created_at", { ascending: false })
            .limit(limit || 10);

          if (username) {
            const { allHandles } = await resolveAuthorHandles(username);
            query = query.in("metadata->>author", allHandles);
          }

          if (days_back) {
            const cutoff = new Date(Date.now() - days_back * 24 * 60 * 60 * 1000).toISOString();
            query = query.gte("created_at", cutoff);
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
          if (!X_BEARER_TOKEN) throw new Error("X_BEARER_TOKEN ist nicht konfiguriert");

          let id = tweet_id;
          const urlMatch = tweet_id.match(/status\/(\d+)/);
          if (urlMatch) id = urlMatch[1];

          const res = await throttledXFetch(`https://api.twitter.com/2/tweets/${id}?tweet.fields=created_at,author_id,entities`, {
            headers: { Authorization: `Bearer ${X_BEARER_TOKEN}` }
          });
          if (!res.ok) throw new Error(`X API failed: ${res.status}`);
          const data = await res.json();
          if (!data.data) throw new Error("Tweet not found");

          const tweet = data.data;
          const dateStr = tweet.created_at ? new Date(tweet.created_at).toLocaleString('de-DE') : 'Unbekanntes Datum';
          return { content: [{ type: "text", text: `📅 ${dateStr} | Author ID: ${tweet.author_id}\n\n${tweet.text}` }] };
        }

        throw new Error("Invalid action");
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // 3. Tool: Manage Influencers
  server.registerTool(
    "manage_influencers",
    {
      title: "Manage Influencers",
      description: "List, add, or remove influencers from the database.",
      inputSchema: {
        action: z.enum(["LIST", "ADD", "REMOVE"]).describe("The action to perform"),
        username: z.string().optional().describe("The X username (for ADD or REMOVE)"),
        notes: z.string().optional().describe("Optional notes about this influencer (for ADD)"),
      },
    },
    async ({ action, username, notes }: any) => {
      try {
        if (action === "LIST") {
          const { data, error } = await supabase.from("x_users").select("username, screen_name, notes").eq("is_active", true).order("username");
          if (error) throw error;
          if (!data || data.length === 0) return { content: [{ type: "text", text: `Keine aktiven Influencer in der Datenbank gefunden.` }] };
          const formatted = data.map((i: any, idx: number) => `${idx + 1}. @${i.username} (${i.screen_name || 'N/A'}) - ${i.notes || ''}`).join("\n");
          return { content: [{ type: "text", text: `Hier sind alle überwachten Influencer:\n\n${formatted}` }] };
        } else if (action === "ADD") {
          if (!username) throw new Error("username ist für ADD erforderlich");
          const cleanName = username.startsWith("@") ? username.substring(1).toLowerCase() : username.toLowerCase();

          const res = await throttledXFetch(`https://api.twitter.com/2/users/by/username/${cleanName}`, {
            headers: { Authorization: `Bearer ${X_BEARER_TOKEN}` }
          });
          if (!res.ok) throw new Error(`X API failed to resolve user: ${res.status}`);
          const data = await res.json();
          if (!data.data?.id) throw new Error(`User @${username} nicht auf X gefunden.`);

          const userId = data.data.id;
          const screenName = data.data.name;
          const embedText = `username: ${cleanName} screen_name: ${screenName} notes: ${notes || ''}`;
          const embedding = (await getEmbeddingsBatch([embedText]))[0];

          const { error } = await supabase.from("x_users").upsert({
            username: cleanName,
            x_id: userId,
            screen_name: screenName,
            notes: notes || null,
            embedding: embedding,
            is_active: true
          }, { onConflict: "username" });
          if (error) throw error;

          // Trigger initial ingestion in background
          const autoCleanName = `@${cleanName}`;
          if (!activeSyncControllers.has(autoCleanName)) {
            const controller = new AbortController();
            activeSyncControllers.set(autoCleanName, controller);
            ingestInfluencerTweets(autoCleanName, cleanName, 200, undefined, controller.signal, false)
              .catch(e => log.error(`Initial sync error for ${autoCleanName}: ${e.message}`))
              .finally(() => activeSyncControllers.delete(autoCleanName));
          }

          return { content: [{ type: "text", text: `✅ Influencer @${cleanName} (${screenName}) wurde erfolgreich hinzugefügt und der Initial-Sync (200 Posts) wurde gestartet.` }] };
        } else if (action === "REMOVE") {
          if (!username) throw new Error("username ist für REMOVE erforderlich");
          const cleanName = username.startsWith("@") ? username.substring(1).toLowerCase() : username.toLowerCase();
          const { error } = await supabase.from("x_users").update({ is_active: false }).eq("username", cleanName);
          if (error) throw error;
          return { content: [{ type: "text", text: `Influencer @${cleanName} wurde deaktiviert (Soft-Delete). Posts bleiben erhalten.` }] };
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
        limit: z.number().optional().default(100).describe("Max posts to fetch (default: 100)"),
        start_time: z.string().optional().describe("Earliest post date (ISO format) for historical backfill"),
      },
    },
    async ({ author, limit, start_time }: any) => {
      try {
        const cleanName = author.toLowerCase().startsWith("@") ? author.toLowerCase() : `@${author.toLowerCase()}`;
        const username = cleanName.substring(1);

        if (activeSyncControllers.has(cleanName)) {
          return { content: [{ type: "text", text: `Sync für ${cleanName} läuft bereits im Hintergrund.` }] };
        }

        const controller = new AbortController();
        activeSyncControllers.set(cleanName, controller);

        // Run ingestion in background
        ingestInfluencerTweets(cleanName, username, limit, start_time, controller.signal, !start_time)
          .then(count => log.info(`[Sync] Manual sync for ${cleanName} completed: ${count} posts ingested.`))
          .catch(e => log.error(`[Sync] Manual sync for ${cleanName} failed: ${e.message}`))
          .finally(() => activeSyncControllers.delete(cleanName));

        return {
          content: [{
            type: "text",
            text: `🚀 Hintergrund-Sync für ${cleanName} gestartet (Limit: ${limit || 100} Posts). Die Posts werden sofort gespeichert und asynchron analysiert & ge-embeddet.`
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
          if (!isConnected || !tokens) {
            return {
              content: [{
                type: "text",
                text: `🔴 X OAuth 2.0 ist noch NICHT autorisiert.\n\nÖffne folgenden Link im Browser:\n👉 http://127.0.0.1:8788/auth/x/login`
              }]
            };
          }

          const expiresInMinutes = Math.round((tokens.expires_at - Date.now()) / 60000);
          const expiryText = expiresInMinutes > 0 ? `in ${expiresInMinutes} Minuten (Auto-Refresh aktiv)` : "Abgelaufen (Auto-Refresh aktiv)";

          return {
            content: [{
              type: "text",
              text: `🟢 X OAuth 2.0 ist erfolgreich VERBUNDEN!\n\n` +
                `👤 Account: @${tokens.username || 'Unbekannt'} (${tokens.name || 'N/A'})\n` +
                `🆔 User ID: ${tokens.user_id || 'N/A'}\n` +
                `🔑 Scopes: ${tokens.scope || 'Standard'}\n` +
                `⏳ Token: ${expiryText}`
            }]
          };
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
