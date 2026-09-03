import { supabase, log, SWITCHYARD_URL, isValidTicker } from "../tools/shared.ts";

export const companyExtractionStats = {
  isRunning: false,
  startTime: 0,
  lastRunTime: 0,
  totalVideosScanned: 0,
  totalCompaniesExtracted: 0,
  totalResolvedTickers: 0,
  totalFailedTickers: 0,
  chunksProcessed: 0,
  timeSpentMs: 0,
  chunksPerSec: 0,
  lastError: null as string | null,
};

let extractionAbortController: AbortController | null = null;

interface ExtractedMention {
  name: string;
  timestamp: string;
  quote: string;
}

interface ResolvedCompanyEntry {
  name: string;
  ticker: string | null;
  provider: string | null;
  exchange: string | null;
  status: "resolved" | "provider failed" | "manual";
  timestamp: string;
  timestamp_sec: number;
  quote: string;
}

/**
 * Parses timestamp string "MM:SS" or "HH:MM:SS" to total seconds
 */
function parseTimestampToSec(ts: string): number {
  if (!ts) return 0;
  const parts = ts.replace(/[\[\]]/g, "").trim().split(":");
  if (parts.length === 2) {
    return parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
  } else if (parts.length === 3) {
    return parseInt(parts[0], 10) * 3600 + parseInt(parts[1], 10) * 60 + parseInt(parts[2], 10);
  }
  return 0;
}

/**
 * Split transcript into chunks preserving timestamp markers, with overlap
 */
function chunkTranscriptForLLM(transcript: string, maxChars: number = 8000, overlapChars: number = 200): string[] {
  if (!transcript || transcript.trim().length === 0) return [];
  const lines = transcript.split("\n");
  const chunks: string[] = [];
  let currentChunk: string[] = [];
  let currentLen = 0;

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    currentChunk.push(line);
    currentLen += line.length + 1;

    if (currentLen >= maxChars || i === lines.length - 1) {
      chunks.push(currentChunk.join("\n"));
      
      // Determine overlap for next chunk
      if (i < lines.length - 1) {
        let overlapLen = 0;
        let overlapLines = [];
        for (let j = currentChunk.length - 1; j >= 0; j--) {
           const l = currentChunk[j];
           if (overlapLen + l.length + 1 > overlapChars && overlapLines.length > 0) break;
           overlapLines.unshift(l);
           overlapLen += l.length + 1;
        }
        currentChunk = [...overlapLines];
        currentLen = overlapLen;
      } else {
        currentChunk = [];
        currentLen = 0;
      }
    }
    i++;
  }
  return chunks;
}

/**
 * Step 1: Extract company names and timestamps from transcript text using Gemma (Local LLM)
 */
async function extractCompaniesFromText(textSegment: string, signal?: AbortSignal): Promise<ExtractedMention[]> {
  try {
    const baseUrl = SWITCHYARD_URL.endsWith("/v1") ? SWITCHYARD_URL : `${SWITCHYARD_URL}/v1`;
    const systemPrompt = `You are a financial entity extraction assistant.
Extract all company names, publicly traded stocks, and commercial brands mentioned or discussed in this YouTube transcript segment.
For each mention, find:
1. "name": The exact full company or brand name (e.g. "Serve Robotics", "Broadcom", "AST SpaceMobile"). Do NOT guess or output ticker symbols, only the real company/brand name.
2. "timestamp": The nearest preceding timestamp in the text (e.g. "12:45" or "[12:45]").
3. "quote": The short sentence where the company was mentioned.

Return ONLY a valid JSON array of objects:
[
  {"name": "Serve Robotics", "timestamp": "12:45", "quote": "..."}
]
If no companies or stocks are mentioned in this text, return: []
Do not include commentary or markdown.`;

    const res = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer switchyard",
      },
      body: JSON.stringify({
        model: "local",
        messages: [
          { role: "system", content: systemPrompt },
          { role: "user", content: textSegment },
        ],
        temperature: 0.1,
      }),
      signal,
    });

    if (!res.ok) {
      log.warn(`[extractCompaniesFromText] Switchyard HTTP ${res.status}`);
      return [];
    }

    const data = await res.json();
    const raw = data?.choices?.[0]?.message?.content || "[]";
    const jsonMatch = raw.match(/\[\s*\{[\s\S]*\}\s*\]/) || raw.match(/\[\s*\]/);
    const parsed = JSON.parse(jsonMatch ? jsonMatch[0] : raw);

    if (!Array.isArray(parsed)) return [];

    return parsed
      .filter((item: any) => item && typeof item.name === "string" && item.name.trim().length > 1)
      .map((item: any) => ({
        name: item.name.trim(),
        timestamp: String(item.timestamp || "00:00").replace(/[\[\]]/g, "").trim(),
        quote: String(item.quote || "").trim().substring(0, 300),
      }));
  } catch (err: any) {
    if (err.name === "AbortError") throw err;
    log.error(`[extractCompaniesFromText] Error: ${err.message}`);
    return [];
  }
}

