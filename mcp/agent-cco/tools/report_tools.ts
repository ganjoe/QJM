import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { supabase, log } from "./shared.ts";

/**
 * Report-Tools — Breite und Belege ueber den Influencer-Korpus.
 *
 * Warum es diese zwei Tools gibt: der Workitem-Lauf 2dd854d4 (Influencer-Report
 * 19./20.09.2026) hat 2,02 Mio Tokens verbraucht und ist gescheitert, weil ein
 * CCO-Task 1107 Posts Volltext durch den Modellkontext gezogen hat. Die Ticker
 * liegen aber laengst in agent_workspace.metadata->'tickers'. Arbeitsteilung:
 *
 *   ticker_breadth   — ERSTER Schritt. Ein Aufruf statt 110 Chunks: welche Ticker
 *                      wurden im Fenster von wie vielen VERSCHIEDENEN Autoren
 *                      genannt, ist der Ticker neu, ist er Nicht-US? ~2-4k Tokens.
 *   ticker_evidence  — ZWEITER Schritt, NUR fuer die Top-Kandidaten: 2-3 Belege
 *                      mit Autor, Datum, Zitat und Post-ID.
 *
 * Beide Funktionen sind serverseitige RPCs (migrations/022_ticker_breadth.sql).
 * Faustregel: erst Breite, dann Tiefe der Top-10 — nie beides in einem Item.
 */

/** ISO-Zeitstempel validieren; fehlende Werte auf ein sinnvolles Fenster setzen. */
function windowOr48h(start?: string, end?: string): { start: string; end: string } {
  const now = new Date();
  const endIso = end && !Number.isNaN(Date.parse(end)) ? new Date(end).toISOString() : now.toISOString();
  const startIso = start && !Number.isNaN(Date.parse(start))
    ? new Date(start).toISOString()
    : new Date(Date.parse(endIso) - 48 * 3600 * 1000).toISOString();
  return { start: startIso, end: endIso };
}

const ISO_HINT = "ISO 8601 UTC, z.B. 2026-09-19T00:00:00Z";

