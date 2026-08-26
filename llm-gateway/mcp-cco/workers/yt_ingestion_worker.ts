import { supabase, log, getEmbeddingsBatch, MAX_CONCURRENT_YT_CHANNELS } from "../tools/shared.ts";

export const ytIngestionStats = {
  isRunning: false,
  startTime: 0,
  lastRunTime: 0,
  totalVideosDiscovered: 0,
  totalProcessed: 0,
  lastError: null as string | null,
};

let ytAbortController: AbortController | null = null;
const YT_COOKIES_PATH = "/app/cookies.txt";

export async function runCommandWithTimeout(
  args: string[],
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<{ success: boolean; stdout: string; stderr: string }> {
  let timerId: number | undefined;
  let aborted = false;

  const child = new Deno.Command("yt-dlp", {
    args,
    stdout: "piped",
    stderr: "piped",
  }).spawn();

  const cleanup = () => {
    if (timerId !== undefined) clearTimeout(timerId);
  };

  if (signal) {
    signal.addEventListener("abort", () => {
      aborted = true;
      cleanup();
      try { child.kill("SIGTERM"); } catch {}
    }, { once: true });
  }

  timerId = setTimeout(() => {
    aborted = true;
    cleanup();
    try { child.kill("SIGKILL"); } catch {}
  }, timeoutMs);

  try {
    const output = await child.output();
    cleanup();

    if (aborted && signal?.aborted) throw new Error("Command aborted");
    if (aborted) throw new Error(`Command timed out after ${timeoutMs / 1000}s`);

    return {
      success: output.success,
      stdout: new TextDecoder().decode(output.stdout),
      stderr: new TextDecoder().decode(output.stderr),
    };
  } catch (err: any) {
    cleanup();
    throw err;
  }
}

export async function resolveYtChannel(input: string): Promise<{ channelId: string; handle: string; title: string }> {
  let target = input;
  if (!target.startsWith("http") && !target.startsWith("@")) target = `@${target}`;
  if (target.startsWith("@")) target = `https://www.youtube.com/${target}/videos`;
  if (target.includes("youtube.com/") && !target.includes("/videos")) target = target.replace(/\/?$/, "/videos");

  const args = ["--cookies", YT_COOKIES_PATH, "--dump-json", "--playlist-items", "1", "--skip-download", target];
  const output = await runCommandWithTimeout(args, 300000);
  if (!output.success) throw new Error(`yt-dlp resolve failed: ${output.stderr.substring(0, 200)}`);

  const data = JSON.parse(output.stdout.trim().split("\n")[0]);
  const channelId = data.channel_id || data.uploader_id || "";
  const handle = data.channel_url?.match(/@[\w.-]+/)?.[0]?.toLowerCase()
    || (data.uploader_id?.startsWith("@") ? data.uploader_id.toLowerCase() : "")
    || `@${(data.channel || data.uploader || input).toLowerCase().replace(/^@/, "")}`;
  const title = data.channel || data.uploader || "";

  if (!channelId) throw new Error(`Could not resolve channel ID for: ${input}`);
  return { channelId, handle, title };
}

function determineTargetLang(meta: any): string {
  const rawLang = meta.language || "";
  if (rawLang.startsWith("de")) return "de";
  if (rawLang.startsWith("en")) return "en";
  const autoKeys = Object.keys(meta.automatic_captions || {});
  if (autoKeys.some(k => k.startsWith("de-orig")) && !autoKeys.some(k => k.startsWith("en-orig"))) return "de";
  return "en";
}

async function getChannelVideos(channelUrl: string, limit?: number, signal?: AbortSignal) {
  const target = channelUrl.includes("/videos") ? channelUrl : channelUrl.replace(/\/?$/, "/videos");
  const useFlat = limit === undefined || limit > 30;

  const args = ["--cookies", YT_COOKIES_PATH, "--dump-json", "--skip-download"];
  if (useFlat) args.push("--flat-playlist");
  if (limit !== undefined) args.push("--playlist-end", String(limit));
  args.push(target);

  const output = await runCommandWithTimeout(args, 300000, signal);
  if (!output.success) throw new Error(`yt-dlp video list failed: ${output.stderr.substring(0, 200)}`);

  const lines = output.stdout.trim().split("\n");
  const videos: Array<{ videoId: string; title: string; duration: number; publishedAt: string; targetLang: string }> = [];

  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const data = JSON.parse(line);
      videos.push({
        videoId: data.id,
        title: data.title || "Unknown",
        duration: data.duration || 0,
        publishedAt: data.upload_date
          ? `${data.upload_date.substring(0, 4)}-${data.upload_date.substring(4, 6)}-${data.upload_date.substring(6, 8)}T00:00:00Z`
          : "",
        targetLang: useFlat ? "en" : determineTargetLang(data),
      });
    } catch {
      // skip
    }
  }
  return videos;
}