/**
 * Step 2: Resolve company name to stock ticker via agent-pta HTTP endpoint
 */
async function resolveCompanyTicker(companyName: string): Promise<{
  ticker: string | null;
  provider: string | null;
  exchange: string | null;
  status: "resolved" | "provider failed" | "manual";
}> {
  try {
    const res = await fetch(`http://llm-gw-mcp-pta:8789/resolve?company_name=${encodeURIComponent(companyName)}`);
    if (res.ok) {
      const data = await res.json();
      return {
        ticker: data.ticker || null,
        provider: data.provider || null,
        exchange: data.exchange || null,
        status: data.status || "provider failed",
      };
    } else {
      log.warn(`[resolveCompanyTicker] HTTP ${res.status} from agent-pta`);
    }
  } catch (err: any) {
    log.error(`[resolveCompanyTicker] Request failed: ${err.message}`);
  }
  return {
    ticker: null,
    provider: null,
    exchange: null,
    status: "provider failed",
  };
}

/**
 * Process a single video: extract companies, resolve tickers, calculate timestamp seconds, and persist
 */
export async function processVideoCompanies(video: any, signal?: AbortSignal): Promise<boolean> {
  try {
    if (!video.transcript || video.transcript.trim().length === 0) return false;

    const chunks = chunkTranscriptForLLM(video.transcript);
    const mentionsMap = new Map<string, ExtractedMention>();

    const concurrency = parseInt(Deno.env.get("YT_COMPANY_CONCURRENCY") || "3");
    
    // Process with concurrency
    let active = 0;
    let index = 0;
    const startMs = Date.now();
    await new Promise<void>((resolve, reject) => {
      function next() {
        if (signal?.aborted) return reject(new Error("Aborted"));
        if (index >= chunks.length && active === 0) {
          resolve();
          return;
        }
        while (active < concurrency && index < chunks.length) {
          const i = index++;
          active++;
          extractCompaniesFromText(chunks[i], signal)
            .then(mentions => {
              for (const m of mentions) {
                const key = m.name.toLowerCase();
                if (!mentionsMap.has(key)) mentionsMap.set(key, m);
              }
              companyExtractionStats.chunksProcessed++;
            })
            .catch(err => log.error(`Chunk extraction failed: ${err.message}`))
            .finally(() => {
              active--;
              next();
            });
        }
      }
      next();
    });

    const elapsed = Date.now() - startMs;
    companyExtractionStats.timeSpentMs += elapsed;
    if (companyExtractionStats.timeSpentMs > 0) {
      companyExtractionStats.chunksPerSec = Number((companyExtractionStats.chunksProcessed / (companyExtractionStats.timeSpentMs / 1000)).toFixed(2));
    }

    const resolvedEntries: ResolvedCompanyEntry[] = [];
    const resolvedTickersSet = new Set<string>();

    for (const mention of mentionsMap.values()) {
      const resolved = await resolveCompanyTicker(mention.name);
      const sec = parseTimestampToSec(mention.timestamp);

      resolvedEntries.push({
        name: mention.name,
        ticker: resolved.ticker,
        provider: resolved.provider,
        exchange: resolved.exchange,
        status: resolved.status,
        timestamp: mention.timestamp,
        timestamp_sec: sec,
        quote: mention.quote,
      });

      if (resolved.ticker && resolved.status === "resolved") {
        resolvedTickersSet.add(resolved.ticker);
        companyExtractionStats.totalResolvedTickers++;
      } else {
        companyExtractionStats.totalFailedTickers++;
      }
      companyExtractionStats.totalCompaniesExtracted++;
    }

    if (resolvedEntries.length === 0) {
      resolvedEntries.push({
        name: "_none_",
        ticker: null,
        provider: null,
        exchange: null,
        status: "resolved",
        timestamp: "00:00",
        timestamp_sec: 0,
        quote: "Keine Firmen erwähnt",
      });
    }

    const finalTickers = Array.from(resolvedTickersSet);

    await supabase.from("yt_videos").update({
      companies: resolvedEntries,
      tickers: finalTickers,
    }).eq("video_id", video.video_id);

    companyExtractionStats.totalVideosScanned++;
    const tickerSummary = finalTickers.length > 0 ? `➔ Ticker: ${finalTickers.join(", ")}` : "➔ Keine Börsenticker";
    log.info(`[YT Company Extractor] "${video.title}" (${video.channel}): ${resolvedEntries.length} Einträge ${tickerSummary}`);
    return true;
  } catch (err: any) {
    if (err.name === "AbortError" || signal?.aborted) throw err;
    log.error(`[YT Company Extractor] Error processing video ${video.video_id}: ${err.message}`);
    return false;
  }
}

