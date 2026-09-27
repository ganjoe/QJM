import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { log, supabase } from "./shared.ts";

declare const Deno: any;

// ═══════════════════════════════════════════════════════════════════════════
// Earnings-Kalender: cda_earnings_history (Vergangenheit) +
// cda_master_universe.next_earnings (Zukunft). Siehe migrations/036.
//
//   * manage_earnings_calendar     → Agentenzugriff: lesen, syncen (Nasdaq),
//                                    manuell korrigieren, anstehende Termine listen.
//   * scripts/earnings_backfill.py → tiefer yfinance-Backfill (viele Quartale).
// ═══════════════════════════════════════════════════════════════════════════

const NASDAQ_BASE = "https://api.nasdaq.com/api";
const NASDAQ_HEADERS: Record<string, string> = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
  "Accept": "application/json, text/plain, */*",
  "Accept-Language": "en-US,en;q=0.9",
};

const SKIP_PREFIXES = ["$", "=", "^"];

// Quellen-Priorität für next_earnings: eine höherwertige Quelle wird von einer
// niedrigerwertigen nur überschrieben, wenn der gespeicherte Termin bereits vergangen ist.
// (Yahoo/yfinance trifft den IR-Termin erfahrungsgemäß genauer als die Zacks-Schätzung.)
const NEXT_SOURCE_PRIORITY: Record<string, number> = {
  manual: 100,
  confirmed: 90,
  yfinance: 60,
  nasdaq_zacks_estimate: 40,
  migrated_from_earnings: 20,
};

function sourcePriority(src: unknown): number {
  return NEXT_SOURCE_PRIORITY[String(src === null || src === undefined ? "" : src)] || 0;
}

export function cleanTicker(t: unknown): string {
  return String(t === null || t === undefined ? "" : t).trim().toUpperCase();
}

function isSkippable(ticker: string): boolean {
  return !ticker || SKIP_PREFIXES.some(function (p) { return ticker.startsWith(p); });
}

// ── Datums-Helfer ──────────────────────────────────────────────────────────

function parseUsDate(mdY: unknown): Date | null {
  const m = String(mdY === null || mdY === undefined ? "" : mdY).trim().match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (!m) return null;
  const d = new Date(Date.UTC(Number(m[3]), Number(m[1]) - 1, Number(m[2])));
  return Number.isFinite(d.getTime()) ? d : null;
}

function nthSundayUtc(year: number, month1: number, n: number): Date {
  const first = new Date(Date.UTC(year, month1 - 1, 1));
  const day = 1 + ((7 - first.getUTCDay()) % 7) + (n - 1) * 7;
  return new Date(Date.UTC(year, month1 - 1, day));
}

/** US-Boersenschluss 16:00 ET als UTC-Instant: 20:00Z in EDT, 21:00Z in EST. */
function usCloseUtc(day: Date): string {
  const y = day.getUTCFullYear();
  const dstStart = nthSundayUtc(y, 3, 2); // 2. Sonntag im Maerz
  const dstEnd = nthSundayUtc(y, 11, 1);  // 1. Sonntag im November
  const hour = day >= dstStart && day < dstEnd ? 20 : 21;
  return new Date(Date.UTC(y, day.getUTCMonth(), day.getUTCDate(), hour, 0, 0)).toISOString();
}

/** Akzeptiert ISO-8601 oder YYYY-MM-DD (dann 20:00Z) und gibt ISO zurueck. */
function normalizeIso(input: unknown): string | null {
  const raw = String(input === null || input === undefined ? "" : input).trim();
  if (!raw) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw + "T20:00:00.000Z";
  const d = new Date(raw.indexOf("T") >= 0 ? raw : raw + "T20:00:00Z");
  return Number.isFinite(d.getTime()) ? d.toISOString() : null;
}

