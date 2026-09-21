import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { STOCK_DATA_NODE_URL, log, supabase } from "./shared.ts";
import { registerMassiveTools } from "./massive.ts";

declare const Deno: any;

// Bekannte Ticker-Mappings für Yahoo Finance
const STATIC_YF_MAPPINGS: Record<string, string> = {
  "LPK": "LPK.DE",
  "HPS.A": "HPS-A.TO",
  "SOI": "SOI.PA",
  "XFAB": "XFAB.PA",
  "SIVE": "SIVE.ST",
  "4GLD": "4GLD.DE",
  "SYSTEM": "SKIP",
  "NOD": "NOD.OL",
  "KCLI.CN": "KCLI.CN",
  "VLX.L": "VLX.L",
  "VLXGF": "VLXGF",
  "AMD": "AMD",
  "BRK.A": "BRK-A",
  "BRK.B": "BRK-B",
  "BF.B": "BF-B",
};

const SKIP_PREFIXES = ["$", "=", "^"];

// ── SEC EDGAR CIK Cache ────────────────────────────────────────────────
let secCikMap: Record<string, number> | null = null;
let secCikCacheTime = 0;
const SEC_CIK_CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24h

function getSecUserAgent(): string {
  return Deno.env.get("SEC_USER_AGENT") || "OpenBrain SEC_Bot@example.com";
}

function getSecRateLimitMs(): number {
  return Number(Deno.env.get("SEC_RATE_LIMIT_MS") || 200);
}

function normalizeTickerForSec(ticker: string): string {
  const t = ticker.trim().toUpperCase();
  const parts = t.split(".");
  if (parts.length === 2 && ["A", "B", "C", "WS", "U"].includes(parts[1])) {
    return `${parts[0]}-${parts[1]}`;
  }
  return t;
}