/**
 * Background loop: Scans videos that have transcripts but empty companies
 */
export async function runCompanyExtractionLoop() {
  companyExtractionStats.isRunning = true;
  companyExtractionStats.startTime = Date.now();
  log.info("[YT Company Extractor] Loop gestartet.");

  const batchLimit = parseInt(Deno.env.get("YT_COMPANY_BATCH_LIMIT") || "2");
  const delayBetweenVideosMs = parseInt(Deno.env.get("YT_COMPANY_DELAY_MS") || "500");
  const pollIntervalMs = parseInt(Deno.env.get("YT_COMPANY_POLL_INTERVAL_MS") || "8000");

  while (extractionAbortController && !extractionAbortController.signal.aborted) {
    try {
      companyExtractionStats.lastRunTime = Date.now();

      // Find videos with transcripts where companies has not been extracted yet
      const { data: candidates, error } = await supabase
        .from("yt_videos")
        .select("video_id, title, channel, published_at, transcript, companies")
        .not("transcript", "is", null)
        .order("published_at", { ascending: false, nullsFirst: false })
        .limit(batchLimit * 10);

      if (error) throw error;

      const pendingVideos = (candidates || []).filter((v: any) => {
        return !v.companies || !Array.isArray(v.companies) || v.companies.length === 0;
      }).slice(0, batchLimit);

      if (pendingVideos.length === 0) {
        await new Promise(r => setTimeout(r, pollIntervalMs));
        continue;
      }

      for (const vid of pendingVideos) {
        if (!extractionAbortController || extractionAbortController.signal.aborted) break;
        await processVideoCompanies(vid, extractionAbortController.signal);
        if (delayBetweenVideosMs > 0) {
          await new Promise(r => setTimeout(r, delayBetweenVideosMs));
        }
      }
    } catch (err: any) {
      if (err.name === "AbortError" || extractionAbortController?.signal.aborted) break;
      companyExtractionStats.lastError = err.message;
      log.error(`[YT Company Extractor Loop Error]: ${err.message}`);
      await new Promise(r => setTimeout(r, 10000));
    }
  }

  companyExtractionStats.isRunning = false;
  log.info("[YT Company Extractor] Loop beendet.");
}

export function startCompanyExtractionWorker() {
  if (companyExtractionStats.isRunning) return;
  extractionAbortController = new AbortController();
  runCompanyExtractionLoop().catch(err => log.error(`Company Extraction Worker fatal: ${err.message}`));
}

export function stopCompanyExtractionWorker() {
  if (extractionAbortController) {
    extractionAbortController.abort();
    extractionAbortController = null;
  }
}
