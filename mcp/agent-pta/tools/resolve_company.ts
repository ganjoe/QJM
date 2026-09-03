import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { supabase, log } from "./shared.ts";

const SWITCHYARD_URL = Deno.env.get("SWITCHYARD_URL") || "http://switchyard:4000/v1";

interface ResolveCandidate {
  symbol: string;
  name: string;
  exchange: string;
  secType: string;
  provider: string;
}

/**
 * Query IBKR for company name via ibkr-sync Daemon
 */
async function searchIBKR(companyName: string): Promise<ResolveCandidate | null> {
  try {
    const res = await fetch(`http://ibkr-sync:8005/search?q=${encodeURIComponent(companyName)}`);
    if (!res.ok) {
      log.warn(`[searchIBKR] HTTP ${res.status} for "${companyName}"`);
      return null;
    }
    const data = await res.json();
    const results = data.results || [];
    if (results.length > 0) {
      const top = results.find((r: any) => r.secType === "STK" || r.secType === "ETF") || results[0];
      return {
        symbol: top.symbol,
        name: top.name || companyName,
        exchange: top.exchange || "SMART",
        secType: top.secType,
        provider: "IBKR",
      };
    }
  } catch (err: any) {
    log.error(`[searchIBKR] Error searching for "${companyName}": ${err.message}`);
  }
  return null;
}

/**
 * Query Yahoo Finance Search API for company name
 */
async function searchYahooFinance(companyName: string): Promise<ResolveCandidate | null> {
  try {
    const url = `https://query2.finance.yahoo.com/v1/finance/search?q=${encodeURIComponent(companyName)}&quotesCount=5&newsCount=0`;
    const res = await fetch(url, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Accept": "application/json",
      },
    });

    if (!res.ok) {
      log.warn(`[searchYahooFinance] HTTP ${res.status} for "${companyName}"`);
      return null;
    }

    const data = await res.json();
    const quotes = data?.quotes || [];

    // Prioritize equity / stocks
    const equityQuote = quotes.find((q: any) => q.quoteType === "EQUITY" || q.quoteType === "ETF") || quotes[0];
    if (!equityQuote || !equityQuote.symbol) return null;

    return {
      symbol: equityQuote.symbol.toUpperCase(),
      name: equityQuote.longname || equityQuote.shortname || equityQuote.symbol,
      exchange: equityQuote.exchange || "UNKNOWN",
      secType: equityQuote.quoteType === "ETF" ? "ETF" : "STK",
      provider: "YFINANCE",
    };
  } catch (err: any) {
    log.error(`[searchYahooFinance] Error searching for "${companyName}": ${err.message}`);
    return null;
  }
}

/**
 * Validate match between company name and candidate ticker using local LLM
 */
async function validateWithLLM(companyName: string, candidate: ResolveCandidate): Promise<{ isMatch: boolean; reason: string }> {
  try {
    const baseUrl = SWITCHYARD_URL.endsWith("/v1") ? SWITCHYARD_URL : `${SWITCHYARD_URL}/v1`;
    const prompt = `Determine whether the stock candidate represents the mentioned company.
Mentioned Company Name: "${companyName}"
Candidate Stock: Symbol="${candidate.symbol}", Name="${candidate.name}", Exchange="${candidate.exchange}"

Answer with ONLY valid JSON:
{"is_match": true/false, "reason": "brief 1-sentence reason"}`;

    const res = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer switchyard",
      },
      body: JSON.stringify({
        model: "local",
        messages: [
          { role: "system", content: "You are a financial entity verification specialist. Return ONLY valid JSON." },
          { role: "user", content: prompt },
        ],
        temperature: 0.1,
      }),
    });

    if (!res.ok) {
      log.warn(`[validateWithLLM] Switchyard error ${res.status}, accepting candidate with soft-check`);
      return { isMatch: true, reason: "LLM offline, accepted via provider ranking" };
    }

    const d = await res.json();
    const content = d.choices?.[0]?.message?.content || "";
    const match = content.match(/\{[\s\S]*\}/);
    const parsed = JSON.parse(match ? match[0] : content);

    return {
      isMatch: Boolean(parsed.is_match),
      reason: String(parsed.reason || ""),
    };
  } catch (err: any) {
    log.warn(`[validateWithLLM] Parse or call error: ${err.message}, fallback to true`);
    return { isMatch: true, reason: "Validation fallback" };
  }
}

