import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { STOCK_DATA_NODE_URL, log, supabase } from "./shared.ts";

declare const Deno: any;

export function registerCdaTools(server: McpServer) {
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
            const rows = successfulTickers.map((t: string) => ({
              ticker: t,
              has_parquet: true,
              last_updated: new Date().toISOString(),
            }));
            await supabase.from("cda_master_universe").upsert(rows, { onConflict: "ticker", ignoreDuplicates: false });
            log.info(`[add_ticker] Synced ${successfulTickers.join(", ")} into cda_master_universe.`);
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
        action: z.enum(["GET", "UPDATE", "COUNT", "SYNC"]).describe("Die auszuführende Aktion: 'GET', 'UPDATE', 'COUNT', oder 'SYNC'"),
        ticker: z.string().optional().describe("Einzelner Ticker (z.B. AAPL) oder kommaseparierte Liste (AAPL,MSFT) bei GET/UPDATE."),
        tickers: z.array(z.string()).optional().describe("Array von Tickersymbolen für SYNC (Bulk-Upsert)."),
        utime: z.string().optional().describe("ISO-8601 Zeitstempel. Gültigkeitsbeginn der Daten. Bei Leer wird now() genutzt."),
        currency: z.string().optional().describe("Währung (z.B. USD, EUR)"),
        shares_outstanding: z.number().optional().describe("Ausstehende Aktien (Shares Outstanding) für Marktkapitalisierung"),
        eps: z.number().optional().describe("Earnings per share"),
        revenue: z.number().optional().describe("Umsatz"),
        earnings: z.string().optional().describe("Datum der nächsten/letzten Earnings (UTC Zeitstempel)"),
        has_parquet: z.boolean().optional().describe("Gibt an, ob lokale Chartdaten vorliegen."),
        chunk_size: z.number().optional().describe("Batch-Größe für Bulk-Sync (Standard: 500 oder UNIVERSE_SYNC_CHUNK_SIZE)"),
      },
    },
    async ({ action, ticker, tickers, utime, currency, shares_outstanding, eps, revenue, earnings, has_parquet, chunk_size }: any) => {
      try {
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
            const batch = syncList.slice(i, i + effectiveChunk).map((t: string) => ({
              ticker: t,
              has_parquet: has_parquet !== undefined ? has_parquet : true,
              last_updated: new Date().toISOString(),
            }));

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
            .select("*")
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
}