async function loadSecCikMapping(): Promise<Record<string, number>> {
  const now = Date.now();
  if (secCikMap && (now - secCikCacheTime) < SEC_CIK_CACHE_TTL_MS) {
    return secCikMap;
  }
  const ua = getSecUserAgent();
  const res = await fetch("https://www.sec.gov/files/company_tickers.json", {
    headers: { "User-Agent": ua },
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) throw new Error(`SEC company_tickers.json: HTTP ${res.status}`);
  const data = await res.json();
  const map: Record<string, number> = {};
  for (const entry of Object.values(data) as any[]) {
    map[entry.ticker] = entry.cik_str;
  }
  secCikMap = map;
  secCikCacheTime = now;
  log.info(`[SEC] CIK-Mapping geladen: ${Object.keys(map).length} Ticker`);
  return map;
}

async function fetchSecSubmissions(cik: number): Promise<any> {
  const ua = getSecUserAgent();
  const paddedCik = String(cik).padStart(10, "0");
  const res = await fetch(`https://data.sec.gov/submissions/CIK${paddedCik}.json`, {
    headers: { "User-Agent": ua },
    signal: AbortSignal.timeout(20_000),
  });
  if (!res.ok) throw new Error(`SEC submissions: HTTP ${res.status}`);
  return res.json();
}

async function downloadSecDocument(url: string): Promise<Uint8Array> {
  const ua = getSecUserAgent();
  const res = await fetch(url, {
    headers: { "User-Agent": ua },
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) throw new Error(`SEC download: HTTP ${res.status}`);
  return new Uint8Array(await res.arrayBuffer());
}

function normalizeTickerForYf(ticker: string): string | null {
  const t = ticker.trim().toUpperCase();
  if (!t || SKIP_PREFIXES.some(p => t.startsWith(p))) return null;
  if (STATIC_YF_MAPPINGS[t]) {
    const m = STATIC_YF_MAPPINGS[t];
    return m === "SKIP" ? null : m;
  }
  const parts = t.split(".");
  if (parts.length === 2 && ["A", "B", "C", "WS", "U"].includes(parts[1])) {
    return `${parts[0]}-${parts[1]}`;
  }
  return t;
}

export async function fetchTickerMetadata(ticker: string, timeoutMs?: number): Promise<Record<string, any> | null> {
  const symbol = normalizeTickerForYf(ticker);
  if (!symbol) return null;
  const timeout = timeoutMs || Number(Deno.env.get("METADATA_TIMEOUT_MS") || 8000);

  try {
    const headers = {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
      "Accept": "application/json"
    };

    let shares: number | null = null;
    let currency: string | null = null;
    let eps: number | null = null;
    let revenue: number | null = null;
    let earningsIso: string | null = null;

    // 1. QuoteSummary v10 abrufen
    try {
      const url = `https://query2.finance.yahoo.com/v10/finance/quoteSummary/${encodeURIComponent(symbol)}?modules=defaultKeyStatistics,financialData,calendarEvents`;
      const res = await fetch(url, { headers, signal: AbortSignal.timeout(timeout) });
      if (res.ok) {
        const data = await res.json();
        const result = data?.quoteSummary?.result?.[0];
        if (result) {
          const keyStats = result.defaultKeyStatistics || {};
          const finData = result.financialData || {};
          const calEvents = result.calendarEvents || {};

          const sRaw = keyStats.sharesOutstanding?.raw ?? keyStats.impliedSharesOutstanding?.raw;
          if (typeof sRaw === "number") shares = sRaw;

          const cRaw = finData.financialCurrency || keyStats.currency;
          if (typeof cRaw === "string" && cRaw.trim()) currency = cRaw.trim().toUpperCase();

          const epsRaw = keyStats.trailingEps?.raw ?? keyStats.forwardEps?.raw;
          if (typeof epsRaw === "number") eps = epsRaw;

          const revRaw = finData.totalRevenue?.raw;
          if (typeof revRaw === "number") revenue = revRaw;

          const earnTs = calEvents.earnings?.earningsDate?.[0]?.raw;
          if (typeof earnTs === "number") earningsIso = new Date(earnTs * 1000).toISOString();
        }
      }
    } catch (_err) {
      // Weiter zum Fallback
    }

    // 2. Chart v8 Fallback für Basisdaten (Currency / Quote)
    if (shares === null || currency === null) {
      try {
        const chartUrl = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?range=1d&interval=1d`;
        const chartRes = await fetch(chartUrl, { headers, signal: AbortSignal.timeout(timeout) });
        if (chartRes.ok) {
          const chartData = await chartRes.json();
          const meta = chartData?.chart?.result?.[0]?.meta;
          if (meta?.currency && !currency) {
            currency = String(meta.currency).trim().toUpperCase();
          }
        }
      } catch (_err) {
        // Ignorieren
      }
    }

    if (shares === null && currency === null && eps === null && revenue === null) {
      return null;
    }

    return {
      ticker,
      shares_outstanding: shares,
      currency,
      eps,
      revenue,
      earnings: earningsIso,
      has_parquet: true,
      last_updated: new Date().toISOString()
    };
  } catch (_err) {
    return null;
  }
}

export async function reconcileMissingMetadata(limit = 50): Promise<number> {
  try {
    const { data, error } = await supabase
      .from("cda_master_universe")
      .select("ticker")
      .is("shares_outstanding", null)
      .eq("has_parquet", true)
      .order("ticker", { ascending: true })
      .limit(limit);

    if (error || !data || data.length === 0) return 0;

    const missing = data
      .map((r: any) => r.ticker)
      .filter((t: string) => t && !SKIP_PREFIXES.some(p => t.startsWith(p)));

    if (missing.length === 0) return 0;
    log.info(`[reconcileMissingMetadata] ${missing.length} unbefüllte Ticker gefunden. Starte Anreicherung...`);

    const batch = [];
    for (const t of missing) {
      const meta = await fetchTickerMetadata(t);
      if (meta) {
        batch.push(meta);
      }
      // Sanfte Pause zwischen Abfragen (0.35s)
      await new Promise(r => setTimeout(r, 350));
    }

    if (batch.length > 0) {
      await supabase.from("cda_master_universe").upsert(batch, { onConflict: "ticker" });
      log.info(`[reconcileMissingMetadata] ${batch.length} Ticker erfolgreich angereichert.`);
    }
    return batch.length;
  } catch (err: any) {
    log.warn(`[reconcileMissingMetadata] Fehler beim Reconcile: ${err.message}`);
    return 0;
  }
}

export function registerCdaTools(server: McpServer) {
  // Massive.com integration controls (incl. download frequency)
  registerMassiveTools(server);

  server.registerTool(
    "manage_chart_downloads",
    {
      title: "Manage Chart Downloads & OHLCV Database Status",
      description:
        "Zentrales Tool zur Überwachung, Diagnose und Steuerung der automatischen Chart-Downloads (stock-data-node).\n" +
        "DAS GESAMTE UNIVERSUM: Alle 5.500+ Aktien liegen im Parquet-Speicher und sind über die dynamische Master-Watchlist 'all' (Single Source of Truth: 'cda_master_universe') abrufbar.\n\n" +
        "ACTIONS:\n" +
        "- GET_STATUS: Liefert Queue-Größe, Service-Health und IBKR-Verbindungsstatus.\n" +
        "- STALENESS_REPORT: Liefert Altersverteilung der gesamten Parquet-Chartdatenbank.\n" +
        "- TRIGGER_SWEEP: Startet sofort einen Staleness-Sweep im Hintergrund über alle Watchlists.\n" +
        "- TICKER_STATUS: Prüft für Ticker, ob Parquet-Dateien existieren, welche Timeframes da sind und Datum der letzten Kerze.\n" +
        "- FALLBACK_CHECK: Prüft, ob Daten für Ticker bei Yahoo Finance verfügbar sind.\n" +
        "- MAPPING: Zeigt das Provider- und Symbol-Mapping eines Tickers an.\n" +
        "- TRIGGER_DOWNLOAD: Reiht Ticker mit höchster Priorität in die Download-Queue ein.\n" +
        "- SET_PROVIDER: Setzt Datenanbieter (IBKR oder YFINANCE) für einen Ticker.",
      inputSchema: {
        action: z.enum([
          "GET_STATUS",
          "STALENESS_REPORT",
          "TRIGGER_SWEEP",
          "TICKER_STATUS",
          "FALLBACK_CHECK",
          "MAPPING",
          "TRIGGER_DOWNLOAD",
          "SET_PROVIDER",
        ]).describe("Die auszuführende Aktion"),
        tickers: z.array(z.string()).optional().describe("Liste von Tickersymbolen (z. B. ['AAPL', 'MSFT'])"),
        provider: z.enum(["IBKR", "YFINANCE"]).optional().describe("Provider für SET_PROVIDER ('IBKR' oder 'YFINANCE')"),
      },
    },
    async ({ action, tickers, provider }: any) => {
      const cleanTickers = (tickers || []).map((t: string) => t.trim().toUpperCase()).filter(Boolean);

      // Timeout wrapper to prevent hangs (15 seconds)
      const fetchWithTimeout = async (url: string, options: RequestInit = {}) => {
        try {
          // AbortSignal.timeout is supported in modern Deno
          return await fetch(url, { ...options, signal: AbortSignal.timeout(15000) });
        } catch (e: any) {
          if (e.name === "TimeoutError" || e.name === "AbortError") {
            throw new Error(`Timeout (15s) bei Anfrage an den stock-data-node Backend-Service (${url})`);
          }
          throw e;
        }
      };

      try {
        switch (action) {
          case "GET_STATUS": {
            const [statusRes, healthRes, connRes] = await Promise.allSettled([
              fetchWithTimeout(`${STOCK_DATA_NODE_URL}/status`).then((r) => r.json()),
              fetchWithTimeout(`${STOCK_DATA_NODE_URL}/health`).then((r) => r.json()),
              fetchWithTimeout(`${STOCK_DATA_NODE_URL}/status/connection`).then((r) => r.json()),
            ]);

            const statusData = statusRes.status === "fulfilled" ? statusRes.value : null;
            const healthData = healthRes.status === "fulfilled" ? healthRes.value : null;
            const connData = connRes.status === "fulfilled" ? connRes.value : null;

            const lines: string[] = [
              "📥 **Download-Node & Queue Status**",
              `• **Queue-Größe:** ${statusData?.queue_size !== undefined ? statusData.queue_size.toLocaleString("de-DE") + " Ticker" : "⚠️ Nicht abrufbar"}`,
              `• **Service-Status:** ${healthData?.status === "ok" ? "🟢 OK" : "🔴 " + (healthData?.status || "offline")}`,
            ];

            if (connData) {
              const ibkrConnected = connData.ibkr_connected ?? connData.connected ?? connData.status;
              lines.push(`• **IBKR-Gateway:** ${ibkrConnected ? "🟢 Verbunden" : "⚠️ Getrennt / Fallback aktiv"}`);
            }

            return {
              content: [{ type: "text", text: lines.join("\n") }],
            };
          }

          case "STALENESS_REPORT": {
            log.info("[manage_chart_downloads] Fetching staleness report...");
            const res = await fetchWithTimeout(`${STOCK_DATA_NODE_URL}/staleness/report`);
            if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
            const data = await res.json();

            const lines: string[] = [
              "📊 **Staleness-Report (Chartdatenbank-Aktualität)**",
              "Verteilung des Datenalters der Ticker:",
            ];

            const entries = Object.entries(data);
            if (entries.length === 0) {
              lines.push("  *(Keine Daten vorhanden)*");
            } else {
              for (const [age, count] of entries) {
                const countNum = typeof count === "number" ? count : Number(count);
                lines.push(`  • \`${age}\`: **${countNum.toLocaleString("de-DE")}** Ticker`);
              }
            }

            return {
              content: [{ type: "text", text: lines.join("\n") }],
            };
          }

          case "TRIGGER_SWEEP": {
            log.info("[manage_chart_downloads] Triggering staleness sweep...");
            const res = await fetchWithTimeout(`${STOCK_DATA_NODE_URL}/trigger-staleness`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
            const data = await res.json();

            return {
              content: [{
                type: "text",
                text: `🔄 **Staleness-Sweep gestartet**\nStatus: \`${data.status || "accepted"}\` · Evaluierte Ticker: **${data.tickers_evaluated ?? "läuft im Hintergrund"}**`,
              }],
            };
          }

          case "TICKER_STATUS": {
            if (cleanTickers.length === 0) {
              return { content: [{ type: "text", text: "❌ Fehler: Mindestens ein Ticker erforderlich." }], isError: true };
            }

            const res = await fetchWithTimeout(`${STOCK_DATA_NODE_URL}/data/status-batch`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ tickers: cleanTickers }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
            const payload = await res.json();
            
            const lines: string[] = [`📈 **Chartdaten-Status für ${cleanTickers.length} Ticker:**`];
            for (const item of payload.results || []) {
              if (item.status === "error") {
                lines.push(`• **${item.ticker}**: ❌ Fehler: ${item.error}`);
              } else {
                const data = item.data;
                if (!data.folder_exists) {
                  lines.push(`• **${item.ticker}**: ⚠️ Kein lokaler Parquet-Ordner vorhanden`);
                } else {
                  const tfs = data.timeframes || {};
                  const tfDetails: string[] = [];
                  for (const [tf, info] of Object.entries<any>(tfs)) {
                    if (info.has_data) {
                      tfDetails.push(`${tf}: Letzte Kerze ${info.last_candle_date || "Datum unbekannt"}`);
                    } else {
                      tfDetails.push(`${tf}: Keine Daten`);
                    }
                  }
                  lines.push(`• **${item.ticker}**: 🟢 Vorhanden (${tfDetails.length > 0 ? tfDetails.join(" | ") : "keine Timeframes"})`);
                }
              }
            }
            return { content: [{ type: "text", text: lines.join("\n") }] };
          }

          case "FALLBACK_CHECK": {
            if (cleanTickers.length === 0) {
              return { content: [{ type: "text", text: "❌ Fehler: Mindestens ein Ticker erforderlich." }], isError: true };
            }

            const res = await fetchWithTimeout(`${STOCK_DATA_NODE_URL}/fallback/check-batch`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ tickers: cleanTickers }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
            const payload = await res.json();
            
            const lines: string[] = [`🔍 **Yahoo Finance Fallback-Verfügbarkeit:**`];
            for (const item of payload.results || []) {
              if (item.status === "error") {
                lines.push(`• **${item.ticker}**: ❌ Fehler: ${item.error}`);
              } else {
                const data = item.data;
                if (data.yfinance_available) {
                  lines.push(`• **${item.ticker}**: 🟢 Verfügbar bei Yahoo Finance (Symbol: \`${data.yf_ticker || item.ticker}\`)`);
                } else {
                  lines.push(`• **${item.ticker}**: 🔴 Nicht verfügbar bei Yahoo Finance`);
                }
              }
            }
            return { content: [{ type: "text", text: lines.join("\n") }] };
          }

          case "MAPPING": {
            if (cleanTickers.length === 0) {
              return { content: [{ type: "text", text: "❌ Fehler: Mindestens ein Ticker erforderlich." }], isError: true };
            }

            const res = await fetchWithTimeout(`${STOCK_DATA_NODE_URL}/mapping-batch`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ tickers: cleanTickers }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
            const payload = await res.json();
            
            const lines: string[] = [`🗺️ **Provider- und Symbol-Mappings:**`];
            for (const item of payload.results || []) {
              if (item.status === "error") {
                lines.push(`• **${item.ticker}**: ❌ Fehler: ${item.error}`);
              } else {
                const data = item.data;
                lines.push(
                  `• **${item.ticker}**: Provider: \`${data.provider || "IBKR"}\` | IBKR-Symbol: \`${data.ibkr_symbol || "-"}\` (Exch: \`${data.ibkr_exchange || "-"}\`, Curr: \`${data.ibkr_currency || "-"}\`) | Provider-Aliases: ${JSON.stringify(data.provider_symbols || {})}`
                );
              }
            }
            return { content: [{ type: "text", text: lines.join("\n") }] };
          }

          case "TRIGGER_DOWNLOAD": {
            if (cleanTickers.length === 0) {
              return { content: [{ type: "text", text: "❌ Fehler: Mindestens ein Ticker erforderlich." }], isError: true };
            }

            const res = await fetchWithTimeout(`${STOCK_DATA_NODE_URL}/download-batch`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ tickers: cleanTickers }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
            const payload = await res.json();
            
            const lines: string[] = [`⚡ **Priority-Downloads angestoßen:**`];
            for (const item of payload.results || []) {
              if (item.status === "error") {
                lines.push(`• **${item.ticker}**: ❌ Fehler: ${item.error}`);
              } else {
                const data = item.data;
                lines.push(`• **${item.ticker}**: ✅ ${data.message || data.status || "Eingereiht"}`);
              }
            }
            return { content: [{ type: "text", text: lines.join("\n") }] };
          }

          case "SET_PROVIDER": {
            if (cleanTickers.length === 0 || !provider) {
              return { content: [{ type: "text", text: "❌ Fehler: Ticker ('tickers') und 'provider' ('IBKR' oder 'YFINANCE') erforderlich." }], isError: true };
            }

            const res = await fetchWithTimeout(`${STOCK_DATA_NODE_URL}/config/provider-batch`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ tickers: cleanTickers, provider }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
            const payload = await res.json();
            
            const lines: string[] = [`⚙️ **Provider-Konfiguration geändert:**`];
            for (const item of payload.results || []) {
              if (item.status === "error") {
                lines.push(`• **${item.ticker}**: ❌ Fehler: ${item.error}`);
              } else {
                const data = item.data;
                lines.push(`• **${item.ticker}**: ✅ Provider auf \`${provider}\` gesetzt. (Alte Daten ${data.data_deleted ? "gelöscht" : "beibehalten/nicht vorhanden"})`);
              }
            }
            return { content: [{ type: "text", text: lines.join("\n") }] };
          }

          default:
            return {
              content: [{ type: "text", text: `❌ Unbekannte Action: ${action}` }],
              isError: true,
            };
        }
      } catch (err: any) {
        log.error(`manage_chart_downloads failed: ${err.message}`);
        return {
          content: [{ type: "text", text: `❌ Interner Fehler: ${err.message}` }],
          isError: true,
        };
      }
    }
  );

  // ── add_ticker ──────────────────────────────────────────────────
  server.registerTool(
    "add_ticker",
    {
      title: "Add Ticker",
      description:
        "Adds one or more stock tickers to the local database and queues them for priority download. " +
        "Automatically resolves unknown ticker symbols across data providers according to provider ranking " +
        "(e.g. IBKR -> YFinance fallback, resolving '4GLD' to '4GLD.DE'). " +
        "The first ticker in the list receives highest download priority. " +
        "Returns 'OK' for direct matches, the resolved symbol and provider if an alias was required, " +
        "or a candidate list if ambiguous.",
      inputSchema: {
        tickers: z.array(z.string()).min(1).describe(
          "List of ticker symbols to add (e.g. ['AAPL'] or ['CBRS', 'HL', 'SPCX', 'TYC1', 'SIVE'])"
        ),
        provider: z.string().optional().describe(
          "Optional: Force a specific provider (e.g. 'IBKR' or 'YFINANCE'). Omit to use automatic provider ranking."
        ),
        override_symbol: z.string().optional().describe(
          "Optional: Explicit provider symbol (e.g. '4GLD.DE'). Only valid when adding a single ticker."
        ),
        sec_type: z.enum(["STK", "ETF", "OPT", "FUT"]).default("STK").optional().describe(
          "Security type to search for (default: 'STK')."
        ),
      },
    },
    async ({ tickers, provider, override_symbol, sec_type }: any) => {
      try {
        const cleanTickers = tickers.map((t: string) => t.trim().toUpperCase()).filter(Boolean);
        if (cleanTickers.length === 0) {
          return { content: [{ type: "text", text: "Error: No valid ticker provided." }], isError: true };
        }

        if (override_symbol && cleanTickers.length > 1) {
          return { content: [{ type: "text", text: "Error: override_symbol can only be used with a single ticker." }], isError: true };
        }

        log.info(`[add_ticker] Requesting add for: ${cleanTickers.join(", ")} (sec_type=${sec_type || "STK"})`);
        const payload: Record<string, any> = {
          tickers: cleanTickers,
          sec_type: sec_type || "STK",
        };
        if (provider) payload.provider = provider.toUpperCase();
        if (override_symbol) payload.override_symbol = override_symbol.trim();

        const res = await fetch(`${STOCK_DATA_NODE_URL}/add`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          signal: AbortSignal.timeout(30000) // 30s timeout because resolution can take time
        });

        if (!res.ok) {
          const errText = await res.text();
          return { content: [{ type: "text", text: `Backend error (HTTP ${res.status}): ${errText}` }], isError: true };
        }

        const data = await res.json();
        const results: any[] = data.results || [];
        const lines: string[] = [];
        let hasAmbiguous = false;
        let hasErrors = false;

        for (const item of results) {
          if (item.status === "ok") {
            lines.push(`✅ ${item.ticker}: OK (${item.provider || "IBKR"}) — Priority 1 download queued`);
          } else if (item.status === "resolved") {
            lines.push(`ℹ️ ${item.ticker}: Resolved to '${item.resolved_symbol}' via ${item.provider} (${item.sec_type || "STK"}) — Download queued`);
          } else if (item.status === "ambiguous") {
            hasAmbiguous = true;
            const candidatesStr = (item.candidates || [])
              .map((c: any, idx: number) => `   ${idx + 1}. Symbol: ${c.symbol} | Name: ${c.name} | Type: ${c.sec_type} | Exch: ${c.exchange}`)
              .join("\n");
            lines.push(`⚠️ ${item.ticker}: Ambiguous ticker — multiple matches found:\n${candidatesStr}\n   -> Please call add_ticker again with override_symbol to specify.`);
          } else {
            hasErrors = true;
            lines.push(`❌ ${item.ticker}: Failed (${item.error || "Could not resolve on any provider"})`);
          }
        }

        const successfulTickers = results
          .filter((item: any) => item.status === "ok" || item.status === "resolved")
          .map((item: any) => item.ticker);

        if (successfulTickers.length > 0) {
          try {
            const rows: any[] = [];
            for (const t of successfulTickers) {
              const meta = await fetchTickerMetadata(t);
              if (meta) {
                rows.push(meta);
                const sStr = meta.shares_outstanding ? meta.shares_outstanding.toLocaleString("de-DE") : "N/A";
                lines.push(`📊 ${t}: Metadaten automatisch ergänzt (Shares: ${sStr}, Währung: ${meta.currency || "N/A"})`);
              } else {
                rows.push({
                  ticker: t,
                  shares_outstanding: null,
                  currency: null,
                  eps: null,
                  revenue: null,
                  earnings: null,
                  has_parquet: true,
                  last_updated: new Date().toISOString(),
                });
                lines.push(`ℹ️ ${t}: In Master Universe registriert (Fundamentaldaten stehen noch aus).`);
              }
            }
            await supabase.from("cda_master_universe").upsert(rows, { onConflict: "ticker", ignoreDuplicates: false });
            log.info(`[add_ticker] Synced and enriched ${successfulTickers.join(", ")} into cda_master_universe.`);
          } catch (upsertErr: any) {
            log.warn(`[add_ticker] Warning: could not upsert into cda_master_universe: ${upsertErr.message}`);
          }
        }

        return {
          content: [{ type: "text", text: lines.join("\n") }],
          isError: hasErrors && !lines.some(l => l.startsWith("✅") || l.startsWith("ℹ️")),
        };
      } catch (err: any) {
        log.error(`add_ticker failed: ${err.message}`);
        return { content: [{ type: "text", text: `Internal Error connecting to stock-data-node: ${err.message}` }], isError: true };
      }
    }
  );

  // ── override_ticker_mapping ──────────────────────────────────────────
  server.registerTool(
    "override_ticker_mapping",
    {
      title: "Override Ticker Mapping",
      description: "Directly override or correct the provider and symbol mapping for a ticker without immediately adding new download tasks. " +
        "Example: ticker='4GLD', provider='YFINANCE', symbol='4GLD.DE'.",
      inputSchema: {
        ticker: z.string().describe("The base ticker symbol (e.g. '4GLD')"),
        provider: z.string().describe("The provider to override (e.g. 'YFINANCE' or 'IBKR')"),
        symbol: z.string().describe("The exact symbol string expected by the provider (e.g. '4GLD.DE')"),
      },
    },
    async ({ ticker, provider, symbol }: any) => {
      try {
        log.info(`Overriding mapping for ${ticker} on ${provider} to ${symbol}...`);
        const res = await fetch(`${STOCK_DATA_NODE_URL}/mapping/${encodeURIComponent(ticker)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider, symbol }),
          signal: AbortSignal.timeout(15000)
        });
        
        if (!res.ok) {
          return { content: [{ type: "text", text: `Error: API returned status ${res.status}` }], isError: true };
        }

        const data = await res.json();
        return { content: [{ type: "text", text: `Successfully overridden mapping for ${ticker}.\nNew Provider Symbols: ${JSON.stringify(data.provider_symbols, null, 2)}` }] };
      } catch (err: any) {
        log.error(`override_ticker_mapping failed: ${err.message}`);
        return { content: [{ type: "text", text: `Internal Error: ${err.message}` }], isError: true };
      }
    }
  );

  server.registerTool(
    "manage_ticker_metadata",
    {
      title: "Manage Ticker Metadata & Master Universe (cda_master_universe)",
      description:
        "Verwaltet fundamentale und systemische Metadaten zu Aktien im zentralen Master Universe (`cda_master_universe` in Supabase).\n\n" +
        "DAS MASTER UNIVERSE:\n" +
        "- Enthält alle Aktien des Systems (5.500+ Ticker aus stock-data-node).\n" +
        "- Speichert Fundamentaldaten und System-Flags:\n" +
        "  * `shares_outstanding`: Ausstehende Aktien (mit aktuellem Kurs multipliziert ergibt das die Marktkapitalisierung / Market Capitalisation).\n" +
        "  * `currency`: Handelswährung (z.B. USD, EUR, JPY).\n" +
        "  * `eps`: Earnings per Share.\n" +
        "  * `revenue`: Umsatz.\n" +
        "  * `earnings`: Datum der nächsten/letzten Earnings (UTC).\n" +
        "  * `has_parquet`: Gibt an, ob lokale Parquet-Chartdaten vorliegen.\n\n" +
        "AKTIONEN:\n" +
        "- GET: Fragt Metadaten für einen oder mehrere Ticker ab (z.B. ticker: 'AAPL' oder 'AAPL,MSFT').\n" +
        "- UPDATE: Aktualisiert/ergänzt fundamentale Metadaten für einen Ticker.\n" +
        "- COUNT: Liefert Gesamtstatistik des Master Universe (Gesamtanzahl Ticker, mit Parquet, mit Marktkapitalisierung, mit EPS/Umsatz).\n" +
        "- SYNC: Synchronisiert eine Liste von Tickern (oder alle lokalen Ticker) als 'has_parquet=true' in die cda_master_universe Tabelle.",
      inputSchema: {
        action: z.enum(["GET", "UPDATE", "COUNT", "SYNC", "RECONCILE"]).describe("Die auszuführende Aktion: 'GET', 'UPDATE', 'COUNT', 'SYNC', oder 'RECONCILE'"),
        ticker: z.string().optional().describe("Einzelner Ticker (z.B. AAPL) oder kommaseparierte Liste (AAPL,MSFT) bei GET/UPDATE."),
        tickers: z.array(z.string()).optional().describe("Array von Tickersymbolen für SYNC (Bulk-Upsert)."),
        utime: z.string().optional().describe("ISO-8601 Zeitstempel. Gültigkeitsbeginn der Daten. Bei Leer wird now() genutzt."),
        currency: z.string().optional().describe("Währung (z.B. USD, EUR)"),
        shares_outstanding: z.number().optional().describe("Ausstehende Aktien (Shares Outstanding) für Marktkapitalisierung"),
        eps: z.number().optional().describe("Earnings per share"),
        revenue: z.number().optional().describe("Umsatz"),
        earnings: z.string().optional().describe("Datum der nächsten/letzten Earnings (UTC Zeitstempel)"),
        has_parquet: z.boolean().optional().describe("Gibt an, ob lokale Chartdaten vorliegen."),
        chunk_size: z.number().optional().describe("Batch-Größe für Bulk-Sync / Reconcile (Standard: 50 oder UNIVERSE_SYNC_CHUNK_SIZE)"),
      },
    },
    async ({ action, ticker, tickers, utime, currency, shares_outstanding, eps, revenue, earnings, has_parquet, chunk_size }: any) => {
      try {
        if (action === "RECONCILE") {
          const limitCount = chunk_size && chunk_size > 0 ? chunk_size : 50;
          log.info(`[manage_ticker_metadata] RECONCILE requested (limit: ${limitCount})`);
          const count = await reconcileMissingMetadata(limitCount);
          return {
            content: [{
              type: "text",
              text: `✅ Reconciler abgeschlossen: ${count} Ticker ohne Metadaten erfolgreich geprüft und angereichert.`,
            }],
          };
        }

        if (action === "COUNT") {
          log.info("[manage_ticker_metadata] COUNT requested");
          const [totalRes, parquetRes, sharesRes, epsRes, revRes] = await Promise.all([
            supabase.from("cda_master_universe").select("*", { count: "exact", head: true }),
            supabase.from("cda_master_universe").select("*", { count: "exact", head: true }).eq("has_parquet", true),
            supabase.from("cda_master_universe").select("*", { count: "exact", head: true }).not("shares_outstanding", "is", null),
            supabase.from("cda_master_universe").select("*", { count: "exact", head: true }).not("eps", "is", null),
            supabase.from("cda_master_universe").select("*", { count: "exact", head: true }).not("revenue", "is", null),
          ]);

          const stats = {
            total_tickers: totalRes.count ?? 0,
            has_parquet: parquetRes.count ?? 0,
            has_market_cap_shares: sharesRes.count ?? 0,
            has_eps: epsRes.count ?? 0,
            has_revenue: revRes.count ?? 0,
          };

          return {
            content: [{
              type: "text",
              text: `Master Universe (cda_master_universe) Statistik:\n` +
                `• Gesamtzahl Ticker: ${stats.total_tickers}\n` +
                `• Mit lokalen Parquet-Daten (has_parquet): ${stats.has_parquet}\n` +
                `• Mit Marktkapitalisierung / Shares Outstanding: ${stats.has_market_cap_shares}\n` +
                `• Mit EPS: ${stats.has_eps}\n` +
                `• Mit Umsatz: ${stats.has_revenue}\n\n` +
                `JSON: ${JSON.stringify(stats, null, 2)}`,
            }],
          };
        }

        if (action === "SYNC") {
          let syncList: string[] = [];
          if (tickers && Array.isArray(tickers) && tickers.length > 0) {
            syncList = tickers.map((t: string) => String(t).trim().toUpperCase()).filter(Boolean);
          } else if (ticker) {
            syncList = ticker.split(",").map((t: string) => t.trim().toUpperCase()).filter(Boolean);
          } else {
            // Fetch list from stock-data-node /staleness/report or watchlists
            try {
              const repRes = await fetch(`${STOCK_DATA_NODE_URL}/staleness/report`, { signal: AbortSignal.timeout(15000) });
              if (repRes.ok) {
                const repData = await repRes.json();
                if (Array.isArray(repData.tickers)) {
                  syncList = repData.tickers;
                } else if (repData.distribution && typeof repData.distribution === "object") {
                  // If distribution returned, we may not have full list directly
                }
              }
            } catch (fetchErr: any) {
              log.warn(`[manage_ticker_metadata] Could not fetch tickers from stock-data-node: ${fetchErr.message}`);
            }

            if (!syncList || syncList.length === 0) {
              const { data: wlData } = await supabase
                .from("pca_watchlists")
                .select("ticker")
                .eq("list_name", "all");
              if (wlData && wlData.length > 0) {
                syncList = wlData.map((r: any) => r.ticker);
                log.info(`[manage_ticker_metadata] SYNC: ${syncList.length} Ticker aus pca_watchlists('all') geladen.`);
              }
            }
          }

          if (!syncList || syncList.length === 0) {
            throw new Error("Keine Ticker für SYNC angegeben und automatische Erkennung lieferte keine Ticker. Bitte 'tickers' Array übergeben.");
          }

          const defaultChunk = Number(Deno.env.get("UNIVERSE_SYNC_CHUNK_SIZE") || 500);
          const effectiveChunk = chunk_size && chunk_size > 0 ? chunk_size : defaultChunk;
          let syncedCount = 0;

          log.info(`[manage_ticker_metadata] SYNC: Synchronisiere ${syncList.length} Ticker in Batches von ${effectiveChunk}...`);
          for (let i = 0; i < syncList.length; i += effectiveChunk) {
            const currentSlice = syncList.slice(i, i + effectiveChunk);
            const batch: any[] = [];
            for (const t of currentSlice) {
              const meta = await fetchTickerMetadata(t);
              if (meta) {
                batch.push(meta);
              } else {
                batch.push({
                  ticker: t,
                  shares_outstanding: null,
                  currency: null,
                  eps: null,
                  revenue: null,
                  earnings: null,
                  has_parquet: has_parquet !== undefined ? has_parquet : true,
                  last_updated: new Date().toISOString(),
                });
              }
            }

            const { error } = await supabase
              .from("cda_master_universe")
              .upsert(batch, { onConflict: "ticker", ignoreDuplicates: false });

            if (error) {
              log.error(`[manage_ticker_metadata] Batch ${i / effectiveChunk + 1} fehlgeschlagen: ${error.message}`);
              throw error;
            }
            syncedCount += batch.length;
          }

          return {
            content: [{
              type: "text",
              text: `✅ Erfolgreich ${syncedCount} Ticker in cda_master_universe synchronisiert (has_parquet=${has_parquet ?? true}).`,
            }],
          };
        }

        const tickersList = (ticker || "").split(",").map((t: string) => t.trim().toUpperCase()).filter(Boolean);

        if (action === "GET") {
          if (tickersList.length === 0) {
            throw new Error("Für GET ist mindestens ein Ticker erforderlich (z.B. ticker: 'AAPL'). Für Gesamtstatistiken nutze action: 'COUNT'.");
          }
          log.info(`[manage_ticker_metadata] GET for tickers: ${tickersList.join(", ")}`);
          const { data, error } = await supabase
            .from("cda_master_universe")
            .select("*, stock_documents(id, doc_type, title, fiscal_year, fiscal_quarter, report_date, relative_path, distillate_relative_path)")
            .in("ticker", tickersList);
          
          if (error) throw new Error(error.message);
          return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
        } else if (action === "UPDATE") {
          if (tickersList.length === 0) {
            throw new Error("Für UPDATE ist der Parameter 'ticker' erforderlich.");
          }
          log.info(`[manage_ticker_metadata] UPDATE for ticker: ${tickersList[0]}`);
          if (tickersList.length > 1) {
            throw new Error("UPDATE unterstützt nur einen einzelnen Ticker zur selben Zeit.");
          }
          const t = tickersList[0];
          
          const { data: existingData } = await supabase.from("cda_master_universe").select("*").eq("ticker", t).maybeSingle();
          
          const payload: any = { ticker: t };
          if (utime !== undefined) payload.utime = utime;
          payload.currency = currency !== undefined ? currency : existingData?.currency;
          payload.shares_outstanding = shares_outstanding !== undefined ? shares_outstanding : existingData?.shares_outstanding;
          payload.eps = eps !== undefined ? eps : existingData?.eps;
          payload.revenue = revenue !== undefined ? revenue : existingData?.revenue;
          payload.earnings = earnings !== undefined ? earnings : existingData?.earnings;
          payload.has_parquet = has_parquet !== undefined ? has_parquet : (existingData?.has_parquet ?? true);
          payload.last_updated = new Date().toISOString();
          
          const { error } = await supabase.from("cda_master_universe").upsert(payload, { onConflict: "ticker" });
          if (error) throw new Error(error.message);
          
          return { content: [{ type: "text", text: `Erfolgreich Metadaten für ${t} aktualisiert.` }] };
        }
        
        return { content: [{ type: "text", text: "Unbekannte Aktion" }], isError: true };
      } catch (err: any) {
        log.error(`manage_ticker_metadata failed: ${err.message}`);
        return { content: [{ type: "text", text: `Internal Error: ${err.message}` }], isError: true };
      }
    }
  );

  // ── 5. manage_stock_documents ─────────────────────────────────────────
  server.registerTool(
    "manage_stock_documents",
    {
      title: "Manage Stock Documents & Distillates",
      description:
        "Verwaltet Finanzdokumente (Earnings Reports, SEC Filings 10-K/10-Q/8-K, Präsentationen, Transkripte) und deren strukturierte Markdown-Destillate (.distillate.md) im Ticker-Ordner von stock-data-node.\n\n" +
        "DAS DOKUMENTEN- & DESTILLAT-SYSTEM:\n" +
        "- Ticker-basierte Ablage: Jedes Dokument liegt unter stock-data-node/data/documents/{TICKER}/.\n" +
        "- 'Destillat-First': Ein hochverdichtetes Markdown-Dokument fasst Schlüsselzahlen, Segmentdaten, Management-Aussagen und Guidance in sauberen Tabellen zusammen. Passt vollständig in das LLM-Context-Window ohne RAG-Chunking-Verluste.\n" +
        "- Postgres GIN-Volltextsuche: Schnelle Volltextsuche über Titel, Dokumenttyp und den gesamten Inhalt aller Destillate.\n" +
        "- Speicherortunabhängig: In der DB werden nur relative Pfade gespeichert; der Basisordner ist über DOCUMENTS_BASE_PATH konfigurierbar.\n\n" +
        "AKTIONEN:\n" +
        "- GET: Ruft Dokument-Metadaten, aufgelösten Dateipfad und das vollständige Markdown-Destillat ab (z.B. ticker: 'DELL' oder document_id: '...').\n" +
        "- SEARCH: Durchsucht alle Dokumente und Destillate per Volltextsuche (z.B. query: 'AI server shipments backlog' oder 'cloud margin guidance'). Unterstützt Filter nach ticker, doc_type und fiscal_year.\n" +
        "- LIST: Listet alle vorhandenen Dokumente eines Tickers chronologisch mit Typ, Datum, Dateigröße und Destillat-Status auf.\n" +
        "- SCAN_FOLDER: Durchsucht den Ticker-Ordner nach neuen, noch nicht in der Datenbank registrierten Dateien (halbautomatischer Abgleich).\n" +
        "- ADD: Registriert ein Dokument im Ticker-Ordner, berechnet die SHA-256 Checksumme und hinterlegt optional das Destillat.\n" +
        "- CREATE_DISTILLATE: Erzeugt oder aktualisiert das Markdown-Destillat für ein registriertes Dokument.\n" +
        "- DELETE: Löscht die Dokumentverknüpfung aus der Datenbank.\n\n" +
        "WANN ZU NUTZEN: Immer wenn nach Unternehmensberichten, Quartalszahlen, Jahresabschlüssen, SEC Filings oder detaillierten Management-Aussagen/Guidance gefragt wird.",
      inputSchema: {
        action: z.enum(["ADD", "SCAN_FOLDER", "CREATE_DISTILLATE", "SEARCH", "GET", "LIST", "DELETE"]).describe("Die auszuführende Aktion: 'GET' (Abruf), 'SEARCH' (Volltextsuche), 'LIST' (Übersicht), 'SCAN_FOLDER' (Ordner-Scan), 'ADD' (Registrieren), 'CREATE_DISTILLATE' (Destillat anlegen), 'DELETE' (Löschen)."),
        ticker: z.string().optional().describe("Aktien-Ticker (z.B. 'DELL', 'AAPL', 'NVDA'). Pflichtfeld bei ADD und SCAN_FOLDER, optionaler Filter bei SEARCH, GET und LIST."),
        document_id: z.string().optional().describe("UUID des Dokuments in der Tabelle stock_documents (für GET, DELETE, CREATE_DISTILLATE)."),
        file_path: z.string().optional().describe("Dateiname im Ticker-Ordner (z.B. 'DELL_2025_Q4_Earnings_Review.pdf') oder relativer/absoluter Quellpfad für ADD."),
        doc_type: z.string().optional().describe("Dokumenttyp: '10-K', '10-Q', '8-K', 'earnings_release', 'presentation', 'transcript', 'other'."),
        title: z.string().optional().describe("Titel des Berichts (z.B. '4Q FY25 Performance Review & Full Year Results')."),
        fiscal_year: z.number().optional().describe("Geschäftsjahr des Berichts (z.B. 2025)."),
        fiscal_quarter: z.string().optional().describe("Quartal des Berichts ('Q1', 'Q2', 'Q3', 'Q4', 'FY')."),
        report_date: z.string().optional().describe("Veröffentlichungsdatum im Format YYYY-MM-DD."),
        query: z.string().optional().describe("Suchbegriff für PostgreSQL GIN-Volltextsuche im Destillat und Titel (z.B. 'AI backlog', 'cloud margin')."),
        distillate_content: z.string().optional().describe("Vollständiger Markdown-Text für das Destillat (.distillate.md) mit Kennzahlen-Tabellen und Zusammenfassung."),
        create_distillate: z.boolean().optional().describe("Gibt an, ob bei ADD automatisch ein Destillat erzeugt/angelegt werden soll."),
        limit: z.number().optional().describe("Maximale Anzahl an Rückgabe-Ergebnissen (Standard: 20 oder DOCUMENTS_SEARCH_LIMIT)."),
      },
    },
    async (args: any) => {
      const {
        action,
        ticker,
        document_id,
        file_path,
        doc_type,
        title,
        fiscal_year,
        fiscal_quarter,
        report_date,
        query,
        distillate_content,
        create_distillate,
        limit
      } = args || {};
      try {
        const getBasePath = (): string => {
          const envPath = Deno.env.get("DOCUMENTS_BASE_PATH");
          if (envPath && envPath.trim()) return envPath.trim();
          try {
            Deno.statSync("/data/documents");
            return "/data/documents";
          } catch (_e) {
            return "/home/daniel/stock-data-node/data/documents";
          }
        };
        const resolveAbsPath = (relPath: string): string => {
          const base = getBasePath().replace(/\/+$/, "");
          const cleanRel = relPath.replace(/^\/+/, "");
          return `${base}/${cleanRel}`;
        };
        const computeSha256 = async (bytes: Uint8Array): Promise<string> => {
          const hashBuf = await crypto.subtle.digest("SHA-256", bytes as any);
          return Array.from(new Uint8Array(hashBuf)).map(b => b.toString(16).padStart(2, "0")).join("");
        };

        const defaultSearchLimit = Number(Deno.env.get("DOCUMENTS_SEARCH_LIMIT") || 20);
        const effectiveLimit = limit && limit > 0 ? limit : defaultSearchLimit;

        // ── AKTION: ADD ──
        if (action === "ADD") {
          if (!ticker || !ticker.trim()) throw new Error("Parameter 'ticker' ist für ADD erforderlich.");
          if (!file_path || !file_path.trim()) throw new Error("Parameter 'file_path' ist für ADD erforderlich.");

          const t = ticker.trim().toUpperCase();
          const basePath = getBasePath();
          const tickerDir = `${basePath}/${t}`;
          try {
            await Deno.mkdir(tickerDir, { recursive: true });
          } catch (_e) {
            // Ignorieren falls bereits existiert
          }

          const srcPath = file_path.trim();
          const fileName = srcPath.split("/").pop() || `${t}_doc`;
          const extParts = fileName.split(".");
          const fileExt = extParts.length > 1 ? extParts.pop()!.toLowerCase() : "";
          const baseName = extParts.join(".");
          const destPath = `${tickerDir}/${fileName}`;

          let fileBytes: Uint8Array;
          try {
            fileBytes = await Deno.readFile(destPath);
          } catch (_e) {
            try {
              fileBytes = await Deno.readFile(srcPath);
              if (srcPath !== destPath) {
                try { await Deno.writeFile(destPath, fileBytes); } catch (_w) {}
              }
            } catch (readErr: any) {
              throw new Error(`Datei '${fileName}' konnte nicht eingelesen werden: ${readErr.message}`);
            }
          }

          const sha256 = await computeSha256(fileBytes);

          const relativePath = `${t}/${fileName}`;
          let distRelPath: string | null = null;
          let distContent: string | null = distillate_content || null;

          // Falls Destillat übergeben oder generiert werden soll
          if (distContent) {
            const distFileName = `${baseName}.distillate.md`;
            const distDestPath = `${tickerDir}/${distFileName}`;
            await Deno.writeTextFile(distDestPath, distContent);
            distRelPath = `${t}/${distFileName}`;
          }

          const payload: any = {
            ticker: t,
            doc_type: doc_type || "other",
            title: title || fileName.replace(/_/g, " "),
            fiscal_year: fiscal_year || null,
            fiscal_quarter: fiscal_quarter || null,
            report_date: report_date || null,
            relative_path: relativePath,
            file_name: fileName,
            file_extension: fileExt,
            file_size_bytes: fileBytes.length,
            sha256_hash: sha256,
            distillate_relative_path: distRelPath,
            distillate_content: distContent,
            distillate_generated_at: distContent ? new Date().toISOString() : null,
          };

          const { data, error } = await supabase
            .from("stock_documents")
            .insert(payload)
            .select("*")
            .single();

          if (error) throw new Error(error.message);

          const result = {
            ...data,
            resolved_absolute_path: resolveAbsPath(data.relative_path),
            resolved_distillate_path: data.distillate_relative_path ? resolveAbsPath(data.distillate_relative_path) : null,
          };

          return {
            content: [{
              type: "text",
              text: `✅ Dokument erfolgreich für ${t} registriert!\nID: ${data.id}\nPfad: ${result.resolved_absolute_path}\nDestillat: ${result.resolved_distillate_path || "Keines"}\nDetails: ${JSON.stringify(result, null, 2)}`
            }]
          };
        }

        // ── AKTION: SCAN_FOLDER ──
        if (action === "SCAN_FOLDER") {
          if (!ticker || !ticker.trim()) throw new Error("Parameter 'ticker' ist für SCAN_FOLDER erforderlich.");
          const t = ticker.trim().toUpperCase();
          const basePath = getBasePath();
          const tickerDir = `${basePath}/${t}`;

          let filesOnDisk: string[] = [];
          try {
            for await (const entry of Deno.readDir(tickerDir)) {
              if (entry.isFile && !entry.name.endsWith(".distillate.md")) {
                filesOnDisk.push(entry.name);
              }
            }
          } catch (_e) {
            filesOnDisk = [];
          }

          const { data: registeredDocs } = await supabase
            .from("stock_documents")
            .select("file_name, id, doc_type, report_date")
            .eq("ticker", t);

          const registeredSet = new Set((registeredDocs || []).map((d: any) => d.file_name));
          const unregisteredFiles = filesOnDisk.filter(f => !registeredSet.has(f));

          return {
            content: [{
              type: "text",
              text: `Ordner-Scan für ${t} (${tickerDir}):\n` +
                    `• Dateien im Dateisystem: ${filesOnDisk.length}\n` +
                    `• Registrierte Dokumente in DB: ${(registeredDocs || []).length}\n` +
                    `• Nicht registrierte Dateien: ${unregisteredFiles.length}\n\n` +
                    JSON.stringify({
                      ticker: t,
                      folder: tickerDir,
                      unregistered_files: unregisteredFiles,
                      registered_documents: registeredDocs
                    }, null, 2)
            }]
          };
        }

        // ── AKTION: CREATE_DISTILLATE ──
        if (action === "CREATE_DISTILLATE") {
          if (!document_id && (!ticker || !file_path)) {
            throw new Error("Für CREATE_DISTILLATE ist entweder 'document_id' oder 'ticker' + 'file_path' erforderlich.");
          }

          let docRecord: any = null;
          if (document_id) {
            const { data, error } = await supabase.from("stock_documents").select("*").eq("id", document_id).single();
            if (error) throw new Error(`Dokument nicht gefunden: ${error.message}`);
            docRecord = data;
          } else {
            const t = ticker!.trim().toUpperCase();
            const fileName = file_path!.split("/").pop();
            const { data, error } = await supabase.from("stock_documents").select("*").eq("ticker", t).eq("file_name", fileName).single();
            if (error) throw new Error(`Dokument nicht gefunden: ${error.message}`);
            docRecord = data;
          }

          const t = docRecord.ticker;
          const absPath = resolveAbsPath(docRecord.relative_path);
          const basePath = getBasePath();
          const tickerDir = `${basePath}/${t}`;

          let distContent = distillate_content;
          if (!distContent) {
            try {
              distContent = await Deno.readTextFile(absPath);
            } catch (_e) {
              distContent = `# ${t} Destillat: ${docRecord.title}\n\nAutomatisches Destillat erstellt am ${new Date().toISOString()}.\nOriginal-Datei: ${docRecord.file_name}`;
            }
          }

          const baseName = docRecord.file_name.replace(/\.[^/.]+$/, "");
          const distFileName = `${baseName}.distillate.md`;
          const distDestPath = `${tickerDir}/${distFileName}`;
          await Deno.writeTextFile(distDestPath, distContent);

          const distRelPath = `${t}/${distFileName}`;
          const { data: updated, error: updateErr } = await supabase
            .from("stock_documents")
            .update({
              distillate_relative_path: distRelPath,
              distillate_content: distContent,
              distillate_generated_at: new Date().toISOString(),
              updated_at: new Date().toISOString()
            })
            .eq("id", docRecord.id)
            .select("*")
            .single();

          if (updateErr) throw new Error(updateErr.message);

          return {
            content: [{
              type: "text",
              text: `✅ Destillat für Dokument ${docRecord.id} (${t}) erstellt!\nPfad: ${distDestPath}\nZeichen: ${distContent.length}`
            }]
          };
        }

        // ── AKTION: GET ──
        if (action === "GET") {
          if (!document_id && !ticker) {
            throw new Error("Für GET ist mindestens 'document_id' oder 'ticker' erforderlich.");
          }

          let queryBuilder = supabase.from("stock_documents").select("*");
          if (document_id) {
            queryBuilder = queryBuilder.eq("id", document_id);
          } else if (ticker) {
            queryBuilder = queryBuilder.eq("ticker", ticker.trim().toUpperCase());
          }

          const { data, error } = await queryBuilder.limit(effectiveLimit);
          if (error) throw new Error(error.message);

          const enriched = (data || []).map((doc: any) => ({
            ...doc,
            resolved_absolute_path: resolveAbsPath(doc.relative_path),
            resolved_distillate_path: doc.distillate_relative_path ? resolveAbsPath(doc.distillate_relative_path) : null,
          }));

          return {
            content: [{ type: "text", text: JSON.stringify(enriched, null, 2) }]
          };
        }

        // ── AKTION: SEARCH ──
        if (action === "SEARCH") {
          let qb = supabase.from("stock_documents").select("*");

          if (ticker) qb = qb.eq("ticker", ticker.trim().toUpperCase());
          if (doc_type) qb = qb.eq("doc_type", doc_type.trim());
          if (fiscal_year) qb = qb.eq("fiscal_year", fiscal_year);

          if (query && query.trim()) {
            qb = qb.textSearch("fts_vector", query.trim(), { type: "websearch" });
          }

          const { data, error } = await qb.limit(effectiveLimit);
          if (error) throw new Error(error.message);

          const results = (data || []).map((doc: any) => ({
            id: doc.id,
            ticker: doc.ticker,
            title: doc.title,
            doc_type: doc.doc_type,
            fiscal_year: doc.fiscal_year,
            fiscal_quarter: doc.fiscal_quarter,
            report_date: doc.report_date,
            resolved_absolute_path: resolveAbsPath(doc.relative_path),
            resolved_distillate_path: doc.distillate_relative_path ? resolveAbsPath(doc.distillate_relative_path) : null,
            distillate_snippet: doc.distillate_content ? doc.distillate_content.slice(0, 300) + "..." : null,
          }));

          return {
            content: [{
              type: "text",
              text: `Gefundene Dokumente (${results.length}):\n` + JSON.stringify(results, null, 2)
            }]
          };
        }

        // ── AKTION: LIST ──
        if (action === "LIST") {
          let qb = supabase.from("stock_documents").select("id, ticker, doc_type, title, fiscal_year, fiscal_quarter, report_date, file_name, file_size_bytes, distillate_relative_path, created_at");
          if (ticker) qb = qb.eq("ticker", ticker.trim().toUpperCase());
          if (doc_type) qb = qb.eq("doc_type", doc_type.trim());
          if (fiscal_year) qb = qb.eq("fiscal_year", fiscal_year);

          const { data, error } = await qb.order("report_date", { ascending: false }).limit(effectiveLimit);
          if (error) throw new Error(error.message);

          return {
            content: [{ type: "text", text: JSON.stringify(data || [], null, 2) }]
          };
        }

        // ── AKTION: DELETE ──
        if (action === "DELETE") {
          if (!document_id) throw new Error("Für DELETE ist 'document_id' erforderlich.");
          const { error } = await supabase.from("stock_documents").delete().eq("id", document_id);
          if (error) throw new Error(error.message);

          return {
            content: [{ type: "text", text: `✅ Dokument ${document_id} erfolgreich aus der Datenbank gelöscht.` }]
          };
        }

        return { content: [{ type: "text", text: "Unbekannte Aktion für manage_stock_documents" }], isError: true };
      } catch (err: any) {
        log.error(`manage_stock_documents failed: ${err.message}`);
        return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
      }
    }
  );

  // ── 6. download_sec_reports ──────────────────────────────────────────
  server.registerTool(
    "download_sec_reports",
    {
      title: "Download SEC Reports (EDGAR)",
      description:
        "Lädt SEC-Berichte (10-Q, 10-K, 8-K) für eine Liste von US-Aktien-Tickern direkt von der SEC EDGAR API herunter.\n\n" +
        "ABLAUF:\n" +
        "1. Löst Ticker → CIK-Nummer über SEC company_tickers.json auf (cached).\n" +
        "2. Sucht den neuesten Bericht des gewünschten Typs in den SEC-Submissions.\n" +
        "3. Lädt das Dokument herunter und speichert es im Ticker-Ordner (DOCUMENTS_BASE_PATH/{TICKER}/).\n" +
        "4. Optional: Registriert das Dokument automatisch in der stock_documents DB.\n\n" +
        "EINSCHRÄNKUNGEN:\n" +
        "- Nur US-gelistete Aktien (SEC-Filer). Nicht-US-Ticker (z.B. 4GLD, LPK.DE) werden übersprungen.\n" +
        "- Sucht nur in den ~40 neuesten Filings (recent). Sehr alte Berichte ggf. nicht verfügbar.\n" +
        "- Rate-limitiert: max 10 Requests/Sekunde zur SEC (konfigurierbar via SEC_RATE_LIMIT_MS).",
      inputSchema: {
        tickers: z.array(z.string()).min(1).describe(
          "Liste von Ticker-Symbolen (z.B. ['HALO', 'AAPL', 'MSFT'])"
        ),
        report_type: z.enum(["10-Q", "10-K", "8-K"]).describe(
          "SEC Filing-Typ: '10-Q' (Quartalsbericht), '10-K' (Jahresbericht), '8-K' (Sondermeldung)"
        ),
        auto_register: z.boolean().optional().default(true).describe(
          "Automatisch in stock_documents DB registrieren (Default: true)"
        ),
      },
    },
    async ({ tickers, report_type, auto_register }: any) => {
      const results: any[] = [];
      const rateLimitMs = getSecRateLimitMs();

      try {
        // 1. CIK-Mapping laden (cached)
        let cikMap: Record<string, number>;
        try {
          cikMap = await loadSecCikMapping();
        } catch (mapErr: any) {
          log.error(`[download_sec_reports] CIK-Mapping fehlgeschlagen: ${mapErr.message}`);
          return {
            content: [{ type: "text", text: `❌ Konnte SEC CIK-Mapping nicht laden: ${mapErr.message}` }],
            isError: true,
          };
        }

        // 2. BasePath für Dateispeicherung (konsistent mit manage_stock_documents)
        const getBasePath = (): string => {
          const envPath = Deno.env.get("DOCUMENTS_BASE_PATH");
          if (envPath && envPath.trim()) return envPath.trim();
          try {
            Deno.statSync("/data/documents");
            return "/data/documents";
          } catch (_e) {
            return "/home/daniel/stock-data-node/data/documents";
          }
        };
        const computeSha256 = async (bytes: Uint8Array): Promise<string> => {
          const hashBuf = await crypto.subtle.digest("SHA-256", bytes as any);
          return Array.from(new Uint8Array(hashBuf)).map(b => b.toString(16).padStart(2, "0")).join("");
        };

        const basePath = getBasePath();

        // 3. Ticker-Schleife
        for (let idx = 0; idx < tickers.length; idx++) {
          const rawTicker = tickers[idx].trim().toUpperCase();

          // Skip ungültige Ticker
          if (!rawTicker || SKIP_PREFIXES.some(p => rawTicker.startsWith(p))) {
            results.push({ ticker: rawTicker, status: "skipped", reason: "Invalid ticker prefix" });
            continue;
          }

          const secTicker = normalizeTickerForSec(rawTicker);
          const cik = cikMap[secTicker] ?? cikMap[rawTicker];

          if (!cik) {
            results.push({ ticker: rawTicker, status: "skipped", reason: "Not a US-listed SEC filer" });
            continue;
          }

          try {
            // Rate Limiting (außer beim ersten Ticker)
            if (idx > 0) {
              await new Promise(r => setTimeout(r, rateLimitMs));
            }

            // Submissions abrufen
            const submissions = await fetchSecSubmissions(cik);
            const recent = submissions?.filings?.recent;
            if (!recent || !recent.form) {
              results.push({ ticker: rawTicker, status: "failed", reason: "No filings data from SEC" });
              continue;
            }

            // Neuesten Bericht des passenden Typs finden
            let accessionNumber: string | null = null;
            let primaryDoc: string | null = null;
            let filingDate: string | null = null;

            for (let i = 0; i < recent.form.length; i++) {
              if (recent.form[i] === report_type) {
                accessionNumber = recent.accessionNumber[i];
                primaryDoc = recent.primaryDocument[i];
                filingDate = recent.filingDate?.[i] || null;
                break;
              }
            }

            if (!accessionNumber || !primaryDoc) {
              results.push({ ticker: rawTicker, status: "failed", reason: `No recent ${report_type} filing found` });
              continue;
            }

            // Duplikat-Check in DB
            const fileName = primaryDoc;
            const { data: existingDocs } = await supabase
              .from("stock_documents")
              .select("id, file_name")
              .eq("ticker", rawTicker)
              .eq("file_name", fileName)
              .limit(1);

            if (existingDocs && existingDocs.length > 0) {
              results.push({ ticker: rawTicker, status: "already_exists", file: fileName, filing_date: filingDate });
              continue;
            }

            // Rate Limiting vor Download
            await new Promise(r => setTimeout(r, rateLimitMs));

            // Download-URL generieren
            const accNoDashes = accessionNumber.replace(/-/g, "");
            const cikInt = String(cik);
            const docUrl = `https://www.sec.gov/Archives/edgar/data/${cikInt}/${accNoDashes}/${primaryDoc}`;

            log.info(`[download_sec_reports] Downloading ${rawTicker} ${report_type}: ${docUrl}`);
            const fileBytes = await downloadSecDocument(docUrl);

            // Ticker-Ordner anlegen und Datei speichern
            const tickerDir = `${basePath}/${rawTicker}`;
            try {
              await Deno.mkdir(tickerDir, { recursive: true });
            } catch (_e) {
              // Bereits vorhanden
            }

            const destPath = `${tickerDir}/${fileName}`;
            await Deno.writeFile(destPath, fileBytes);
            log.info(`[download_sec_reports] Saved ${rawTicker} → ${destPath} (${fileBytes.length} bytes)`);

            let registered = false;

            // Optional: In stock_documents registrieren
            if (auto_register) {
              try {
                const sha256 = await computeSha256(fileBytes);
                const extParts = fileName.split(".");
                const fileExt = extParts.length > 1 ? extParts.pop()!.toLowerCase() : "";
                const relativePath = `${rawTicker}/${fileName}`;

                // Fiscal year/quarter aus filingDate ableiten
                let fiscalYear: number | null = null;
                let fiscalQuarter: string | null = null;
                if (filingDate) {
                  const d = new Date(filingDate);
                  fiscalYear = d.getFullYear();
                  const month = d.getMonth() + 1;
                  if (report_type === "10-Q") {
                    if (month <= 3) fiscalQuarter = "Q4";
                    else if (month <= 6) fiscalQuarter = "Q1";
                    else if (month <= 9) fiscalQuarter = "Q2";
                    else fiscalQuarter = "Q3";
                  }
                }

                const docTypeMap: Record<string, string> = { "10-Q": "10-Q", "10-K": "10-K", "8-K": "8-K" };

                const payload: any = {
                  ticker: rawTicker,
                  doc_type: docTypeMap[report_type] || "other",
                  title: `${rawTicker} ${report_type} (Filed: ${filingDate || "unknown"})`,
                  fiscal_year: fiscalYear,
                  fiscal_quarter: fiscalQuarter,
                  report_date: filingDate,
                  relative_path: relativePath,
                  file_name: fileName,
                  file_extension: fileExt,
                  file_size_bytes: fileBytes.length,
                  sha256_hash: sha256,
                  distillate_relative_path: null,
                  distillate_content: null,
                  distillate_generated_at: null,
                };

                const { error: insertErr } = await supabase
                  .from("stock_documents")
                  .insert(payload);

                if (insertErr) {
                  log.warn(`[download_sec_reports] DB-Insert für ${rawTicker} fehlgeschlagen: ${insertErr.message}`);
                } else {
                  registered = true;
                }
              } catch (regErr: any) {
                log.warn(`[download_sec_reports] Auto-Register für ${rawTicker} fehlgeschlagen: ${regErr.message}`);
              }
            }

            results.push({
              ticker: rawTicker,
              status: "success",
              file: fileName,
              filing_date: filingDate,
              size_bytes: fileBytes.length,
              registered,
            });
          } catch (tickerErr: any) {
            log.error(`[download_sec_reports] ${rawTicker} fehlgeschlagen: ${tickerErr.message}`);
            results.push({ ticker: rawTicker, status: "failed", reason: tickerErr.message });
          }
        }

        // Ergebnis formatieren
        const successCount = results.filter(r => r.status === "success").length;
        const skippedCount = results.filter(r => r.status === "skipped").length;
        const failedCount = results.filter(r => r.status === "failed").length;
        const existsCount = results.filter(r => r.status === "already_exists").length;

        const lines: string[] = [
          `📄 **SEC ${report_type} Download — Ergebnis (${tickers.length} Ticker)**`,
          `✅ Erfolgreich: ${successCount} | ⏭️ Übersprungen: ${skippedCount} | 📁 Bereits vorhanden: ${existsCount} | ❌ Fehlgeschlagen: ${failedCount}`,
          "",
        ];

        for (const r of results) {
          switch (r.status) {
            case "success":
              lines.push(`✅ **${r.ticker}**: ${r.file} (${(r.size_bytes / 1024).toFixed(0)} KB, Filed: ${r.filing_date})${r.registered ? " — In DB registriert" : ""}`);
              break;
            case "already_exists":
              lines.push(`📁 **${r.ticker}**: ${r.file} bereits vorhanden`);
              break;
            case "skipped":
              lines.push(`⏭️ **${r.ticker}**: ${r.reason}`);
              break;
            case "failed":
              lines.push(`❌ **${r.ticker}**: ${r.reason}`);
              break;
          }
        }

        return {
          content: [{ type: "text", text: lines.join("\n") }],
          isError: failedCount > 0 && successCount === 0,
        };
      } catch (err: any) {
        log.error(`download_sec_reports failed: ${err.message}`);
        return {
          content: [{ type: "text", text: `❌ Interner Fehler: ${err.message}` }],
          isError: true,
        };
      }
    }
  );

  // ── Autonomer Reconciler-Loop (Self-Healing Safety Net) ──────────────
  const reconcileIntervalSec = Number(Deno.env.get("METADATA_RECONCILE_INTERVAL_SEC") || 900);
  if (reconcileIntervalSec > 0) {
    setInterval(() => {
      reconcileMissingMetadata(50).catch(err => log.error(`[cda.ts] Background metadata reconcile failed: ${err.message}`));
    }, reconcileIntervalSec * 1000);
    log.info(`[registerCdaTools] Background metadata reconciler aktiv (Intervall: ${reconcileIntervalSec}s).`);
  }
}