async function downloadVtt(videoId: string, targetLang: string, signal?: AbortSignal): Promise<string | null> {
  const outputDir = "/data/yt/vtt";
  try { await Deno.mkdir(outputDir, { recursive: true }); } catch {}

  const outputTemplate = `${outputDir}/${videoId}`;
  const args = [
    "--cookies", YT_COOKIES_PATH,
    "--write-auto-sub",
    "--sub-lang", targetLang,
    "--skip-download",
    "--sub-format", "vtt",
    "--output", outputTemplate,
    `https://www.youtube.com/watch?v=${videoId}`,
  ];

  const output = await runCommandWithTimeout(args, 600000, signal);
  if (!output.success) {
    if (output.stderr.includes("no subtitles") || output.stderr.includes("Subtitles are disabled")) return null;
    throw new Error(`yt-dlp VTT download failed: ${output.stderr.substring(0, 200)}`);
  }

  for await (const entry of Deno.readDir(outputDir)) {
    if (entry.name.startsWith(videoId) && entry.name.endsWith(".vtt")) {
      const content = await Deno.readTextFile(`${outputDir}/${entry.name}`);
      try { await Deno.remove(`${outputDir}/${entry.name}`); } catch {}
      return content;
    }
  }
  return null;
}

function vttToPlaintext(vttContent: string): string {
  const lines = vttContent.split("\n");
  const outputLines: string[] = [];
  let lastText = "";

  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed === "WEBVTT" || trimmed === "" || trimmed.startsWith("NOTE") || trimmed.startsWith("Kind:") || trimmed.startsWith("Language:")) continue;
    if (trimmed.includes("-->")) {
      const match = trimmed.match(/(\d{2}):(\d{2}):(\d{2})\.\d+/);
      if (match) {
        const totalMin = parseInt(match[1]) * 60 + parseInt(match[2]);
        outputLines.push(`[${String(totalMin).padStart(2, "0")}:${String(parseInt(match[3])).padStart(2, "0")}]`);
      }
      continue;
    }
    const cleanText = trimmed.replace(/<[^>]+>/g, "").replace(/\{[^}]+\}/g, "").trim();
    if (cleanText && cleanText !== lastText) {
      outputLines.push(cleanText);
      lastText = cleanText;
    }
  }
  return outputLines.join("\n");
}

export async function syncSingleChannel(handle: string, signal?: AbortSignal) {
  try {
    const channelUrl = `https://www.youtube.com/${handle}`;
    const videosToProcess = await getChannelVideos(channelUrl, undefined, signal);
    if (videosToProcess.length === 0) return;

    const { data: dbVideos } = await supabase.from("yt_videos").select("video_id").eq("channel", handle);
    const existingIds = new Set((dbVideos || []).map((v: any) => v.video_id));
    const newVideos = videosToProcess.filter((v: any) => !existingIds.has(v.videoId));

    if (newVideos.length === 0) return;
    log.info(`[YT Sync] ${handle}: ${newVideos.length} neue Videos gefunden.`);

    for (const video of newVideos) {
      if (signal?.aborted) break;
      await supabase.from("yt_videos").upsert({
        video_id: video.videoId,
        channel: handle,
        title: video.title,
        duration: video.duration,
        published_at: video.publishedAt || null,
        status: "pending",
        language: video.targetLang || "en",
      }, { onConflict: "video_id" });
      ytIngestionStats.totalVideosDiscovered++;
    }
  } catch (e: any) {
    if (e.name === "AbortError") throw e;
    log.error(`[YT Sync] Channel ${handle} failed: ${e.message}`);
  }
}

