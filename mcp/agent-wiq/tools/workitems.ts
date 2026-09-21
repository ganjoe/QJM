import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { supabase, log, text, fail, describeError, MAX_RESULT_CHARS } from "./shared.ts";

/**
 * Die vier Tools, mit denen ein Agent am Workitem-Graphen teilnimmt.
 *
 * Bewusste Grenzen:
 *  - Kein Unblock-Tool und kein Status-Setzen an fremden Items. Bereitschaft ist
 *    abgeleitet (View ready_workitems), Unblock ist Sache der Sekretaerin.
 *  - Der Server kennt den Aufrufer nicht (geteilter Key im internen Netz). Die
 *    Rollenpruefung fuer workitem_create ist eine TYPPRUEFUNG am Parent, keine
 *    Identitaetspruefung. Bekannte Grenze der v1.
 */

type WorkitemRow = {
  id: string;
  change_item_id: string;
  step_key: string;
  type: string;
  role: string;
  parent_id: string | null;
  payload: unknown;
  status: string;
  result: unknown;
  attempts: number;
  max_attempts: number;
  priority: number;
  round: number;
  budget: unknown;
  usage: unknown;
};

const WORKITEM_COLUMNS =
  "id,change_item_id,step_key,type,role,parent_id,payload,status,result,attempts,max_attempts,priority,round,budget,usage";

const TERMINAL = ["done", "failed", "skipped", "cancelled"];

/** Ein Item in wenigen Zeilen, damit der Agent schnell lesen kann. */
function renderItem(row: WorkitemRow): string {
  const parts = [
    row.step_key + " [" + row.type + "/" + row.role + "] status=" + row.status,
    "  id=" + row.id,
  ];
  if (row.attempts > 0) parts.push("  attempts=" + row.attempts + "/" + row.max_attempts + " round=" + row.round);
  return parts.join("\n");
}