function toNum(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  const s = String(v).replace(/[$,%\s]/g, "");
  if (!s || /^(n\/?a|-|--)$/i.test(s)) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

/** "Jun 2026" → { date: "2026-06-30", year: 2026, quarter: 2 } */
function parseFiscalQuarterEnd(label: unknown): { date: string | null; year: number | null; quarter: number | null } {
  const m = String(label === null || label === undefined ? "" : label).trim().match(/^([A-Za-z]{3})[a-z]*\s+(\d{4})$/);
  if (!m) return { date: null, year: null, quarter: null };
  const months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
  const mi = months.indexOf(m[1].toLowerCase());
  if (mi < 0) return { date: null, year: Number(m[2]) || null, quarter: null };
  const lastDay = new Date(Date.UTC(Number(m[2]), mi + 1, 0));
  const isCalQuarter = [2, 5, 8, 11].indexOf(mi) >= 0; // Maerz/Juni/Sep/Dez
  return {
    date: lastDay.toISOString().slice(0, 10),
    year: Number(m[2]),
    quarter: isCalQuarter ? Math.floor(mi / 3) + 1 : null,
  };
}

// ── Nasdaq-Quellen (kein API-Key noetig) ──────────────────────────────────

async function nasdaqGet(path: string, timeoutMs = 20000): Promise<any> {
  const res = await fetch(NASDAQ_BASE + path, {
    headers: NASDAQ_HEADERS,
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) throw new Error("Nasdaq HTTP " + res.status + " fuer " + path);
  const json = await res.json();
  return json && json.data ? json.data : null;
}

/** Vergangene Termine (Nasdaq liefert die letzten ~4 Quartale). */
export async function fetchNasdaqHistory(ticker: string): Promise<any[]> {
  const data = await nasdaqGet("/company/" + encodeURIComponent(ticker) + "/earnings-surprise");
  const rows = data && data.earningsSurpriseTable ? data.earningsSurpriseTable.rows : null;
  if (!Array.isArray(rows)) return [];
  const out: any[] = [];
  for (const r of rows) {
    const day = parseUsDate(r ? r.dateReported : null);
    if (!day) continue;
    const fq = parseFiscalQuarterEnd(r ? r.fiscalQtrEnd : null);
    out.push({
      ticker: ticker,
      report_date: usCloseUtc(day),
      fiscal_quarter_end: fq.date,
      fiscal_year: fq.year,
      fiscal_quarter: fq.quarter,
      eps_actual: toNum(r ? r.eps : null),
      eps_estimate: toNum(r ? r.consensusForecast : null),
      eps_surprise_pct: toNum(r ? r.percentageSurprise : null),
      source: "nasdaq",
    });
  }
  return out;
}

/** Naechster geschaetzter Termin (Zacks-Schaetzung ueber Nasdaq). */
export async function fetchNasdaqNext(ticker: string): Promise<any | null> {
  const data = await nasdaqGet("/analyst/" + encodeURIComponent(ticker) + "/earnings-date");
  const text = String(data && data.reportText ? data.reportText : "").replace(/\s+/g, " ").trim();
  if (!text) return null;
  const m = text.match(/report[s]?\s+earnings\s+on\s+(\d{1,2}\/\d{1,2}\/\d{4})/i) ||
            text.match(/\bon\s+(\d{1,2}\/\d{1,2}\/\d{4})/i);
  if (!m) return null;
  const day = parseUsDate(m[1]);
  if (!day) return null;
  const consensus = text.match(/consensus EPS forecast for the quarter is \$([\d.,]+)/i);
  return {
    date: usCloseUtc(day),
    date_only: day.toISOString().slice(0, 10),
    consensus_eps: consensus ? toNum(consensus[1]) : null,
    estimated: /estimat|expect/i.test(text),
    note: text.slice(0, 300),
  };
}

// ── Schreib-Helfer ────────────────────────────────────────────────────────

async function upsertHistory(rows: any[]): Promise<{ written: number; error?: string }> {
  if (rows.length === 0) return { written: 0 };
  const { error } = await supabase
    .from("cda_earnings_history")
    .upsert(rows, { onConflict: "ticker,report_date" });
  if (error) return { written: 0, error: error.message };
  return { written: rows.length };
}

async function loadMaster(ticker: string): Promise<any | null> {
  const { data } = await supabase
    .from("cda_master_universe")
    .select("ticker, earnings, next_earnings, next_earnings_source")
    .eq("ticker", ticker)
    .maybeSingle();
  return data ? data : null;
}

// ── Sync-Kern (auch vom Backfill-Skript nutzbar) ──────────────────────────

export interface EarningsSyncResult {
  ticker: string;
  status: "ok" | "skipped" | "failed";
  reason?: string;
  history_rows: number;
  history_written: number;
  last_report?: string | null;
  next_earnings?: string | null;
  next_source?: string | null;
  next_note?: string;
  kept_manual?: boolean;
}

export async function syncEarningsForTicker(
  tickerRaw: string,
  opts: { writeHistory?: boolean; keepManualNext?: boolean } = {},
): Promise<EarningsSyncResult> {
  const ticker = cleanTicker(tickerRaw);
  const writeHistory = opts.writeHistory !== false;
  const keepManualNext = opts.keepManualNext !== false;

  const result: EarningsSyncResult = { ticker: ticker, status: "ok", history_rows: 0, history_written: 0 };

  if (isSkippable(ticker)) {
    return Object.assign(result, { status: "skipped" as const, reason: "kein handelbares Symbol" });
  }

  const master = await loadMaster(ticker);
  if (!master) {
    return Object.assign(result, { status: "skipped" as const, reason: "nicht im Master Universe (cda_master_universe)" });
  }

  let history: any[] = [];
  let next: any = null;
  try {
    history = await fetchNasdaqHistory(ticker);
  } catch (err: any) {
    result.reason = "Historie: " + err.message;
  }
  try {
    next = await fetchNasdaqNext(ticker);
  } catch (err: any) {
    result.reason = [result.reason, "Next: " + err.message].filter(Boolean).join(" | ");
  }

  if (history.length === 0 && !next) {
    return Object.assign(result, {
      status: "failed" as const,
      reason: result.reason || "keine Nasdaq-Daten (Nicht-US-Titel?)",
    });
  }

  result.history_rows = history.length;

  if (writeHistory && history.length > 0) {
    const up = await upsertHistory(history);
    if (up.error) result.reason = [result.reason, "Upsert: " + up.error].filter(Boolean).join(" | ");
    else result.history_written = up.written;
  }

  const patch: any = {};
  const latestReport = history.map(function (h) { return h.report_date as string; }).sort().pop() || null;
  result.last_report = latestReport;
  if (latestReport && (!master.earnings || new Date(latestReport) > new Date(master.earnings))) {
    patch.earnings = latestReport;
  }

  const now = Date.now();
  const curNext = master.next_earnings ? new Date(master.next_earnings) : null;
  const curNextFuture = !!curNext && curNext.getTime() > now;
  const incomingSource = "nasdaq_zacks_estimate";
  const protectManual = String(master.next_earnings_source || "") === "manual" && keepManualNext;
  const keepExisting = curNextFuture &&
    (protectManual || sourcePriority(master.next_earnings_source) > sourcePriority(incomingSource));

  if (keepExisting) {
    result.kept_manual = protectManual;
    result.next_earnings = master.next_earnings;
    result.next_source = master.next_earnings_source;
    result.next_note = protectManual
      ? "manuell gesetzter Termin bleibt unveraendert"
      : "hoeherwertige Quelle (" + String(master.next_earnings_source) + ") bleibt erhalten";
  } else if (next) {
    const fetched = new Date(next.date);
    if (fetched.getTime() >= now - 24 * 3600 * 1000) {
      patch.next_earnings = next.date;
      patch.next_earnings_source = "nasdaq_zacks_estimate";
      result.next_earnings = next.date;
      result.next_source = "nasdaq_zacks_estimate";
      result.next_note = next.estimated ? "Zacks-Schaetzung (unbestaetigt)" : "Nasdaq-Termin";
    } else {
      result.next_note = "Nasdaq-Schaetzung liegt in der Vergangenheit - ignoriert";
    }
  } else if (curNext && !curNextFuture) {
    patch.next_earnings = null;
    patch.next_earnings_source = null;
    result.next_note = "kein neuer Termin gefunden; abgelaufenen Termin entfernt";
  }

  if (Object.keys(patch).length > 0) {
    const { error } = await supabase.from("cda_master_universe").update(patch).eq("ticker", ticker);
    if (error) {
      result.status = "failed";
      result.reason = [result.reason, "Master-Update: " + error.message].filter(Boolean).join(" | ");
      return result;
    }
  }

  if (result.next_earnings === undefined) result.next_earnings = master.next_earnings || null;
  if (result.next_source === undefined) result.next_source = master.next_earnings_source || null;
  return result;
}

// ── Ausgabe-Helfer ────────────────────────────────────────────────────────

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "-";
  return String(iso).slice(0, 10);
}

function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return null;
  const today = new Date();
  const a = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  const b = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  return Math.round((a - b) / 86400000);
}