/**
 * Process a pending YouTube video (download transcript + embedding)
 */
async function processSingleYtVideo(video: any, signal?: AbortSignal) {
  try {
    const vtt = await downloadVtt(video.video_id, video.language || "en", signal);
    if (!vtt) {
      await supabase.from("yt_videos").update({ status: "failed", error_msg: "Keine Auto-Captions" }).eq("video_id", video.video_id);
      return;
    }

    const plaintext = vttToPlaintext(vtt);
    await supabase.from("yt_videos").update({
      transcript: plaintext,
      status: "downloaded",
      error_msg: null,
    }).eq("video_id", video.video_id);

    // Generate embedding for transcript / title
    const embedText = `Channel: ${video.channel}\nTitle: ${video.title}\nTranscript:\n${plaintext.substring(0, 4000)}`;
    const [embedding] = await getEmbeddingsBatch([embedText]);

    await supabase.from("yt_videos").update({
      status: "embedded",
      embedding,
    }).eq("video_id", video.video_id);

    ytIngestionStats.totalProcessed++;
    log.info(`[YT Process] Transkript & Embedding abgeschlossen für "${video.title}"`);
  } catch (err: any) {
    if (err.name === "AbortError") throw err;
    log.error(`[YT Process] Video ${video.video_id} failed: ${err.message}`);
    await supabase.from("yt_videos").update({ status: "failed", error_msg: err.message }).eq("video_id", video.video_id);
  }
}

/**
 * Stage 1 & Processing Loop for YouTube
 */
export async function runYtLoop() {
  ytIngestionStats.isRunning = true;
  ytIngestionStats.startTime = Date.now();
  log.info(`[YT Loop] Gestartet (Parallel Channels: ${MAX_CONCURRENT_YT_CHANNELS})`);

  while (ytAbortController && !ytAbortController.signal.aborted) {
    try {
      ytIngestionStats.lastRunTime = Date.now();

      // 1. Channel Discovery (Parallel)
      const { data: channels } = await supabase.from("yt_channels").select("handle").eq("is_active", true);
      if (channels && channels.length > 0) {
        const executing = new Set<Promise<any>>();
        for (const ch of channels) {
          if (!ytAbortController || ytAbortController.signal.aborted) break;
          const p = syncSingleChannel(ch.handle, ytAbortController.signal).then(() => executing.delete(p));
          executing.add(p);
          if (executing.size >= MAX_CONCURRENT_YT_CHANNELS) {
            await Promise.race(executing);
          }
        }
        await Promise.all(executing);
      }

      // 2. Process Pending Videos (One by one with delay)
      const { data: pendingVideos } = await supabase
        .from("yt_videos")
        .select("video_id, channel, title, published_at, language")
        .eq("status", "pending")
        .order("published_at", { ascending: false })
        .limit(3);

      if (pendingVideos && pendingVideos.length > 0) {
        for (const vid of pendingVideos) {
          if (!ytAbortController || ytAbortController.signal.aborted) break;
          await processSingleYtVideo(vid, ytAbortController.signal);
          await new Promise(r => setTimeout(r, 2000));
        }
      }

      // Wait between discovery cycles (1 hour or interruptible)
      const intervalMs = parseInt(Deno.env.get("YT_SYNC_INTERVAL_MS") || "3600000");
      let waited = 0;
      while (waited < intervalMs && ytAbortController && !ytAbortController.signal.aborted) {
        await new Promise(r => setTimeout(r, 10000));
        waited += 10000;
      }
    } catch (err: any) {
      if (err.name === "AbortError") break;
      ytIngestionStats.lastError = err.message;
      log.error(`[YT Loop Error]: ${err.message}`);
      await new Promise(r => setTimeout(r, 30000));
    }
  }

  ytIngestionStats.isRunning = false;
  log.info("[YT Loop] Beendet.");
}

export function startYtWorker() {
  if (ytIngestionStats.isRunning) return;
  ytAbortController = new AbortController();
  runYtLoop().catch(err => log.error(`YT Worker fatal error: ${err.message}`));
}

export function stopYtWorker() {
  if (ytAbortController) {
    ytAbortController.abort();
    ytAbortController = null;
  }
}
