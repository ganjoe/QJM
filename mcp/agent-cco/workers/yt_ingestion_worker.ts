import { supabase, log, getEmbeddingsBatch, MAX_CONCURRENT_YT_CHANNELS, AGENT_ID } from "../tools/shared.ts";

export const ytIngestionStats = {
  isRunning: false,
  startTime: 0,
  lastRunTime: 0,
  totalVideosDiscovered: 0,
  totalTranscriptsDownloaded: 0,
  totalVideosProcessed: 0,
  totalChunksProcessed: 0,
  totalTokensEstimated: 0,
  lastBatchChunks: 0,
  lastBatchDurationMs: 0,
  currentChunksPerSec: 0,
  currentTokensPerSec: 0,
  avgChunkLatencyMs: 0,
  activeEmbeddingsRunning: 0,
  activeDownloadsRunning: 0,
  lastProcessedTitle: "",
  lastProcessedChannel: "",
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
 * Chunk transcript text into manageable overlapping segments
 */
export function chunkTranscript(
  transcript: string,
  chunkSize: number = parseInt(Deno.env.get("YT_CHUNK_SIZE") || "1500"),
  overlap: number = parseInt(Deno.env.get("YT_CHUNK_OVERLAP") || "200"),
): string[] {
  if (!transcript || transcript.trim().length === 0) return [];
  const text = transcript.trim();
  if (text.length <= chunkSize) return [text];

  const chunks: string[] = [];
  let startIndex = 0;

  while (startIndex < text.length) {
    let endIndex = startIndex + chunkSize;

    if (endIndex >= text.length) {
      chunks.push(text.substring(startIndex).trim());
      break;
    }

    const slice = text.substring(startIndex, endIndex);
    const lastBreak = Math.max(
      slice.lastIndexOf("\n"),
      slice.lastIndexOf(". "),
      slice.lastIndexOf("? "),
      slice.lastIndexOf("! "),
      slice.lastIndexOf(" "),
    );

    if (lastBreak > chunkSize * 0.6) {
      endIndex = startIndex + lastBreak + 1;
    }

    const chunk = text.substring(startIndex, endIndex).trim();
    if (chunk.length > 0) {
      chunks.push(chunk);
    }

    startIndex = Math.max(startIndex + 1, endIndex - overlap);
  }

  return chunks;
}

/**
 * Step 1: Download transcript for a pending YouTube video (Network only, zero GPU usage)
 */
async function downloadSingleTranscript(video: any, signal?: AbortSignal): Promise<boolean> {
  try {
    const vtt = await downloadVtt(video.video_id, video.language || "en", signal);
    if (!vtt) {
      await supabase.from("yt_videos").update({ status: "failed", error_msg: "Keine Auto-Captions" }).eq("video_id", video.video_id);
      return false;
    }

    const plaintext = vttToPlaintext(vtt);
    if (!plaintext || plaintext.trim().length === 0) {
      await supabase.from("yt_videos").update({ status: "failed", error_msg: "Leeres Transkript" }).eq("video_id", video.video_id);
      return false;
    }

    await supabase.from("yt_videos").update({
      transcript: plaintext,
      status: "downloaded",
      error_msg: null,
    }).eq("video_id", video.video_id);

    ytIngestionStats.totalTranscriptsDownloaded++;
    log.info(`[YT Downloader] Transkript geladen für "${video.title}" (${video.channel})`);
    return true;
  } catch (err: any) {
    if (err.name === "AbortError" || signal?.aborted) throw err;
    log.error(`[YT Downloader] Video ${video.video_id} download failed: ${err.message}`);
    await supabase.from("yt_videos").update({ status: "failed", error_msg: err.message }).eq("video_id", video.video_id);
    return false;
  }
}

/**
 * Step 2: Generate embeddings for a downloaded video (GPU only, zero YouTube network delay)
 */
async function embedSingleTranscript(video: any, signal?: AbortSignal): Promise<boolean> {
  try {
    const plaintext = video.transcript;
    if (!plaintext || plaintext.trim().length === 0) {
      await supabase.from("yt_videos").update({ status: "failed", error_msg: "Leeres Transkript" }).eq("video_id", video.video_id);
      return false;
    }

    const chunks = chunkTranscript(plaintext);
    if (chunks.length === 0) {
      await supabase.from("yt_videos").update({ status: "failed", error_msg: "Keine Chunks generierbar" }).eq("video_id", video.video_id);
      return false;
    }

    const augmentedTexts = chunks.map((chunk, idx) =>
      `[Video: "${video.title}" | Kanal: ${video.channel} | Datum: ${video.published_at || "Unbekannt"}]\n\n${chunk}`
    );

    ytIngestionStats.activeEmbeddingsRunning++;
    const t0 = Date.now();
    let embeddings: number[][];
    try {
      embeddings = await getEmbeddingsBatch(augmentedTexts);
    } finally {
      ytIngestionStats.activeEmbeddingsRunning = Math.max(0, ytIngestionStats.activeEmbeddingsRunning - 1);
    }
    const durationMs = Math.max(1, Date.now() - t0);

    // Live performance metrics calculation
    const chunksPerSec = Number((chunks.length / (durationMs / 1000)).toFixed(1));
    const totalChars = chunks.reduce((acc, c) => acc + c.length, 0);
    const estTokens = Math.round(totalChars / 4);
    const tokensPerSec = Math.round(estTokens / (durationMs / 1000));
    const avgLatencyMs = Math.round(durationMs / chunks.length);

    ytIngestionStats.lastBatchChunks = chunks.length;
    ytIngestionStats.lastBatchDurationMs = durationMs;
    ytIngestionStats.currentChunksPerSec = chunksPerSec;
    ytIngestionStats.currentTokensPerSec = tokensPerSec;
    ytIngestionStats.avgChunkLatencyMs = avgLatencyMs;
    ytIngestionStats.totalChunksProcessed += chunks.length;
    ytIngestionStats.totalTokensEstimated += estTokens;
    ytIngestionStats.totalVideosProcessed++;
    ytIngestionStats.lastProcessedTitle = video.title || "";
    ytIngestionStats.lastProcessedChannel = video.channel || "";

    const rowsToInsert = chunks.map((chunk, idx) => ({
      agent_id: AGENT_ID || "cco",
      artifact_type: "yt_chunk",
      content: augmentedTexts[idx],
      embedding: embeddings[idx],
      created_at: video.published_at || new Date().toISOString(),
      metadata: {
        channel: video.channel,
        video_id: video.video_id,
        video_title: video.title,
        block_index: idx,
        total_blocks: chunks.length,
        published_at: video.published_at,
        stage: "embedded",
      },
    }));

    await supabase.from("agent_workspace").delete().eq("artifact_type", "yt_chunk").eq("metadata->>video_id", video.video_id);
    const { error: insErr } = await supabase.from("agent_workspace").insert(rowsToInsert);
    if (insErr) throw insErr;

    await supabase.from("yt_videos").update({
      status: "embedded",
      chunk_count: chunks.length,
      error_msg: null,
    }).eq("video_id", video.video_id);

    log.info(`[YT Embedder] Fertig: ${chunks.length} Chunks in ${(durationMs / 1000).toFixed(2)}s (${chunksPerSec} Chunks/s, ~${tokensPerSec} t/s) für "${video.title}"`);
    return true;
  } catch (err: any) {
    if (err.name === "AbortError" || signal?.aborted) throw err;
    log.error(`[YT Embedder] Video ${video.video_id} failed: ${err.message}`);
    await supabase.from("yt_videos").update({ status: "failed", error_msg: err.message }).eq("video_id", video.video_id);
    return false;
  }
}

/**
 * Loop 1: Independent Transcript Downloader (Network/yt-dlp -> DB 'downloaded')
 */
async function runTranscriptDownloadLoop() {
  const downloadBatchLimit = parseInt(Deno.env.get("YT_DOWNLOAD_BATCH_LIMIT") || "6");
  const downloadConcurrency = parseInt(Deno.env.get("YT_DOWNLOAD_CONCURRENCY") || "2");
  const delayBetweenDownloadsMs = parseInt(Deno.env.get("YT_DOWNLOAD_DELAY_MS") || "500");
  const pollIntervalMs = parseInt(Deno.env.get("YT_DOWNLOAD_POLL_INTERVAL_MS") || "5000");

  log.info(`[YT Downloader Loop] Gestartet (Concurrency: ${downloadConcurrency})`);

  while (ytAbortController && !ytAbortController.signal.aborted) {
    try {
      const { data: pendingVideos, error } = await supabase
        .from("yt_videos")
        .select("video_id, channel, title, published_at, language")
        .eq("status", "pending")
        .order("published_at", { ascending: false, nullsFirst: false })
        .limit(downloadBatchLimit);

      if (error) throw error;

      if (!pendingVideos || pendingVideos.length === 0) {
        await new Promise(r => setTimeout(r, pollIntervalMs));
        continue;
      }

      const executing = new Set<Promise<any>>();
      for (const vid of pendingVideos) {
        if (!ytAbortController || ytAbortController.signal.aborted) break;
        ytIngestionStats.activeDownloadsRunning++;
        const p: Promise<any> = downloadSingleTranscript(vid, ytAbortController.signal)
          .finally(() => {
            executing.delete(p);
            ytIngestionStats.activeDownloadsRunning = Math.max(0, ytIngestionStats.activeDownloadsRunning - 1);
          });
        executing.add(p);
        if (executing.size >= downloadConcurrency) {
          await Promise.race(executing);
        }
        if (delayBetweenDownloadsMs > 0) {
          await new Promise(r => setTimeout(r, delayBetweenDownloadsMs));
        }
      }
      await Promise.all(executing);
    } catch (err: any) {
      if (err.name === "AbortError" || ytAbortController?.signal.aborted) break;
      log.error(`[YT Downloader Error]: ${err.message}`);
      await new Promise(r => setTimeout(r, 10000));
    }
  }
}

/**
 * Loop 2: Independent GPU Embedder (Local DB 'downloaded' -> Ollama GPU -> 'embedded')
 */
async function runEmbeddingLoop() {
  const embedBatchLimit = parseInt(Deno.env.get("YT_EMBED_BATCH_LIMIT") || "4");
  const embedConcurrency = parseInt(Deno.env.get("YT_EMBED_CONCURRENCY") || "2");
  const pollIntervalMs = parseInt(Deno.env.get("YT_EMBED_POLL_INTERVAL_MS") || "2000");

  log.info(`[YT Embedder Loop] Gestartet (Concurrency: ${embedConcurrency})`);

  while (ytAbortController && !ytAbortController.signal.aborted) {
    try {
      const { data: downloadedVideos, error } = await supabase
        .from("yt_videos")
        .select("video_id, channel, title, published_at, language, transcript, status")
        .eq("status", "downloaded")
        .order("published_at", { ascending: false, nullsFirst: false })
        .limit(embedBatchLimit);

      if (error) throw error;

      if (!downloadedVideos || downloadedVideos.length === 0) {
        await new Promise(r => setTimeout(r, pollIntervalMs));
        continue;
      }

      const executing = new Set<Promise<any>>();
      for (const vid of downloadedVideos) {
        if (!ytAbortController || ytAbortController.signal.aborted) break;
        const p: Promise<any> = embedSingleTranscript(vid, ytAbortController.signal)
          .finally(() => executing.delete(p));
        executing.add(p);
        if (executing.size >= embedConcurrency) {
          await Promise.race(executing);
        }
      }
      await Promise.all(executing);
    } catch (err: any) {
      if (err.name === "AbortError" || ytAbortController?.signal.aborted) break;
      log.error(`[YT Embedder Error]: ${err.message}`);
      await new Promise(r => setTimeout(r, 5000));
    }
  }
}

/**
 * Loop 3: Periodic Channel Discovery
 */
async function runChannelDiscoveryLoop() {
  const intervalMs = parseInt(Deno.env.get("YT_SYNC_INTERVAL_MS") || "3600000");
  log.info(`[YT Discovery Loop] Gestartet (Intervall: ${Math.round(intervalMs / 60000)}m)`);

  while (ytAbortController && !ytAbortController.signal.aborted) {
    try {
      const { data: channels } = await supabase.from("yt_channels").select("handle").eq("is_active", true);
      if (channels && channels.length > 0) {
        const executing = new Set<Promise<any>>();
        for (const ch of channels) {
          if (!ytAbortController || ytAbortController.signal.aborted) break;
          let p: Promise<any>;
          p = syncSingleChannel(ch.handle, ytAbortController.signal).then(() => executing.delete(p));
          executing.add(p);
          if (executing.size >= MAX_CONCURRENT_YT_CHANNELS) {
            await Promise.race(executing);
          }
        }
        await Promise.all(executing);
      }
      let waited = 0;
      while (waited < intervalMs && ytAbortController && !ytAbortController.signal.aborted) {
        await new Promise(r => setTimeout(r, 10000));
        waited += 10000;
      }
    } catch (err: any) {
      if (err.name === "AbortError" || ytAbortController?.signal.aborted) break;
      log.error(`[YT Discovery Error]: ${err.message}`);
      await new Promise(r => setTimeout(r, 60000));
    }
  }
}

/**
 * Main YouTube Worker Loop: Runs Discovery, Downloader, and GPU Embedder concurrently
 */
export async function runYtLoop() {
  ytIngestionStats.isRunning = true;
  ytIngestionStats.startTime = Date.now();
  log.info("[YT Loop] Haupt-Pipeline gestartet: Discovery, Downloader & GPU Embedder laufen parallel!");

  try {
    await Promise.all([
      runChannelDiscoveryLoop(),
      runTranscriptDownloadLoop(),
      runEmbeddingLoop(),
    ]);
  } catch (err: any) {
    if (err.name !== "AbortError") {
      log.error(`[YT Loop Fatal]: ${err.message}`);
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