export function registerReportTools(server: McpServer) {
  // ────────────────────────────────────────────────────────────────────────
  // ticker_breadth — Breiten-Ranking (IMMER der erste Schritt)
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "ticker_breadth",
    {
      title: "Ticker breadth (ranking over the corpus)",
      description:
        "Rangliste der Ticker, die in einem Zeitfenster in den ueberwachten Posts genannt wurden: " +
        "Anzahl VERSCHIEDENER Autoren, Nennungen, Erstnennung im Korpus und ein Nicht-US-Flag. " +
        "Rechnet serverseitig ueber metadata.tickers — ein Aufruf, wenige tausend Tokens.\n\n" +
        "WHEN TO USE: IMMER als erster Schritt bei Rang-, Breiten- oder Watchlist-Fragen " +
        "(z.B. 'welche Ticker gehoeren kommende Woche auf die Watchlist'). " +
        "Mit only_new=true beantwortet es direkt 'was ist neu?' — ohne vorherige Sammlung, " +
        "denn die Neuheit wird gegen die First-Mentions-Historie geprueft, nicht abgeleitet.\n" +
        "WHEN NOT TO USE: Wenn du Belege/Zitate brauchst — danach ticker_evidence fuer die " +
        "Top-Kandidaten. Und NIE show_x_content ueber den gesamten Zeitraum, um dasselbe zu zaehlen: " +
        "das kostet gemessen 400k Tokens pro Tageshaelfte statt ~3k.\n" +
        "DEUTUNG: autoren ist die belastbare Groesse (Breite), nennungen nur Zusatz. " +
        "nicht_us=true heisst: Ticker mit Auslands-Suffix (EEE.L, PKN.WA) — vor dem Kauf pruefen.",
      inputSchema: {
        start_time: z.string().optional().describe("Beginn des Fensters (" + ISO_HINT + "). Default: 48h vor end_time."),
        end_time: z.string().optional().describe("Ende des Fensters (" + ISO_HINT + "). Default: jetzt."),
        min_authors: z.number().int().min(1).max(50).optional()
          .describe("Nur Ticker ab so vielen verschiedenen Autoren. Default 2 = belastbare Mehrfach-Belege. 1 = alles."),
        only_new: z.boolean().optional()
          .describe("true = nur Ticker, deren ERSTE Nennung ueberhaupt in dieses Fenster faellt. Der Kern der Watchlist-Frage."),
        limit: z.number().int().min(1).max(200).optional().describe("Max Zeilen. Default 50."),
      },
    },
    async ({ start_time, end_time, min_authors, only_new, limit }: any) => {
      try {
        const { start, end } = windowOr48h(start_time, end_time);
        const { data, error } = await supabase.rpc("ticker_breadth", {
          p_start: start,
          p_end: end,
          p_min_authors: min_authors ?? 2,
          p_only_new: only_new ?? false,
          p_limit: limit ?? 50,
        });
        if (error) throw error;
        const rows = (data ?? []) as any[];
        if (rows.length === 0) {
          return { content: [{ type: "text", text: "Keine Treffer im Fenster " + start + " bis " + end + (only_new ? " (nur Neuzugaenge)." : ".") }] };
        }
        const header = "| Ticker | Autoren | Nennungen | erstmals im Korpus | neu | nicht-US |";
        const sep = "|---|---|---|---|---|---|";
        const body = rows.map((r) =>
          "| " + r.ticker + " | " + r.autoren + " | " + r.nennungen + " | " +
          (r.erstmals_im_korpus ? new Date(r.erstmals_im_korpus).toLocaleDateString("de-DE") : "unbekannt") + " | " +
          (r.neu_im_fenster ? "JA" : "-") + " | " + (r.nicht_us ? "ja" : "-") + " |"
        ).join("\n");
        const neue = rows.filter((r) => r.neu_im_fenster).length;
        log.info("ticker_breadth: " + rows.length + " Zeilen, " + neue + " neu (" + start + " - " + end + ")");
        return {
          content: [{
            type: "text",
            text: "### Ticker-Breite " + start + " bis " + end + "\n" +
              rows.length + " Ticker (min. " + (min_authors ?? 2) + " Autoren)" +
              (only_new ? ", davon Neuzugaenge: " + neue : "") + "\n\n" +
              header + "\n" + sep + "\n" + body + "\n\n" +
              "Naechster Schritt: ticker_evidence fuer die Top-Kandidaten, die belegt werden sollen.",
          }],
        };
      } catch (err: any) {
        return { content: [{ type: "text", text: "ticker_breadth Fehler: " + (err?.message ?? err) }], isError: true };
      }
    }
  );

  // ────────────────────────────────────────────────────────────────────────
  // ticker_evidence — Belege zu EINEM Ticker
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "ticker_evidence",
    {
      title: "Ticker evidence (posts with quote and post id)",
      description:
        "Beleg-Posts zu EINEM Ticker im Zeitfenster: Autor, Datum, Post-ID und Zitatanfang, " +
        "nach Engagement sortiert.\n\n" +
        "WHEN TO USE: Nach ticker_breadth, fuer die Kandidaten, die in den Report sollen " +
        "(2-3 Belege je Kandidat genuegen). Liefert die Post-ID, die show_x_content nicht liefert.\n" +
        "WHEN NOT TO USE: Um einen Ticker erst zu FINDEN — dafuer ticker_breadth. " +
        "Und nicht in einer Schleife ueber alle Ticker: Belege nur fuer die Top-Kandidaten.",
      inputSchema: {
        ticker: z.string().describe("Ticker, z.B. VLO. Gross-/Kleinschreibung egal."),
        start_time: z.string().optional().describe("Beginn des Fensters (" + ISO_HINT + "). Default: 48h vor end_time."),
        end_time: z.string().optional().describe("Ende des Fensters (" + ISO_HINT + "). Default: jetzt."),
        limit: z.number().int().min(1).max(20).optional().describe("Max Belege. Default 3."),
      },
    },
    async ({ ticker, start_time, end_time, limit }: any) => {
      try {
        const { start, end } = windowOr48h(start_time, end_time);
        const { data, error } = await supabase.rpc("ticker_evidence", {
          p_ticker: ticker, p_start: start, p_end: end, p_limit: limit ?? 3,
        });
        if (error) throw error;
        const rows = (data ?? []) as any[];
        if (rows.length === 0) {
          return { content: [{ type: "text", text: "Keine Belege fuer " + ticker + " im Fenster " + start + " bis " + end + "." }] };
        }
        const blocks = rows.map((r, i) =>
          "**" + (i + 1) + ". " + (r.autor ?? "unbekannt") + "** · " +
          (r.zeit ? new Date(r.zeit).toLocaleString("de-DE") : "ohne Datum") +
          " · Post-ID: " + (r.post_id ?? "fehlt") + "\n> " + String(r.auszug ?? "")
        ).join("\n\n");
        log.info("ticker_evidence: " + ticker + " -> " + rows.length + " Belege");
        return { content: [{ type: "text", text: "### Belege " + ticker + " (" + start + " bis " + end + ")\n\n" + blocks }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: "ticker_evidence Fehler: " + (err?.message ?? err) }], isError: true };
      }
    }
  );
}
