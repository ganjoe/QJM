import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { supabase, log, getEmbedding, getEmbeddingsBatch } from "./shared.ts";
import { resolveYtChannel, syncSingleChannel, runCommandWithTimeout } from "../workers/yt_ingestion_worker.ts";

const YT_COOKIES_PATH = "/app/cookies.txt";

function qualityLabel(source?: string | null, precision?: string | null): string {
  if (!source) return "unbekannt";
  if (source === "exact") return "exakt";
  return precision ? `approximativ/${precision}` : "approximativ";
}

function fmtDate(d?: string | null): string {
  return d ? String(d).substring(0, 10) : "Unbekannt";
}

async function resolveChannelHandle(channel: string): Promise<string> {
  let targetHandle = channel.startsWith("@") ? channel.toLowerCase() : `@${channel.toLowerCase()}`;

  if (!channel.startsWith("@")) {
    try {
      const queryEmbedding = (await getEmbeddingsBatch([channel], "yt"))[0];
      const { data: searchResults, error } = await supabase.rpc("search_yt_channels", {
        query_embedding: queryEmbedding,
        query_text: channel,
        match_threshold: 0.5,
        match_count: 5,
      });
      if (!error && searchResults && searchResults.length > 0) {
        const top = searchResults[0] as any;
        if (top && top.handle) targetHandle = top.handle;
      }
    } catch (_e) {
      // fallback
    }
  }
  return targetHandle;
}

async function fetchExactUploadDate(videoId: string): Promise<string | null> {
  try {
    const out = await runCommandWithTimeout([
      "--cookies", YT_COOKIES_PATH,
      "--skip-download", "--no-warnings",
      "--print", "%(id)s|%(upload_date)s",
      `https://www.youtube.com/watch?v=${videoId}`,
    ], 60000);
    if (!out.success) return null;
    const line = out.stdout.trim().split("\n").filter(Boolean).pop() || "";
    const [id, date] = line.split("|");
    if (id === videoId && /^\d{8}$/.test(date || "")) return date;
    return null;
  } catch (_e) {
    return null;
  }
}