function fmtNum(v: any): string {
  return v === null || v === undefined ? "-" : String(Number(v));
}

function sleep(ms: number): Promise<void> {
  return new Promise(function (r) { setTimeout(r, ms); });
}

// ── Tool ─────────────────────────────────────────────────────────────────

export function registerEarningsTools(server: McpServer) {
  server.registerTool(
    "manage_earnings_calendar",
    {
      title: "Earnings-Kalender (Historie + naechster Termin)",
      description:
        "Verwaltet Earnings-Termine in zwei getrennten Strukturen:\n" +
        "  * cda_earnings_history = VERGANGENE Termine (1 Zeile je Ticker + Termin, inkl. EPS actual/estimate/Surprise).\n" +
        "  * cda_master_universe.next_earnings = NAECHSTER Termin (vorwaertsgerichtet) + next_earnings_source.\n" +
        "  * cda_master_universe.earnings = letzter gemeldeter Termin.\n\n" +
        "ACTIONS:\n" +
        "- GET: Zeigt next_earnings + Historie eines oder mehrerer Ticker (kommasepariert).\n" +
        "- SYNC: Holt Historie (letzte ~4 Quartale) und den naechsten Termin von Nasdaq und schreibt beides. " +
        "Manuell gesetzte, noch zukuenftige Termine (source=manual) werden NICHT ueberschrieben.\n" +
        "- SET_NEXT: Setzt next_earnings manuell (z.B. bestaetigter Termin von der IR-Seite; source=confirmed).\n" +
        "QUELLEN-PRIORITAET fuer next_earnings: manual(100) > confirmed(90) > yfinance(60) > nasdaq_zacks_estimate(40). " +
        "Eine hoeherwertige, noch zukuenftige Quelle wird nicht durch eine niedrigerwertige ersetzt.\n" +
        "- UPSERT: Legt einen historischen Termin manuell an/aktualisiert ihn (idempotent ueber Ticker+Termin).\n" +
        "- LIST: Alle Ticker mit next_earnings in den naechsten days Tagen (wer berichtet demnaechst).\n" +
        "- DELETE: Loescht Historien-Zeilen eines Tickers (report_date oder all=true).\n\n" +
        "FUER TIEFE HISTORIE (viele Quartale): scripts/earnings_backfill.py (yfinance, laeuft im stock-data-node-Container).\n" +
        "DANACH: pca-Scanner zeigt days_to_earnings aus next_earnings und kann danach filtern.",
      inputSchema: {
        action: z.enum(["GET", "SYNC", "SET_NEXT", "UPSERT", "LIST", "DELETE"]).describe("Die auszufuehrende Aktion"),
        ticker: z.string().optional().describe("Einzelner Ticker oder kommaseparierte Liste (GET/SYNC/DELETE)"),
        tickers: z.array(z.string()).optional().describe("Array von Tickern fuer SYNC"),
        watchlist: z.string().optional().describe("SYNC: Ticker aus einer Supabase-Watchlist (z.B. current_positions)"),
        next_earnings: z.string().optional().describe("SET_NEXT: naechster Termin (ISO-8601 oder YYYY-MM-DD)"),
        source: z.string().optional().describe("SET_NEXT/UPSERT: Herkunft (Default manual)"),
        report_date: z.string().optional().describe("UPSERT/DELETE: Termin (ISO-8601 oder YYYY-MM-DD)"),
        fiscal_quarter_end: z.string().optional().describe("UPSERT: Quartalsende (YYYY-MM-DD)"),
        eps_actual: z.number().optional().describe("UPSERT: gemeldetes EPS"),
        eps_estimate: z.number().optional().describe("UPSERT: Konsens-EPS zum Termin"),
        eps_surprise_pct: z.number().optional().describe("UPSERT: Ueberraschung in Prozent"),
        days: z.number().optional().describe("LIST: Fenster in Tagen (Default 14)"),
        limit: z.number().optional().describe("LIST/GET: max. Zeilen (Default 25 bzw. 8)"),
        write_history: z.boolean().optional().describe("SYNC: Historie mitschreiben (Default true)"),
        delay_ms: z.number().optional().describe("SYNC: Pause zwischen Tickern (Default 600ms)"),
        all: z.boolean().optional().describe("DELETE: gesamte Historie des Tickers entfernen"),
      },
    },
    async (args: any) => {
      const { action, ticker, tickers, watchlist, next_earnings, source, report_date, fiscal_quarter_end, eps_actual, eps_estimate, eps_surprise_pct, days, limit, write_history, delay_ms, all } = args;
      try {
        if (action === "GET") {
          const list = String(ticker || "").split(",").map(cleanTicker).filter(Boolean);
          if (list.length === 0) throw new Error("Fuer GET ist ticker erforderlich (z.B. META oder META,MSFT).");
          const maxRows = limit && limit > 0 ? limit : 8;

          const { data: master, error } = await supabase
            .from("cda_master_universe")
            .select("ticker, currency, earnings, next_earnings, next_earnings_source")
            .in("ticker", list);
          if (error) throw new Error(error.message);

          const { data: hist, error: hErr } = await supabase
            .from("cda_earnings_history")
            .select("ticker, report_date, fiscal_quarter_end, fiscal_year, fiscal_quarter, eps_actual, eps_estimate, eps_surprise_pct, source")
            .in("ticker", list)
            .order("report_date", { ascending: false });
          if (hErr) throw new Error(hErr.message);

          const lines: string[] = [];
          for (const t of list) {
            const m = (master || []).find(function (r: any) { return r.ticker === t; });
            const dte = daysUntil(m ? m.next_earnings : null);
            const own = (hist || []).filter(function (h: any) { return h.ticker === t; });
            lines.push("## " + t);
            if (m) {
              lines.push("**Naechster Termin:** " +
                (m.next_earnings ? fmtDate(m.next_earnings) + " (in " + dte + "d)" : "- nicht hinterlegt") +
                " · Quelle: " + String(m.next_earnings_source || "-"));
              lines.push("**Letzter Termin:** " + fmtDate(m.earnings) + " · **Historie-Zeilen:** " + own.length);
            } else {
              lines.push("Nicht im Master Universe.");
            }
            const rows = own.slice(0, maxRows);
            if (rows.length > 0) {
              lines.push("| Termin | Quartal | EPS ist | EPS erwartet | Ueberraschung | Quelle |");
              lines.push("| :--- | :--- | ---: | ---: | ---: | :--- |");
              for (const r of rows) {
                const q = r.fiscal_year && r.fiscal_quarter ? ("Q" + r.fiscal_quarter + " " + r.fiscal_year) : (r.fiscal_quarter_end || "-");
                lines.push("| " + fmtDate(r.report_date) + " | " + q + " | " + fmtNum(r.eps_actual) + " | " +
                  fmtNum(r.eps_estimate) + " | " + fmtNum(r.eps_surprise_pct) + "% | " + (r.source || "-") + " |");
              }
            } else {
              lines.push("_Keine Historie gespeichert - SYNC oder scripts/earnings_backfill.py ausfuehren._");
            }
            lines.push("");
          }
          return { content: [{ type: "text", text: lines.join("\n") }] };
        }

        if (action === "SYNC") {
          let list: string[] = [];
          if (Array.isArray(tickers) && tickers.length > 0) list = tickers.map(cleanTicker);
          else if (ticker) list = String(ticker).split(",").map(cleanTicker);
          else if (watchlist) {
            const { data, error } = await supabase.from("pca_watchlists").select("ticker").eq("list_name", watchlist);
            if (error) throw new Error("Watchlist " + watchlist + ": " + error.message);
            list = (data || []).map(function (r: any) { return cleanTicker(r.ticker); });
          }
          list = Array.from(new Set(list.filter(function (t) { return t && !isSkippable(t); })));
          if (list.length === 0) throw new Error("Fuer SYNC ticker, tickers oder watchlist angeben.");

          const delay = typeof delay_ms === "number" && delay_ms >= 0 ? delay_ms : 600;
          const results: EarningsSyncResult[] = [];
          for (let i = 0; i < list.length; i++) {
            results.push(await syncEarningsForTicker(list[i], { writeHistory: write_history !== false }));
            if (i < list.length - 1 && delay > 0) await sleep(delay);
          }

          const okRows = results.filter(function (r) { return r.status === "ok"; });
          const lines: string[] = [
            "Earnings-Sync: " + okRows.length + "/" + results.length + " erfolgreich",
            "",
            "| Ticker | Historie | Letzter Termin | Naechster Termin | Quelle | Hinweis |",
            "| :--- | ---: | :--- | :--- | :--- | :--- |",
          ];
          for (const r of results) {
            if (r.status !== "ok") {
              lines.push("| " + r.ticker + " | - | - | - | - | " + (r.status === "skipped" ? "uebersprungen: " : "FEHLER: ") + (r.reason || "") + " |");
              continue;
            }
            lines.push("| " + r.ticker + " | " + r.history_written + "/" + r.history_rows + " | " + fmtDate(r.last_report) +
              " | " + fmtDate(r.next_earnings) + " | " + String(r.next_source || "-") + " | " +
              (r.reason || r.next_note || "") + (r.kept_manual ? " (manuell geschuetzt)" : "") + " |");
          }
          return { content: [{ type: "text", text: lines.join("\n") }], isError: okRows.length === 0 };
        }

        if (action === "SET_NEXT") {
          const t = cleanTicker(ticker);
          if (!t) throw new Error("Fuer SET_NEXT ist ticker erforderlich.");
          if (!next_earnings) throw new Error("Fuer SET_NEXT ist next_earnings erforderlich (ISO-8601 oder YYYY-MM-DD).");
          const iso = normalizeIso(next_earnings);
          if (!iso) throw new Error("'" + next_earnings + "' ist kein gueltiges Datum.");
          const master = await loadMaster(t);
          if (!master) throw new Error(t + " ist nicht im Master Universe.");
          const { error } = await supabase
            .from("cda_master_universe")
            .update({ next_earnings: iso, next_earnings_source: source ? String(source) : "manual" })
            .eq("ticker", t);
          if (error) throw new Error(error.message);
          return {
            content: [{
              type: "text",
              text: "OK " + t + ": next_earnings = " + iso + " (in " + daysUntil(iso) + "d), Quelle " + (source || "manual") + ".",
            }],
          };
        }

        if (action === "UPSERT") {
          const t = cleanTicker(ticker);
          if (!t) throw new Error("Fuer UPSERT ist ticker erforderlich.");
          if (!report_date) throw new Error("Fuer UPSERT ist report_date erforderlich.");
          const iso = normalizeIso(report_date);
          if (!iso) throw new Error("'" + report_date + "' ist kein gueltiges Datum.");
          const master = await loadMaster(t);
          if (!master) throw new Error(t + " ist nicht im Master Universe.");

          let fqe: string | null = fiscal_quarter_end ? normalizeIso(fiscal_quarter_end) : null;
          let fy: number | null = null;
          let fq: number | null = null;
          if (fqe) {
            const parsed = parseFiscalQuarterEnd(
              new Date(fqe).toLocaleString("en-US", { month: "short", year: "numeric", timeZone: "UTC" }),
            );
            fy = parsed.year;
            fq = parsed.quarter;
          }

          const row: any = {
            ticker: t,
            report_date: iso,
            fiscal_quarter_end: fqe ? fqe.slice(0, 10) : null,
            fiscal_year: fy,
            fiscal_quarter: fq,
            eps_actual: eps_actual === undefined ? null : eps_actual,
            eps_estimate: eps_estimate === undefined ? null : eps_estimate,
            eps_surprise_pct: eps_surprise_pct === undefined ? null : eps_surprise_pct,
            source: source ? String(source) : "manual",
          };
          const up = await upsertHistory([row]);
          if (up.error) throw new Error(up.error);

          const dte = daysUntil(iso);
          const future = dte !== null && dte > 0;
          return {
            content: [{
              type: "text",
              text: "OK " + t + ": Historien-Zeile " + iso.slice(0, 10) + " gespeichert (Quelle " + row.source + ")" +
                (future ? " - ACHTUNG: liegt in der Zukunft (" + dte + "d); dafuer ist SET_NEXT gedacht." : ""),
            }],
          };
        }

        if (action === "LIST") {
          const windowDays = days && days > 0 ? days : 14;
          const maxRows = limit && limit > 0 ? limit : 25;
          const from = new Date();
          const to = new Date(from.getTime() + windowDays * 86400000);

          const { data, error } = await supabase
            .from("cda_master_universe")
            .select("ticker, currency, eps, next_earnings, next_earnings_source, has_parquet")
            .gte("next_earnings", from.toISOString())
            .lte("next_earnings", to.toISOString())
            .order("next_earnings", { ascending: true })
            .limit(maxRows);
          if (error) throw new Error(error.message);

          const rows = (data || []).filter(function (r: any) { return r.has_parquet !== false; });
          const lines: string[] = [
            "Anstehende Earnings - heute + " + windowDays + " Tage - " + rows.length + " Ticker mit lokalen Chartdaten",
            "",
            "| Termin | in | Ticker | Waehrung | EPS (trailing) | Quelle |",
            "| :--- | ---: | :--- | :--- | ---: | :--- |",
          ];
          for (const r of rows) {
            lines.push("| " + fmtDate(r.next_earnings) + " | " + daysUntil(r.next_earnings) + "d | **" + r.ticker + "** | " +
              (r.currency || "-") + " | " + fmtNum(r.eps) + " | " + String(r.next_earnings_source || "-") + " |");
          }
          if (rows.length === 0) lines.push("| - | - | _keine Termine im Fenster_ | - | - | - |");
          return { content: [{ type: "text", text: lines.join("\n") }] };
        }

        if (action === "DELETE") {
          const t = cleanTicker(ticker);
          if (!t) throw new Error("Fuer DELETE ist ticker erforderlich.");
          if (!all && !report_date) throw new Error("Fuer DELETE report_date angeben oder all=true setzen.");

          let query = supabase.from("cda_earnings_history").delete().eq("ticker", t);
          if (!all && report_date) {
            const iso = normalizeIso(report_date);
            if (!iso) throw new Error("'" + report_date + "' ist kein gueltiges Datum.");
            query = query.eq("report_date", iso);
          }
          const { data, error } = await query.select("id");
          if (error) throw new Error(error.message);
          const removed = Array.isArray(data) ? data.length : 0;
          return {
            content: [{
              type: "text",
              text: t + ": " + removed + " Historien-Zeile(n) entfernt" + (all ? " (gesamte Historie)" : " (" + report_date + ")") + ".",
            }],
          };
        }

        return { content: [{ type: "text", text: "Unbekannte Aktion" }], isError: true };
      } catch (err: any) {
        log.error("manage_earnings_calendar failed: " + err.message);
        return { content: [{ type: "text", text: "Fehler: " + err.message }], isError: true };
      }
    },
  );

  log.info("[registerEarningsTools] manage_earnings_calendar registriert.");
}