export function registerWorkitemTools(server: McpServer) {
  // ────────────────────────────────────────────────────────────────────────
  // workitem_get — das eigene Item lesen
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "workitem_get",
    {
      title: "Read one workitem",
      description:
        "Liest ein Workitem samt seinen Kanten (depends_on = Gate, context = Lese-Kante).\n\n" +
        "WHEN TO USE: Zu Beginn eines Auftrags, um den eigenen Payload und die Vorgaenger zu sehen.\n" +
        "WHEN NOT TO USE: Um Ergebnisse von Vorgaengern zu holen — dafuer workitem_results.",
      inputSchema: {
        workitem_id: z.string().describe("UUID des Workitems."),
      },
    },
    async ({ workitem_id }) => {
      const { data, error } = await supabase
        .from("workitems")
        .select(WORKITEM_COLUMNS)
        .eq("id", workitem_id)
        .maybeSingle();
      if (error) return fail("workitems-Lesefehler: " + describeError(error));
      if (!data) return fail("Workitem " + workitem_id + " existiert nicht.");

      const { data: links, error: linkError } = await supabase
        .from("workitem_links")
        .select("to_id,kind")
        .eq("from_id", workitem_id);
      if (linkError) return fail("Kanten-Lesefehler: " + describeError(linkError));

      const targets = (links ?? []).map((l) => l.to_id as string);
      let related: WorkitemRow[] = [];
      if (targets.length > 0) {
        const { data: rows, error: relError } = await supabase
          .from("workitems")
          .select(WORKITEM_COLUMNS)
          .in("id", targets);
        if (relError) return fail("Vorgaenger-Lesefehler: " + describeError(relError));
        related = (rows ?? []) as WorkitemRow[];
      }
      const byId = new Map(related.map((r) => [r.id, r]));

      const lines = [
        renderItem(data as WorkitemRow),
        "  change_item_id=" + (data as WorkitemRow).change_item_id,
        "",
        "payload_json:",
        JSON.stringify((data as WorkitemRow).payload ?? {}),
        "",
        "budget_json (Soll, wird hart durchgesetzt):",
        JSON.stringify((data as WorkitemRow).budget ?? {}),
      ];
      if ((links ?? []).length > 0) {
        lines.push("", "Kanten (dieses Item braucht das Ziel):");
        for (const l of links ?? []) {
          const target = byId.get(l.to_id as string);
          lines.push("  " + l.kind + ": " + (target ? target.step_key + " [" + target.status + "]" : l.to_id));
        }
      }
      return text(lines.join("\n"));
    },
  );

  // ────────────────────────────────────────────────────────────────────────
  // workitem_results — Kontext-Pull
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "workitem_results",
    {
      title: "Pull predecessor results",
      description:
        "Holt die Ergebnisse benannter Workitems. Das ist der Kontext-Pull: ein abhaengiges Item " +
        "zieht sich die Resultate seiner Vorgaenger, statt dass sie ihm geschickt werden.\n\n" +
        "WHEN TO USE: Wenn dein payload auf context-Kanten verweist und du deren Ergebnisse brauchst.\n" +
        "WHEN NOT TO USE: Fuer dein eigenes Item — nutze workitem_get.",
      inputSchema: {
        workitem_ids: z.array(z.string()).min(1).max(20).describe("UUIDs der Vorgaenger."),
      },
    },
    async ({ workitem_ids }) => {
      const { data, error } = await supabase
        .from("workitems")
        .select(WORKITEM_COLUMNS)
        .in("id", workitem_ids);
      if (error) return fail("workitems-Lesefehler: " + describeError(error));

      const found = new Map((data ?? []).map((r) => [r.id as string, r as WorkitemRow]));
      const blocks: string[] = [];
      for (const id of workitem_ids) {
        const row = found.get(id);
        if (!row) {
          blocks.push("### " + id + "\nFEHLT (unbekannte id)");
          continue;
        }
        const usage = (row.usage ?? {}) as Record<string, unknown>;
        const budget = (row.budget ?? {}) as Record<string, unknown>;
        const spent = Object.keys(usage).length > 0
          ? "verbraucht: " + JSON.stringify(usage)
          : "verbraucht: (nicht erfasst)";
        const planned = Object.keys(budget).length > 0
          ? "budget: " + JSON.stringify(budget)
          : "budget: (keins gesetzt)";
        blocks.push([
          "### " + row.step_key + " [" + row.type + "/" + row.role + "] status=" + row.status,
          "  " + planned,
          "  " + spent,
          "result_json: " + JSON.stringify(row.result ?? null),
        ].join("\n"));
      }
      return text(blocks.join("\n\n"));
    },
  );

  // ────────────────────────────────────────────────────────────────────────
  // workitem_create — nur fuer Planer (initial) und Reviewer
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "workitem_create",
    {
      title: "Create child workitems",
      description:
        "Legt Kind-Items im selben Lauf an. NUR ein Item vom Typ initial oder review darf das; " +
        "ein initial-Item plant Runde 1, ein review-Item plant die naechste Runde.\n\n" +
        "depends_on und context_refs nennen STEP_KEYS, keine UUIDs. depends_on ist ein Gate " +
        "(Vorgaenger muss erfolgreich sein), context_refs sind Lese-Kanten (Ergebnis wird per " +
        "workitem_results gezogen). Eine Kette check -> confirm -> act wird ueber depends_on " +
        "ausgedrueckt; bleibt check erfolglos, werden die Folgenden automatisch uebersprungen.\n\n" +
        "WHEN TO USE: Als Planer oder Reviewer, um Arbeit zu strukturieren.\n" +
        "WHEN NOT TO USE: Als ausfuehrender Worker — du erledigst genau dein Item.",
      inputSchema: {
        workitem_id: z.string().describe("UUID DEINES Items (der Parent)."),
        items: z.array(z.object({
          step_key: z.string().describe("Stabile Knotenidentitaet, z.B. 'analyse-charts'."),
          type: z.enum(["task", "review"]).describe("task = ausfuehren, review = bewerten."),
          role: z.string().describe("Rollenname, z.B. 'cco' oder 'lead_engineer'."),
          payload: z.record(z.string(), z.unknown()).optional().describe("Arbeitsauftrag (Prompt + Parameter)."),
          depends_on: z.array(z.string()).optional().describe("step_keys, die ERFOLGREICH sein muessen."),
          context_refs: z.array(z.string()).optional().describe("step_keys, deren Ergebnis gelesen wird."),
          priority: z.number().int().optional(),
          max_attempts: z.number().int().min(1).max(5).optional(),
          budget: z.object({
            rounds: z.number().int().min(1).max(200).optional()
              .describe("Erwartete Modell-Runden. Die ehrlichste Metrik — schaetze sie aus der Aufgabengroesse."),
            tokens: z.number().int().min(1000).optional()
              .describe("Erwartete Tokens ueber alle Runden. Grosszuegig schaetzen."),
            seconds: z.number().int().min(10).optional()
              .describe("Wanduhr-Obergrenze als Notausstieg. Misst auch Wartezeit — grosszuegig."),
          }).optional().describe("Geschaetztes Budget fuer dieses Item. Wird dem Agenten gezeigt und hart durchgesetzt."),
        })).min(1).max(50).describe("Die anzulegenden Items."),
      },
    },
    async ({ workitem_id, items }) => {
      const { data: parent, error: parentError } = await supabase
        .from("workitems")
        .select(WORKITEM_COLUMNS)
        .eq("id", workitem_id)
        .maybeSingle();
      if (parentError) return fail("Parent-Lesefehler: " + describeError(parentError));
      if (!parent) return fail("Parent " + workitem_id + " existiert nicht.");
      const p = parent as WorkitemRow;
      if (p.type !== "initial" && p.type !== "review") {
        return fail("Nur initial- oder review-Items duerfen planen. " + p.step_key + " ist vom Typ '" + p.type + "'.");
      }

      // Ein review plant die naechste Runde, ein initial die erste.
      const round = p.type === "review" ? p.round + 1 : p.round;

      // Jede Runde braucht genau ein review. Ohne das entscheidet
      // finalize_change_items() mit der Bewertung der VORIGEN Runde — ein Lauf,
      // der in Runde 2 alles liefert, wuerde trotzdem als failed enden.
      if (!items.some((i) => i.type === "review")) {
        return fail(
          "Runde " + round + " ohne review-Item. Jede Runde braucht ein review, " +
          "sonst bewertet finalize_change_items() den Lauf mit dem Ergebnis der vorigen Runde."
        );
      }

      const newKeys = items.map((i) => i.step_key);
      if (new Set(newKeys).size !== newKeys.length) {
        return fail("step_key doppelt innerhalb des Aufrufs.");
      }
      for (const i of items) {
        if ((i.depends_on ?? []).includes(i.step_key) || (i.context_refs ?? []).includes(i.step_key)) {
          return fail(i.step_key + " verweist auf sich selbst.");
        }
      }

      const { data: existing, error: existError } = await supabase
        .from("workitems")
        .select("step_key")
        .eq("change_item_id", p.change_item_id)
        .eq("round", round);
      if (existError) return fail("Bestandslesefehler: " + describeError(existError));
      const existingKeys = new Set((existing ?? []).map((r) => r.step_key as string));

      for (const key of newKeys) {
        if (existingKeys.has(key)) {
          return fail("step_key '" + key + "' existiert in Runde " + round + " bereits.");
        }
      }
      const known = new Set([...existingKeys, ...newKeys]);
      for (const i of items) {
        for (const ref of [...(i.depends_on ?? []), ...(i.context_refs ?? [])]) {
          if (!known.has(ref)) {
            return fail(
              i.step_key + " verweist auf '" + ref + "', das es in Runde " + round + " nicht gibt. " +
              "Bekannt: " + ([...known].sort().join(", ") || "(nichts)"),
            );
          }
        }
      }

      const { data: created, error: insertError } = await supabase
        .from("workitems")
        .insert(items.map((i) => ({
          change_item_id: p.change_item_id,
          step_key: i.step_key,
          type: i.type,
          role: i.role,
          parent_id: p.id,
          payload: i.payload ?? {},
          priority: i.priority ?? 0,
          max_attempts: i.max_attempts ?? 2,
          budget: i.budget ?? {},
          round,
        })))
        .select("id,step_key");
      if (insertError) return fail("Anlegen fehlgeschlagen: " + describeError(insertError));

      const idByKey = new Map((created ?? []).map((c) => [c.step_key as string, c.id as string]));

      const { data: allRows, error: allError } = await supabase
        .from("workitems")
        .select("id,step_key")
        .eq("change_item_id", p.change_item_id)
        .eq("round", round);
      if (allError) return fail("Kanten-Aufloesung fehlgeschlagen: " + describeError(allError));
      const resolve = new Map((allRows ?? []).map((r) => [r.step_key as string, r.id as string]));

      const linkRows: { from_id: string; to_id: string; kind: string }[] = [];
      for (const i of items) {
        const fromId = idByKey.get(i.step_key);
        if (!fromId) continue;
        for (const ref of i.depends_on ?? []) {
          linkRows.push({ from_id: fromId, to_id: resolve.get(ref)!, kind: "depends_on" });
        }
        for (const ref of i.context_refs ?? []) {
          linkRows.push({ from_id: fromId, to_id: resolve.get(ref)!, kind: "context" });
        }
      }
      if (linkRows.length > 0) {
        const { error: linkInsertError } = await supabase.from("workitem_links").insert(linkRows);
        if (linkInsertError) {
          // Ohne Kanten ist der Graph falsch — lieber die Items zuruecknehmen als Halbzustand lassen.
          await supabase.from("workitems").delete().in("id", [...idByKey.values()]);
          return fail("Kanten anlegen fehlgeschlagen (Items zurueckgenommen): " + describeError(linkInsertError));
        }
      }

      log.info("create: parent=" + p.step_key + " round=" + round + " items=" + newKeys.join(","));
      return text([
        "Angelegt in Runde " + round + ":",
        ...items.map((i) => "  " + i.step_key + " [" + i.type + "/" + i.role + "] id=" + idByKey.get(i.step_key)),
        "",
        "Kanten: " + linkRows.length,
      ].join("\n"));
    },
  );

  // ────────────────────────────────────────────────────────────────────────
  // workitem_finish — der Worker schliesst sein eigenes Item ab
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "workitem_finish",
    {
      title: "Finish your workitem",
      description:
        "Schliesst DEIN Workitem ab. Nur aus dem Zustand running moeglich; ein zweiter Aufruf " +
        "aendert nichts und meldet das.\n\n" +
        "WHEN TO USE: Genau einmal am Ende deines Auftrags.\n" +
        "WHEN NOT TO USE: Um fremde Items zu schliessen. Das Ergebnis gehoert in result, nicht in " +
        "eine Chat-Antwort — die Sekretaerin liest die Datenbank, nicht deinen Verlauf.",
      inputSchema: {
        workitem_id: z.string().describe("UUID DEINES Workitems."),
        status: z.enum(["done", "failed"]).describe("done = Auftrag erfuellt, failed = erfuellt ihn nicht."),
        result: z.record(z.string(), z.unknown()).optional().describe("Das Arbeitsergebnis als JSON."),
      },
    },
    async ({ workitem_id, status, result }) => {
      const { data, error } = await supabase
        .from("workitems")
        .select("id,step_key,status,type,change_item_id")
        .eq("id", workitem_id)
        .maybeSingle();
      if (error) return fail("Lesefehler: " + describeError(error));
      if (!data) return fail("Workitem " + workitem_id + " existiert nicht.");

      const current = data.status as string;
      if (TERMINAL.includes(current)) {
        return text("Bereits abgeschlossen (status=" + current + ") — nichts geaendert.");
      }
      if (current !== "running") {
        return fail(
          "Item ist '" + current + "', nicht 'running'. Ein Item wird erst durch die Sekretaerin " +
          "gestartet; melde dich nicht selbst an.",
        );
      }

      const payload = result ?? {};
      const size = JSON.stringify(payload).length;
      if (size > MAX_RESULT_CHARS) {
        return fail("result ist " + size + " Zeichen gross, erlaubt sind " + MAX_RESULT_CHARS + ". Kuerzen.");
      }

      const { data: updated, error: updateError } = await supabase
        .from("workitems")
        // Der Lease gehoert zum Zustand running und muss mit ihm enden —
        // sonst verletzt die Zeile workitems_lease_check.
        .update({
          status,
          result: payload,
          finished_at: new Date().toISOString(),
          lease_owner: null,
          lease_expires_at: null,
        })
        .eq("id", workitem_id)
        .eq("status", "running")        // Vergleich-und-Setze: kein Ueberschreiben eines Renntags
        .select("id,status")
        .maybeSingle();
      if (updateError) return fail("Abschluss fehlgeschlagen: " + describeError(updateError));
      if (!updated) return fail("Zustand hat sich waehrend des Schreibens geaendert — erneut lesen.");

      log.info("finish: " + workitem_id + " step=" + data.step_key + " status=" + status + " bytes=" + size);
      return text("Abgeschlossen: " + data.step_key + " -> " + status + " (" + size + " Zeichen Ergebnis).");
    },
  );
}
