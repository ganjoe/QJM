import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { supabase, log, text, fail, describeError } from "./shared.ts";

/**
 * Die BOSS-Schnittstelle: Laeufe einreichen, beobachten, Ergebnis lesen.
 *
 * Bis hierher war die Sekretaerin nur ueber die Kommandozeile bedienbar. Diese
 * drei Tools machen sie aus jedem DSH-Chat heraus benutzbar — submit, status,
 * result. Die Sekretaerin nimmt den Auftrag beim naechsten Tick (max. 2 s) auf.
 *
 * Bewusste Grenze: der Server kennt den Aufrufer nicht (geteilter Key im
 * internen Netz). Jede Session mit den wiq-Tools kann einen Lauf einreichen.
 */

const OPEN_STATES = ["pending", "running"];

function shortId(value: string): string {
  return String(value).slice(0, 8);
}

export function registerRunTools(server: McpServer) {
  // ────────────────────────────────────────────────────────────────────────
  // run_submit — einen Boss-Prompt einreichen
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "run_submit",
    {
      title: "Submit a work order",
      description:
        "Reicht einen Auftrag in natürlicher Sprache bei der Workitem-Engine ein. Der Lead-Engineer " +
        "plant daraus einen Graphen, Worker arbeiten ihn ab, ein Review entscheidet.\n\n" +
        "Der Aufruf kehrt sofort zurück — die Sekretärin nimmt den Lauf beim nächsten Tick auf " +
        "(max. 2 s). Fortschritt mit run_status, Ergebnis mit run_result.\n\n" +
        "WHEN TO USE: Wenn der Boss eine Aufgabe delegieren will, die Recherche, mehrere Schritte " +
        "oder eine Bewertung braucht.\n" +
        "WHEN NOT TO USE: Für Fragen, die du selbst in einem Schritt beantworten kannst — eine " +
        "Ausführung kostet Minuten und Tokens.",
      inputSchema: {
        prompt: z.string().describe("Der Auftrag in natürlicher Sprache, so wie der Boss ihn formuliert."),
        title: z.string().optional().describe("Kurzer Titel; sonst die erste Zeile des Prompts."),
        rounds: z.number().int().min(1).max(200).optional()
          .describe("Deckel für das PLANEN des Leads in Modell-Runden. Ohne Angabe schätzt er selbst."),
        tokens: z.number().int().min(1000).optional()
          .describe("Dasselbe in Tokens."),
        max_rounds: z.number().int().min(1).max(10).optional()
          .describe("Wie oft der Lead nachplanen darf (1-10, Default 2). Ist das Review am " +
                    "letzten erlaubten Durchgang unbefriedigt, endet der Lauf als failed. " +
                    "NICHT zu verwechseln mit 'rounds' (Budget des Planungs-Items)."),
      },
    },
    async ({ prompt, title, rounds, tokens, max_rounds }) => {
      const clean = prompt.trim();
      if (clean.length === 0) return fail("Der Prompt ist leer.");
      const headline = (title ?? clean.split("\n")[0]).slice(0, 120);
      const budget: Record<string, number> = {};
      if (rounds) budget.rounds = rounds;
      if (tokens) budget.tokens = tokens;

      const created = await supabase
        .from("change_items")
        .insert({ title: headline, entry_prompt: clean, max_rounds: max_rounds ?? 2 })
        .select("id")
        .single();
      if (created.error || !created.data) {
        return fail("Lauf konnte nicht angelegt werden: " + describeError(created.error));
      }
      const changeItemId = created.data.id as string;

      const item = await supabase
        .from("workitems")
        .insert({
          change_item_id: changeItemId,
          step_key: "initial",
          type: "initial",
          role: "lead_engineer",
          priority: 100,
          payload: { prompt: clean },
          budget,
        })
        .select("id")
        .single();
      if (item.error || !item.data) {
        // Kein halber Lauf: das change_item ohne Wurzel-Item waere unerreichbar.
        await supabase.from("change_items").delete().eq("id", changeItemId);
        return fail("Wurzel-Item konnte nicht angelegt werden (zurueckgenommen): " + describeError(item.error));
      }

      log.info("run_submit: " + changeItemId + " budget=" + JSON.stringify(budget));
      return text([
        "Eingereicht.",
        "change_item_id: " + changeItemId,
        "workitem_id:    " + item.data.id,
        "budget:         " + (Object.keys(budget).length ? JSON.stringify(budget) : "(schaetzt der Lead)"),
        "max_rounds:     " + (max_rounds ?? 2),
        "",
        "Die Sekretaerin nimmt den Lauf beim naechsten Tick auf.",
        "Fortschritt: run_status mit change_item_id=" + changeItemId,
      ].join("\n"));
    },
  );

  // ────────────────────────────────────────────────────────────────────────
  // run_status — den Graphen eines Laufs ansehen
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "run_status",
    {
      title: "Watch a run",
      description:
        "Zeigt Zustand und Fortschritt eines Laufs: jedes Workitem mit Status, Runde, Versuch, " +
        "Budget und Ist-Verbrauch.\n\n" +
        "WHEN TO USE: Nach run_submit, um zu sehen ob der Lauf laeuft, haengt oder fertig ist.",
      inputSchema: {
        change_item_id: z.string().describe("UUID des Laufs aus run_submit."),
        include_results: z.boolean().optional()
          .describe("Auch die (gekuerzten) Ergebnisse der Items zeigen. Default false."),
      },
    },
    async ({ change_item_id, include_results }) => {
      const run = await supabase
        .from("change_items")
        .select("id,title,state,entry_prompt,created_at,finished_at")
        .eq("id", change_item_id)
        .maybeSingle();
      if (run.error) return fail("Lesefehler: " + describeError(run.error));
      if (!run.data) return fail("Unbekannter Lauf " + change_item_id + ".");

      const items = await supabase
        .from("workitems")
        .select("step_key,type,role,status,round,attempts,budget,usage,result")
        .eq("change_item_id", change_item_id)
        .order("created_at");
      if (items.error) return fail("Item-Lesefehler: " + describeError(items.error));

      const rows = (items.data ?? []) as Record<string, unknown>[];
      const open = rows.filter((r) => OPEN_STATES.includes(String(r.status))).length;
      const lines = [
        String(run.data.state).toUpperCase() + "  " + String(run.data.title),
        open > 0 ? "(" + open + " Item(s) offen)" : "(nichts offen)",
        "",
        "step_key".padEnd(26) + "typ".padEnd(9) + "rolle".padEnd(15) + "status".padEnd(10)
          + "runde".padStart(6) + "  runden".padStart(10) + "  tokens".padStart(16),
      ];
      for (const r of rows) {
        const b = (r.budget ?? {}) as Record<string, number>;
        const u = (r.usage ?? {}) as Record<string, number>;
        lines.push(
          String(r.step_key).padEnd(26) + String(r.type).padEnd(9) + String(r.role).padEnd(15)
          + String(r.status).padEnd(10) + String(r.round).padStart(6) + "  "
          + ((u.rounds ?? 0) + "/" + (b.rounds ?? "-")).padStart(10) + "  "
          + ((u.tokens ?? 0) + "/" + (b.tokens ?? "-")).padStart(16),
        );
      }
      if (include_results) {
        lines.push("", "Ergebnisse (gekuerzt):");
        for (const r of rows) {
          const result = r.result;
          if (result === null || result === undefined) continue;
          lines.push("  " + r.step_key + ": " + JSON.stringify(result).slice(0, 400));
        }
      }
      return text(lines.join("\n"));
    },
  );

  // ────────────────────────────────────────────────────────────────────────
  // run_result — die Antwort des Laufs
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "run_result",
    {
      title: "Read the answer of a run",
      description:
        "Liefert das Ergebnis des letzten abgeschlossenen Reviews — also die Antwort auf den " +
        "Boss-Prompt — zusammen mit den Zwischenergebnissen der Tasks.\n\n" +
        "WHEN TO USE: Wenn run_status zeigt, dass der Lauf durch ist.\n" +
        "WHEN NOT TO USE: Solange noch Items offen sind; dann kommt hier nur ein Hinweis.",
      inputSchema: {
        change_item_id: z.string().describe("UUID des Laufs."),
        include_tasks: z.boolean().optional()
          .describe("Auch die Ergebnisse der Task-Items zeigen. Default true."),
      },
    },
    async ({ change_item_id, include_tasks }) => {
      const run = await supabase
        .from("change_items")
        .select("state,title")
        .eq("id", change_item_id)
        .maybeSingle();
      if (run.error) return fail("Lesefehler: " + describeError(run.error));
      if (!run.data) return fail("Unbekannter Lauf " + change_item_id + ".");

      const items = await supabase
        .from("workitems")
        .select("step_key,type,role,status,round,result")
        .eq("change_item_id", change_item_id)
        .order("created_at");
      if (items.error) return fail("Item-Lesefehler: " + describeError(items.error));

      const rows = (items.data ?? []) as Record<string, unknown>[];
      const open = rows.filter((r) => OPEN_STATES.includes(String(r.status)));
      if (open.length > 0) {
        return text("Der Lauf ist noch nicht durch (" + String(run.data.state) + ", "
          + open.length + " Item(s) offen: " + open.map((r) => r.step_key).join(", ") + ").");
      }

      const reviews = rows.filter((r) => r.type === "review").sort(
        (a, b) => Number(a.round) - Number(b.round),
      );
      const last = reviews[reviews.length - 1];
      const lines: string[] = [
        "Lauf: " + String(run.data.state).toUpperCase() + "  " + String(run.data.title),
        "",
      ];
      if (last) {
        lines.push("=== Review (Runde " + last.round + ", " + last.status + ") ===");
        lines.push(JSON.stringify(last.result, null, 1));
      } else {
        lines.push("(kein Review-Item vorhanden)");
      }
      if (include_tasks !== false) {
        const tasks = rows.filter((r) => r.type === "task");
        if (tasks.length > 0) {
          lines.push("", "=== Zwischenergebnisse der Tasks ===");
          for (const t of tasks) {
            lines.push("--- " + t.step_key + " (" + t.status + ", Runde " + t.round + ") ---");
            lines.push(JSON.stringify(t.result, null, 1));
          }
        }
      }
      return text(lines.join("\n"));
    },
  );
}