export async function resolveCompanyLogic(
  company_name: string,
  validate_with_llm: boolean = true,
  force_refresh: boolean = false
): Promise<{
  isError: boolean;
  message: string;
  ticker: string | null;
  provider: string | null;
  exchange: string | null;
  status: "resolved" | "provider failed" | "manual";
}> {
  const cleanName = company_name.trim();
  if (!cleanName) {
    return { isError: true, message: "Fehler: Kein Firmenname angegeben.", ticker: null, provider: null, exchange: null, status: "provider failed" };
  }

  // 1. Check Global Cache
  if (!force_refresh) {
    const { data: cached, error: cacheErr } = await supabase
      .from("company_ticker_mappings")
      .select("*")
      .ilike("company_name", cleanName)
      .maybeSingle();

    if (!cacheErr && cached) {
      if (cached.status === "resolved" || cached.status === "manual") {
        return {
          isError: false,
          message: `✅ Gefunden (Cache): "${cached.company_name}" ➔ **${cached.ticker}** (${cached.provider || "IBKR"}, Exchange: ${cached.exchange || "N/A"})`,
          ticker: cached.ticker,
          provider: cached.provider,
          exchange: cached.exchange,
          status: "resolved"
        };
      } else if (cached.status === "provider failed") {
        return {
          isError: true,
          message: `⚠️ Bereits versucht (Cache): Für "${cached.company_name}" konnte bisher kein Ticker gefunden werden (-provider failed-). Notiz: ${cached.notes || "Keine"}`,
          ticker: null, provider: null, exchange: null, status: "provider failed"
        };
      }
    }
  }

  // 2. Query Provider (IBKR -> Yahoo Finance Search API)
  let candidate = await searchIBKR(cleanName);
  if (!candidate) {
    candidate = await searchYahooFinance(cleanName);
  }

  if (!candidate) {
    // Record provider failed
    await supabase.from("company_ticker_mappings").upsert({
      company_name: cleanName,
      ticker: null,
      provider: null,
      status: "provider failed",
      validated: false,
      notes: "Kein Treffer bei Provider-Suche",
      updated_at: new Date().toISOString(),
    }, { onConflict: "company_name" });

    return {
      isError: true,
      message: `❌ Kein Börsenticker gefunden für "${cleanName}". Status auf -provider failed- gesetzt.`,
      ticker: null, provider: null, exchange: null, status: "provider failed"
    };
  }

  // 3. LLM Validation (if requested)
  if (validate_with_llm) {
    const val = await validateWithLLM(cleanName, candidate);
    if (!val.isMatch) {
      await supabase.from("company_ticker_mappings").upsert({
        company_name: cleanName,
        ticker: null,
        provider: candidate.provider,
        status: "provider failed",
        validated: false,
        notes: `LLM-Validierung fehlgeschlagen: Kandidat "${candidate.symbol}" (${candidate.name}) passte nicht zu "${cleanName}". Grund: ${val.reason}`,
        updated_at: new Date().toISOString(),
      }, { onConflict: "company_name" });

      return {
        isError: true,
        message: `❌ Ticker-Kandidat ${candidate.symbol} (${candidate.name}) passte laut LLM-Prüfung nicht zu "${cleanName}". Status: -provider failed-.`,
        ticker: null, provider: null, exchange: null, status: "provider failed"
      };
    }
  }

  // 4. Save validated mapping into cache
  await supabase.from("company_ticker_mappings").upsert({
    company_name: cleanName,
    ticker: candidate.symbol,
    provider: candidate.provider,
    exchange: candidate.exchange,
    sec_type: candidate.secType,
    status: "resolved",
    validated: true,
    notes: candidate.name,
    updated_at: new Date().toISOString(),
  }, { onConflict: "company_name" });

  return {
    isError: false,
    message: `✅ Erfolgreich aufgelöst: "${cleanName}" ➔ **${candidate.symbol}** (${candidate.name}, Börse: ${candidate.exchange}, Provider: ${candidate.provider})`,
    ticker: candidate.symbol,
    provider: candidate.provider,
    exchange: candidate.exchange,
    status: "resolved",
  };
}

