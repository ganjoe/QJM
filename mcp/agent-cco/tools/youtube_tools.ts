import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { supabase, log, getEmbedding, getEmbeddingsBatch } from "./shared.ts";
import { resolveYtChannel, syncSingleChannel } from "../workers/yt_ingestion_worker.ts";

async function resolveChannelHandle(channel: string): Promise<string> {
  let targetHandle = channel.startsWith("@") ? channel.toLowerCase() : `@${channel.toLowerCase()}`;

  if (!channel.startsWith("@")) {
    try {
      const queryEmbedding = (await getEmbeddingsBatch([channel]))[0];
      const { data: searchResults, error: searchError } = await supabase.rpc("search_yt_channels", {
        query_embedding: queryEmbedding,
        query_text: channel,
        match_threshold: 0.5,
        match_count: 5,
      });

      if (!searchError && searchResults && searchResults.length > 0) {
        const top = searchResults[0] as any;
        if (top && top.handle) {
          targetHandle = top.handle;
        }
      }
    } catch (_e) {
      // fallback
    }
  }

  return targetHandle;
}

export function registerYouTubeTools(server: McpServer) {
  // 1. Tool: Manage YouTube Channels
  server.registerTool(
    "manage_youtube_channels",
    {
      title: "Manage YouTube Channels",
      description: "List, add, or remove monitored YouTube channels. Channels are automatically synced in the background without needing manual video IDs.",
      inputSchema: {
        action: z.enum(["LIST", "ADD", "REMOVE"]).describe("The action to perform"),
        channel: z.string().optional().describe("YouTube Handle (@MarkMinervini) or Channel-URL (for ADD or REMOVE)"),
        notes: z.string().optional().describe("Optional notes about this channel (for ADD)"),
      },
    },
    async ({ action, channel, notes }: any) => {
      try {
        if (action === "LIST") {
          const { data, error } = await supabase
            .from("yt_channels")
            .select("handle, title, notes")
            .eq("is_active", true)
            .order("handle");

          if (error) throw error;
          if (!data || data.length === 0) {
            return { content: [{ type: "text", text: "Keine aktiven YouTube-Channels in der Datenbank gefunden." }] };
          }

          const { data: videoCounts } = await supabase
            .from("yt_videos")
            .select("channel, video_id, status")
            .in("channel", data.map((c: any) => c.handle))
            .or("status.eq.downloaded,status.eq.embedded");

          const countMap = new Map<string, number>();
          for (const v of (videoCounts || [])) {
            countMap.set(v.channel, (countMap.get(v.channel) || 0) + 1);
          }

          const formatted = data.map((c: any, idx: number) =>
            `${idx + 1}. ${c.handle} (${c.title || "N/A"}) — ${countMap.get(c.handle) || 0} Videos mit Transkript — ${c.notes || ""}`
          ).join("\n");

          return { content: [{ type: "text", text: `Hier sind alle überwachten YouTube-Channels:\n\n${formatted}` }] };
        } else if (action === "ADD") {
          if (!channel) throw new Error("channel ist für ADD erforderlich");

          const resolved = await resolveYtChannel(channel);
          const embedText = `handle: ${resolved.handle} title: ${resolved.title} notes: ${notes || ""}`;
          const embedding = (await getEmbeddingsBatch([embedText]))[0];

          const { error } = await supabase.from("yt_channels").upsert({
            handle: resolved.handle,
            channel_id: resolved.channelId,
            title: resolved.title,
            notes: notes || null,
            embedding,
            is_active: true,
          }, { onConflict: "handle" });

          if (error) throw error;

          // Trigger initial sync in background
          syncSingleChannel(resolved.handle).catch(e => log.error(`Channel sync error: ${e.message}`));

          return {
            content: [{
              type: "text",
              text: `✅ YouTube-Channel ${resolved.handle} ("${resolved.title}") erfolgreich hinzugefügt. Initialer Video-Abruf läuft im Hintergrund.`
            }]
          };
        } else if (action === "REMOVE") {
          if (!channel) throw new Error("channel ist für REMOVE erforderlich");
          const targetHandle = await resolveChannelHandle(channel);
          const { error } = await supabase.from("yt_channels").update({ is_active: false }).eq("handle", targetHandle);
          if (error) throw error;

          return { content: [{ type: "text", text: `YouTube-Channel ${targetHandle} wurde deaktiviert.` }] };
        }

        throw new Error("Invalid action");
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // 2. Tool: Show YouTube Content
  server.registerTool(
    "show_yt_content",
    {
      title: "Show YouTube Content",
      description: "List videos for a channel from the local database.",
      inputSchema: {
        channel: z.string().describe("YouTube Handle (@handle) or channel name"),
        limit: z.number().optional().default(10).describe("Max videos to return"),
      },
    },
    async ({ channel, limit }: any) => {
      try {
        const targetHandle = await resolveChannelHandle(channel);
        const { data: videos, error } = await supabase
          .from("yt_videos")
          .select("video_id, title, duration, published_at, status, error_msg")
          .eq("channel", targetHandle)
          .order("published_at", { ascending: false })
          .limit(limit || 10);

        if (error) throw error;
        if (!videos || videos.length === 0) {
          return { content: [{ type: "text", text: `Keine Videos in der Datenbank für ${targetHandle} gefunden.` }] };
        }

        const formatted = videos.map((v: any, idx: number) => {
          const durationMin = Math.floor(v.duration / 60);
          const durationSec = v.duration % 60;
          const durationStr = `${durationMin}:${String(durationSec).padStart(2, "0")}`;
          const dateStr = v.published_at ? new Date(v.published_at).toLocaleDateString("de-DE") : "Unbekannt";
          return `${idx + 1}. 📅 ${dateStr} - **${v.title}** (${durationStr}) - [${v.status}] (ID: ${v.video_id})`;
        }).join("\n");

        return { content: [{ type: "text", text: `Übersicht der Videos für ${targetHandle}:\n\n${formatted}` }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // 3. Tool: Show YouTube Transcript
  server.registerTool(
    "show_yt_transcript",
    {
      title: "Show YouTube Transcript",
      description: "Read the transcript for a video from the database.",
      inputSchema: {
        video_names: z.array(z.string()).describe("Array of video titles or YouTube Video IDs"),
        include_timestamps: z.boolean().optional().default(false).describe("Include [MM:SS] timestamps"),
      },
    },
    async ({ video_names, include_timestamps }: any) => {
      try {
        if (!video_names || video_names.length === 0) throw new Error("Mindestens ein Videoname/ID ist erforderlich.");

        const results: string[] = [];
        for (const name of video_names) {
          const { data: exactId } = await supabase.from("yt_videos").select("video_id, title, transcript").eq("video_id", name).limit(1);
          let video = exactId?.[0];

          if (!video) {
            const { data: fuzzyTitle } = await supabase.from("yt_videos").select("video_id, title, transcript").ilike("title", `%${name}%`).order("published_at", { ascending: false }).limit(1);
            video = fuzzyTitle?.[0];
          }

          if (!video || !video.transcript) {
            results.push(`=== "${name}" nicht gefunden oder kein Transkript vorhanden ===`);
            continue;
          }

          let text = video.transcript;
          if (!include_timestamps) {
            text = text.split("\n").filter((l: string) => !l.trim().match(/^\[\d{2,}(:\d{2}){1,2}\]$/)).join("\n");
          }

          results.push(`=== TRANSKRIPT: "${video.title}" (${video.video_id}) ===\n\n${text}`);
        }

        return { content: [{ type: "text", text: results.join("\n\n=======================\n\n") }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // 4. Tool: Search YouTube Content
  server.registerTool(
    "search_youtube_content",
    {
      title: "Search YouTube Content",
      description: "Semantic search across YouTube video transcripts in the database.",
      inputSchema: {
        query: z.string().describe("Search query / topic"),
        channel: z.string().optional().describe("Optional filter by channel handle"),
        limit: z.number().optional().default(10).describe("Max results"),
      },
    },
    async ({ query, channel, limit }: any) => {
      try {
        const qEmb = await getEmbedding(query);
        let targetHandle: string | null = null;
        if (channel) targetHandle = await resolveChannelHandle(channel);

        const { data, error } = await supabase.rpc("hybrid_search_open_brain", {
          query_embedding: qEmb,
          query_text: query,
          match_threshold: 0.4,
          match_count: limit || 10,
          p_agent_id: "cco",
        });

        if (error) {
          // Fallback direct query
          const { data: videos } = await supabase
            .from("yt_videos")
            .select("video_id, channel, title, published_at, transcript")
            .ilike("transcript", `%${query}%`)
            .limit(limit || 10);

          if (!videos || videos.length === 0) return { content: [{ type: "text", text: "Keine Treffer gefunden." }] };
          const formatted = videos.map((v: any, idx: number) => `${idx + 1}. **${v.title}** (${v.channel})\nID: ${v.video_id}`).join("\n\n");
          return { content: [{ type: "text", text: `Gefundene Videos:\n\n${formatted}` }] };
        }

        if (!data || data.length === 0) return { content: [{ type: "text", text: "Keine Treffer gefunden." }] };

        const formatted = data.map((d: any, idx: number) => `[${idx + 1}] ID: ${d.id} | Date: ${new Date(d.created_at).toLocaleDateString()}\nContent:\n${d.content.substring(0, 300)}...`).join("\n\n");
        return { content: [{ type: "text", text: formatted }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );
}