export function registerYouTubeTools(server: McpServer) {
  // 1. Manage YouTube Channels
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
          for (const v of (videoCounts || [])) countMap.set(v.channel, (countMap.get(v.channel) || 0) + 1);
          const formatted = data.map((c: any, idx: number) =>
            `${idx + 1}. ${c.handle} (${c.title || "N/A"}) — ${countMap.get(c.handle) || 0} Videos mit Transkript — ${c.notes || ""}`
          ).join("\n");
          return { content: [{ type: "text", text: `Hier sind alle überwachten YouTube-Channels:\n\n${formatted}` }] };
        } else if (action === "ADD") {
          if (!channel) throw new Error("channel ist für ADD erforderlich");
          const resolved = await resolveYtChannel(channel);
          const embedText = `handle: ${resolved.handle} title: ${resolved.title} notes: ${notes || ""}`;
          const embedding = (await getEmbeddingsBatch([embedText], "yt"))[0];
          const { error } = await supabase.from("yt_channels").upsert({
            handle: resolved.handle,
            channel_id: resolved.channelId,
            title: resolved.title,
            notes: notes || null,
            embedding,
            is_active: true,
          }, { onConflict: "handle" });
          if (error) throw error;
          syncSingleChannel(resolved.handle).catch(e => log.error(`Channel sync error: ${e.message}`));
          return { content: [{ type: "text", text: `✅ YouTube-Channel ${resolved.handle} ("${resolved.title}") erfolgreich hinzugefügt. Initialer Video-Abruf läuft im Hintergrund.` }] };
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

  // 2. Show YouTube Content (nutzt jetzt upload_date + Qualitaet)
  server.registerTool(
    "show_yt_content",
    {
      title: "Show YouTube Content",
      description: "List videos for a channel from the local database, sorted by exact upload date.",
      inputSchema: {
        channel: z.string().describe("YouTube Handle (@handle) or channel name"),
        limit: z.number().optional().default(10).describe("Max videos to return"),
        date_from: z.string().optional().describe("Optional start date YYYY-MM-DD (UTC, inclusive)"),
        date_to: z.string().optional().describe("Optional end date YYYY-MM-DD (UTC, inclusive)"),
      },
    },
    async ({ channel, limit, date_from, date_to }: any) => {
      try {
        const targetHandle = await resolveChannelHandle(channel);
        let q = supabase
          .from("yt_videos")
          .select("video_id, title, duration, upload_date, upload_date_source, upload_date_precision, status, error_msg")
          .eq("channel", targetHandle)
          .order("upload_date", { ascending: false, nullsFirst: false })
          .limit(limit || 10);
        if (date_from) q = q.gte("upload_date", date_from);
        if (date_to) q = q.lte("upload_date", date_to);
        const { data: videos, error } = await q;
        if (error) throw error;
        if (!videos || videos.length === 0) {
          return { content: [{ type: "text", text: `Keine Videos in der Datenbank für ${targetHandle} gefunden.` }] };
        }
        const formatted = videos.map((v: any, idx: number) => {
          const durationMin = Math.floor((v.duration || 0) / 60);
          const durationSec = (v.duration || 0) % 60;
          const durationStr = `${durationMin}:${String(durationSec).padStart(2, "0")}`;
          return `${idx + 1}. 📅 ${fmtDate(v.upload_date)} [${qualityLabel(v.upload_date_source, v.upload_date_precision)}] - **${v.title}** (${durationStr}) - [${v.status}] (ID: ${v.video_id})`;
        }).join("\n");
        const unknown = videos.filter((v: any) => !v.upload_date).length;
        const approx = videos.filter((v: any) => v.upload_date_source === "approximate").length;
        let warning = "";
        if (unknown > 0) warning += `\n\n⚠️ ${unknown} Video(s) ohne Upload-Datum.`;
        if (approx > 0) warning += `\n\n⚠️ ${approx} Video(s) mit approximativem Datum (Quelle: YouTube-Relativangabe, kann bei älteren Videos um Monate/Jahre abweichen).`;
        return { content: [{ type: "text", text: `Übersicht der Videos für ${targetHandle}:\n\n${formatted}${warning}` }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // 3. Show YouTube Transcript (mit Metadaten-Kopf)
  server.registerTool(
    "show_yt_transcript",
    {
      title: "Show YouTube Transcript",
      description: "Read the transcript for a video from the database, including channel, upload date and video id.",
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
          const { data: exactId } = await supabase
            .from("yt_videos")
            .select("video_id, title, channel, upload_date, upload_date_source, transcript")
            .eq("video_id", name).limit(1);
          let video = exactId?.[0];
          if (!video) {
            const { data: fuzzyTitle } = await supabase
              .from("yt_videos")
              .select("video_id, title, channel, upload_date, upload_date_source, transcript")
              .ilike("title", `%${name}%`)
              .order("upload_date", { ascending: false, nullsFirst: false }).limit(1);
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
          const meta = `Kanal: ${video.channel || "?"} | Upload: ${fmtDate(video.upload_date)} [${qualityLabel(video.upload_date_source)}] | ID: ${video.video_id}`;
          results.push(`=== TRANSKRIPT: "${video.title}" ===\n${meta}\n\n${text}`);
        }
        return { content: [{ type: "text", text: results.join("\n\n=======================\n\n") }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // 4. Search YouTube Content -> echte Chunks via search_yt_chunks
  server.registerTool(
    "search_youtube_content",
    {
      title: "Search YouTube Content",
      description: "Hybrid search (vector + keyword) across YouTube transcript chunks with optional channel and date filters. Returns traceable results (channel, video, exact upload date, timecode, url, score).",
      inputSchema: {
        query: z.string().describe("Search query / topic"),
        channel: z.string().optional().describe("Optional filter by channel handle"),
        channels: z.array(z.string()).optional().describe("Optional filter by multiple channel handles"),
        date_from: z.string().optional().describe("Optional start date YYYY-MM-DD (UTC, inclusive)"),
        date_to: z.string().optional().describe("Optional end date YYYY-MM-DD (UTC, exclusive)"),
        tickers: z.array(z.string()).optional().describe("Optional ticker filter (overlap)"),
        min_similarity: z.number().optional().default(0.0).describe("Minimum cosine similarity (0..1)"),
        limit: z.number().optional().default(10).describe("Max results"),
      },
    },
    async ({ query, channel, channels, date_from, date_to, tickers, min_similarity, limit }: any) => {
      try {
        const qEmb = await getEmbedding(query, "x_search");
        let handles: string[] | null = null;
        if (channel) handles = [await resolveChannelHandle(channel)];
        if (channels && channels.length > 0) {
          const resolved = await Promise.all(channels.map((c: string) => resolveChannelHandle(c)));
          handles = handles ? Array.from(new Set([...handles, ...resolved])) : resolved;
        }
        const applied = {
          query,
          channels: handles,
          date_from: date_from || null,
          date_to: date_to || null,
          tickers: tickers && tickers.length ? tickers : null,
          min_similarity: min_similarity ?? 0,
        };
        const { data, error } = await supabase.rpc("search_yt_chunks", {
          p_query: query,
          p_embedding: qEmb,
          p_channels: handles,
          p_date_from: date_from || null,
          p_date_to: date_to || null,
          p_tickers: tickers && tickers.length ? tickers : null,
          p_language: null,
          p_min_similarity: min_similarity ?? 0,
          p_limit: limit || 10,
        });
        if (error) {
          const { data: videos } = await supabase
            .from("yt_videos")
            .select("video_id, channel, title, upload_date, transcript")
            .ilike("transcript", `%${query}%`)
            .limit(limit || 10);
          if (!videos || videos.length === 0) return { content: [{ type: "text", text: `Keine Treffer. (RPC-Fehler: ${error.message})` }], isError: true };
          const fb = videos.map((v: any, idx: number) => `${idx + 1}. ${fmtDate(v.upload_date)} | ${v.channel} | **${v.title}** | ID: ${v.video_id}`).join("\n");
          return { content: [{ type: "text", text: `Fallback-Treffer:\n\n${fb}` }] };
        }
        if (!data || data.length === 0) {
          return { content: [{ type: "text", text: `Keine Treffer.\nAngewandte Filter: ${JSON.stringify(applied)}` }] };
        }
        const formatted = data.map((d: any, idx: number) => {
          const tc = d.t_start_sec != null ? ` @ ${Math.floor(d.t_start_sec / 60)}:${String(d.t_start_sec % 60).padStart(2, "0")}` : "";
          return `[${idx + 1}] ${fmtDate(d.upload_date)} [${qualityLabel(d.upload_date_source, d.upload_date_precision)}] | ${d.channel} | "${d.title}"${tc}` +
            `\n    URL: ${d.url}` +
            `\n    Score: ${Number(d.score).toFixed(5)} (sim ${Number(d.similarity).toFixed(3)}, kw ${Number(d.keyword_rank).toFixed(3)})` +
            `\n    Treffer: ${(d.matched_terms || []).join(", ") || "-"}` +
            `\n    ${String(d.content || "").substring(0, 400).replace(/\n/g, " ")}...`;
        }).join("\n\n");
        return { content: [{ type: "text", text: `${data.length} Treffer.\nAngewandte Filter: ${JSON.stringify(applied)}\n\n${formatted}` }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );

  // 5. Manage YouTube Metadata (Status + exakter Backfill)
  server.registerTool(
    "manage_yt_metadata",
    {
      title: "Manage YouTube Metadata",
      description: "Data-quality status of YouTube upload dates and an exact metadata backfill (downloads no transcripts).",
      inputSchema: {
        action: z.enum(["STATUS", "BACKFILL_DATES"]).describe("STATUS = report coverage, BACKFILL_DATES = fetch exact upload dates for pending videos"),
        limit: z.number().optional().default(25).describe("Max videos to backfill per call (BACKFILL_DATES, max 200)"),
      },
    },
    async ({ action, limit }: any) => {
      try {
        if (action === "STATUS") {
          const { data, error } = await supabase
            .from("yt_videos")
            .select("channel, upload_date_source")
            .limit(20000);
          if (error) throw error;
          const agg = new Map<string, { exact: number; approx: number; missing: number }>();
          for (const v of (data || [])) {
            const e = agg.get(v.channel) || { exact: 0, approx: 0, missing: 0 };
            if (v.upload_date_source === "exact") e.exact++;
            else if (v.upload_date_source === "approximate") e.approx++;
            else e.missing++;
            agg.set(v.channel, e);
          }
          const lines = [...agg.entries()].sort().map(([ch, e]) =>
            `  ${ch}: exakt ${e.exact} | approximativ ${e.approx} | fehlend ${e.missing}`
          );
          const totals = [...agg.values()].reduce((a, e) => ({ exact: a.exact + e.exact, approx: a.approx + e.approx, missing: a.missing + e.missing }), { exact: 0, approx: 0, missing: 0 });
          return { content: [{ type: "text", text: `YouTube-Upload-Datum Status:\n  Gesamt: exakt ${totals.exact} | approximativ ${totals.approx} | fehlend ${totals.missing}\n\n${lines.join("\n")}` }] };
        }
        const max = Math.min(Math.max(limit || 25, 1), 200);
        const { data: pending, error } = await supabase
          .from("yt_videos")
          .select("video_id, channel")
          .or("upload_date_source.is.null,upload_date_source.neq.exact")
          .order("upload_date", { ascending: true, nullsFirst: true })
          .limit(max);
        if (error) throw error;
        if (!pending || pending.length === 0) {
          return { content: [{ type: "text", text: "Alle Videos haben bereits ein exaktes Upload-Datum." }] };
        }
        let ok = 0, failed = 0;
        for (const v of pending) {
          const date = await fetchExactUploadDate(v.video_id);
          if (!date) { failed++; continue; }
          const iso = `${date.substring(0, 4)}-${date.substring(4, 6)}-${date.substring(6, 8)}`;
          const { error: upErr } = await supabase.from("yt_videos").update({
            upload_date: iso,
            upload_date_source: "exact",
            upload_date_precision: "day",
            metadata_synced_at: new Date().toISOString(),
          }).eq("video_id", v.video_id);
          if (upErr) failed++; else ok++;
        }
        return { content: [{ type: "text", text: `Backfill: ${ok} exakt gesetzt, ${failed} fehlgeschlagen/nicht verfügbar (von ${pending.length}).` }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );
}