export function registerResolveCompanyTools(server: McpServer) {
  // ── 1. resolve_company_ticker ─────────────────────────────────────
  server.registerTool(
    "resolve_company_ticker",
    {
      title: "Resolve Company Name to Stock Ticker",
      description:
        "Resolves a company or brand name (e.g. 'Serve Robotics', 'Broadcom') to an official broker-ready stock ticker symbol, " +
        "prioritizing Interactive Brokers (IBKR) with automatic Yahoo Finance fallback. Checks and updates the global company_ticker_mappings cache.",
      inputSchema: {
        company_name: z.string().describe("The company or brand name mentioned in speech or text"),
        validate_with_llm: z.boolean().optional().default(true).describe("Whether to verify candidate with LLM"),
        force_refresh: z.boolean().optional().default(false).describe("Bypass cache and force a new lookup"),
      },
    },
    async ({ company_name, validate_with_llm, force_refresh }: any) => {
      try {
        const result = await resolveCompanyLogic(company_name, validate_with_llm, force_refresh);
        return {
          content: [{ type: "text", text: result.message }],
          isError: result.isError
        };
      } catch (err: any) {
        log.error(`[resolve_company_ticker] Error: ${err.message}`);
        return { content: [{ type: "text", text: `Fehler beim Auflösen: ${err.message}` }], isError: true };
      }
    }
  );

  // ── 2. manage_company_mappings ────────────────────────────────────
  server.registerTool(
    "manage_company_mappings",
    {
      title: "Manage Company Ticker Mappings",
      description:
        "Monitor and manage company-to-ticker resolutions across YouTube and X. " +
        "Lists unresolved companies (-provider failed-) and allows manually resolving or updating tickers.",
      inputSchema: {
        action: z.enum(["STATUS", "LIST_FAILED", "MANUAL_RESOLVE", "RETRY_FAILED"]).describe("The management action"),
        company_name: z.string().optional().describe("Company name (required for MANUAL_RESOLVE)"),
        ticker: z.string().optional().describe("Ticker symbol (required for MANUAL_RESOLVE)"),
        provider: z.string().optional().default("MANUAL").describe("Provider label for manual resolution"),
        notes: z.string().optional().describe("Optional notes (e.g. source URL from web search)"),
      },
    },
    async ({ action, company_name, ticker, provider, notes }: any) => {
      try {
        if (action === "STATUS") {
          const [
            { count: totalCount },
            { count: resolvedCount },
            { count: failedCount },
            { count: manualCount },
          ] = await Promise.all([
            supabase.from("company_ticker_mappings").select("*", { count: "exact", head: true }),
            supabase.from("company_ticker_mappings").select("*", { count: "exact", head: true }).eq("status", "resolved"),
            supabase.from("company_ticker_mappings").select("*", { count: "exact", head: true }).eq("status", "provider failed"),
            supabase.from("company_ticker_mappings").select("*", { count: "exact", head: true }).eq("status", "manual"),
          ]);

          const lines = [
            "=== Status des globalen Ticker-Mapping-Caches ===",
            `• Gesamt erfasste Firmen: ${totalCount || 0}`,
            `• Erfolgreich aufgelöst (automatisch): ${resolvedCount || 0}`,
            `• Manuell korrigiert / verifiziert: ${manualCount || 0}`,
            `• Nicht auflösbar (-provider failed-): ${failedCount || 0}`,
          ];

          return { content: [{ type: "text", text: lines.join("\n") }] };
        }

        if (action === "LIST_FAILED") {
          const { data: failed, error } = await supabase
            .from("company_ticker_mappings")
            .select("company_name, notes, updated_at")
            .eq("status", "provider failed")
            .order("updated_at", { ascending: false })
            .limit(50);

          if (error) throw error;
          if (!failed || failed.length === 0) {
            return { content: [{ type: "text", text: "✅ Keine offenen -provider failed- Einträge vorhanden! Alle Firmen wurden erfolgreich zugeordnet." }] };
          }

          const formatted = failed.map((f: any, idx: number) =>
            `${idx + 1}. **"${f.company_name}"** — Grund: ${f.notes || "Kein Provider-Treffer"} (Stand: ${new Date(f.updated_at).toLocaleDateString()})`
          ).join("\n");

          return {
            content: [{
              type: "text",
              text: `### Offene Fälle (-provider failed-):\nVerwende \`action: "MANUAL_RESOLVE"\` mit \`company_name\` und \`ticker\`, um einen Ticker zuzuweisen.\n\n${formatted}`,
            }],
          };
        }

        if (action === "MANUAL_RESOLVE") {
          if (!company_name || !ticker) {
            return { content: [{ type: "text", text: "Fehler: company_name und ticker sind für MANUAL_RESOLVE erforderlich." }], isError: true };
          }

          const cleanName = company_name.trim();
          const cleanTicker = ticker.trim().toUpperCase();

          // 1. Upsert global mapping
          const { error: mapErr } = await supabase.from("company_ticker_mappings").upsert({
            company_name: cleanName,
            ticker: cleanTicker,
            provider: provider || "MANUAL",
            status: "manual",
            validated: true,
            notes: notes || "Manuell über MCP-Tool gelöst",
            updated_at: new Date().toISOString(),
          }, { onConflict: "company_name" });

          if (mapErr) throw mapErr;

          // 2. Cascade update to all affected YouTube videos
          const { data: affectedVideos } = await supabase
            .from("yt_videos")
            .select("video_id, companies, tickers")
            .contains("companies", JSON.stringify([{ name: cleanName }]));

          let updatedCount = 0;
          if (affectedVideos && affectedVideos.length > 0) {
            for (const vid of affectedVideos) {
              const comps = vid.companies || [];
              let changed = false;
              for (const c of comps) {
                if (c.name.toLowerCase() === cleanName.toLowerCase()) {
                  c.ticker = cleanTicker;
                  c.status = "resolved";
                  changed = true;
                }
              }
              if (changed) {
                const updatedTickers = Array.from(new Set([...(vid.tickers || []), cleanTicker]));
                await supabase.from("yt_videos").update({
                  companies: comps,
                  tickers: updatedTickers,
                }).eq("video_id", vid.video_id);
                updatedCount++;
              }
            }
          }

          // 3. Cascade update to x_posts (agent_workspace)
          const { data: affectedPosts } = await supabase
            .from("agent_workspace")
            .select("id, companies, tickers")
            .contains("companies", JSON.stringify([{ name: cleanName }]));

          let updatedPostsCount = 0;
          if (affectedPosts && affectedPosts.length > 0) {
            for (const post of affectedPosts) {
              const comps = post.companies || [];
              let changed = false;
              for (const c of comps) {
                if (c.name.toLowerCase() === cleanName.toLowerCase()) {
                  c.ticker = cleanTicker;
                  c.status = "resolved";
                  changed = true;
                }
              }
              if (changed) {
                const updatedTickers = Array.from(new Set([...(post.tickers || []), cleanTicker]));
                await supabase.from("agent_workspace").update({
                  companies: comps,
                  tickers: updatedTickers,
                }).eq("id", post.id);
                updatedPostsCount++;
              }
            }
          }

          return {
            content: [{
              type: "text",
              text: `✅ Manuelle Zuordnung gespeichert: "${cleanName}" ➔ **${cleanTicker}**.\nGlobaler Cache aktualisiert. In ${updatedCount} YouTube-Videos und ${updatedPostsCount} X-Posts rückwirkend eingetragen!`,
            }],
          };
        }

        if (action === "RETRY_FAILED") {
          const { data, error } = await supabase
            .from("company_ticker_mappings")
            .delete()
            .eq("status", "provider failed")
            .select("company_name");

          if (error) throw error;
          const count = data?.length || 0;
          return {
            content: [{
              type: "text",
              text: `🔄 ${count} fehlgeschlagene Einträge wurden aus dem Cache gelöscht. Der Resolver wird sie beim nächsten Durchlauf erneut prüfen.`,
            }],
          };
        }

        return { content: [{ type: "text", text: `Unbekannte Aktion: ${action}` }], isError: true };
      } catch (err: any) {
        log.error(`[manage_company_mappings] Error: ${err.message}`);
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );
}
