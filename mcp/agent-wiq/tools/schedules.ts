import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { supabase, log, text, fail, describeError } from "./shared.ts";

/**
 * Stehende Aufgaben (Klasse B): ein change_item mit Zeitregel, das zu jedem
 * Termin EINEN Lauf erzeugt.
 *
 * Arbeitsteilung, bewusst:
 *   - Dieses Tool prueft die FORM (genau eine Zeitangabe, Zone, Uhrzeit,
 *     Wochentag, Mindestabstand) und schreibt Regel + Vorlagen-Workitem.
 *   - Die Sekretaerin besitzt die ZEIT. Sie stellt den ersten Termin scharf und
 *     materialisiert jedes Vorkommen. Deshalb schreibt dieses Tool NIE einen
 *     Termin — es gaebe sonst zwei Terminlogiken (TypeScript und Python), und
 *     genau daran scheitern Wartungssysteme.
 *
 * Eine unbrauchbare Regel (z.B. eine Uhrzeit, die es wegen Zeitumstellung nicht
 * gibt) erscheint nach dem naechsten Tick als state='failed' mit
 * schedule.error — sichtbar in schedule_list und im Dashboard, nicht nur im Log.
 *
 * Wie bei run_submit kennt der Server den Aufrufer nicht (geteilter Key im
 * internen Netz). Jede Session mit den wiq-Tools darf eine stehende Aufgabe
 * anlegen — bekanntes Grenze der v1.
 */

const MIN_EVERY_SECONDS = 300;

/** IANA-Zone pruefen: Intl wirft bei einem unbekannten Namen. */
function assertZone(zone: string): string | null {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: zone });
    return null;
  } catch {
    return "unbekannte Zeitzone '" + zone + "'";
  }
}

function assertClock(time: string): string | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(time.trim());
  if (!match) return "Uhrzeit '" + time + "' ist nicht HH:MM";
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (hour > 23 || minute > 59) return "Uhrzeit '" + time + "' liegt ausserhalb 00:00-23:59";
  return null;
}

export function registerScheduleTools(server: McpServer) {
  // ────────────────────────────────────────────────────────────────────────
  // schedule_create — eine stehende Aufgabe anlegen
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "schedule_create",
    {
      title: "Create a standing task",
      description:
        "Legt eine STEHENDE AUFGABE an: ein Auftrag, der verzoegert einmal oder " +
        "wiederkehrend ausgefuehrt wird. Jedes Vorkommen wird ein eigener Lauf, in dem EINE " +
        "Rolle den Prompt mit ihren MCP-Werkzeugen ausfuehrt.\n\n" +
        "Genau EINE Zeitangabe ist Pflicht:\n" +
        "  at              einmalig zu einem Zeitpunkt. Als Zeichenkette MIT Offset " +
        "(\"2026-09-24T18:00:00+02:00\") oder als {date, time, time_zone} — die zweite Form " +
        "ist sicherer, wenn du eine Uhrzeit in einer Zeitzone meinst.\n" +
        "  after_seconds   einmalig in N Sekunden.\n" +
        "  every=weekly    wiederkehrend, braucht time und weekday (0=Montag ... 6=Sonntag).\n" +
        "  every=daily     wiederkehrend, braucht time.\n" +
        "  every_seconds   feste Rate, mindestens 300 (jedes Vorkommen kostet Modell-Tokens).\n\n" +
        "Beispiele:\n" +
        '  "pruefe jeden Sonntag um 18:00, ob in den ueberwachten X-Posts etwas ueber Trump steht, ' +
        'und bewerte es semantisch" -> role=cco, every=weekly, weekday=6, time=18:00\n' +
        '  "erinnere mich am 24.09. um 18:00 an den DRAM-Preisindex" -> role=cco, at={date,time,time_zone}\n\n' +
        "WHEN TO USE: Wenn ein Auftrag zu einem Zeitpunkt oder wiederkehrend laufen soll.\n" +
        "WHEN NOT TO USE: Fuer einen sofortigen Lauf — dafuer ist run_submit da. Und NICHT fuer " +
        "Pruefungen, die eine Maschine sekundengenau auswerten muss (Kurs gegen Indikator) — " +
        "dafuer ist eine Wache (STM) da, kein wiederkehrender Modellauf.",
      inputSchema: {
        prompt: z.string().describe("Was ausgefuehrt wird — der Arbeitsauftrag in natuerlicher Sprache."),
        role: z.string().describe("Rolle, deren MCP-Werkzeuge der Lauf benutzt (z.B. 'cco', 'pca', 'cda')."),
        title: z.string().optional().describe("Kurzer Titel; sonst die erste Zeile des Prompts."),
        at: z.union([
          z.string().describe("Absoluter Zeitpunkt MIT Zeitzonen-Offset."),
          z.object({
            date: z.string().describe("ISO-Datum, z.B. 2026-09-24"),
            time: z.string().describe("Uhrzeit HH:MM"),
            time_zone: z.string().describe("IANA-Zone, z.B. Europe/Berlin"),
          }),
        ]).optional().describe("Einmalig, absoluter Zeitpunkt."),
        after_seconds: z.number().int().min(1).optional().describe("Einmalig, in N Sekunden."),
        every: z.enum(["daily", "weekly"]).optional().describe("Wiederkehrend, zur Uhrzeit."),
        time: z.string().optional().describe("Uhrzeit HH:MM fuer daily/weekly."),
        weekday: z.number().int().min(0).max(6).optional()
          .describe("0=Montag ... 6=Sonntag, nur fuer every='weekly'."),
        time_zone: z.string().optional()
          .describe("IANA-Zone der Uhrzeit. Default Europe/Berlin. NICHT die Zone des Marktes."),
        every_seconds: z.number().int().min(MIN_EVERY_SECONDS).optional()
          .describe("Feste Rate in Sekunden, Minimum 300."),
        max_runs: z.number().int().min(1).optional()
          .describe("Nach N Vorkommen automatisch beenden (sonst laeuft sie weiter)."),
        rounds: z.number().int().min(1).max(200).optional()
          .describe("Budget eines Vorkommens in Modell-Runden."),
        tokens: z.number().int().min(1000).optional()
          .describe("Budget eines Vorkommens in Tokens."),
        max_rounds: z.number().int().min(1).max(10).optional()
          .describe("Wie oft der Lead in einem Vorkommen nachplanen darf (1-10, Default 2)."),
      },
    },
    async ({ prompt, role, title, at, after_seconds, every, time, weekday,
             time_zone, every_seconds, max_runs, rounds, tokens, max_rounds }) => {
      const clean = prompt.trim();
      if (clean.length === 0) return fail("Der Prompt ist leer.");

      // ── genau eine Zeitangabe ───────────────────────────────────────────
      const specs = [at !== undefined, after_seconds !== undefined,
                     every !== undefined, every_seconds !== undefined].filter(Boolean).length;
      if (specs !== 1) {
        return fail("Genau EINE Zeitangabe noetig: at | after_seconds | every+daily/weekly | every_seconds. " +
                    "Gefunden: " + specs + ".");
      }

      const rule: Record<string, unknown> = {};
      if (at !== undefined) {
        rule.kind = "once";
        if (typeof at === "string") {
          const parsed = Date.parse(at);
          if (Number.isNaN(parsed)) return fail("at '" + at + "' ist kein lesbarer Zeitpunkt.");
          if (parsed <= Date.now()) return fail("at liegt in der Vergangenheit: " + at);
          rule.at = at;
        } else {
          const zoneError = assertZone(at.time_zone);
          if (zoneError) return fail(zoneError);
          const clockError = assertClock(at.time);
          if (clockError) return fail(clockError);
          if (!/^\d{4}-\d{2}-\d{2}$/.test(at.date.trim())) {
            return fail("date '" + at.date + "' ist nicht YYYY-MM-DD.");
          }
          rule.at = { date: at.date.trim(), time: at.time.trim(), time_zone: at.time_zone };
        }
      } else if (after_seconds !== undefined) {
        rule.kind = "once";
        rule.at = new Date(Date.now() + after_seconds * 1000).toISOString();
      } else if (every !== undefined) {
        if (!time) return fail("every='" + every + "' braucht time (HH:MM).");
        const clockError = assertClock(time);
        if (clockError) return fail(clockError);
        const zone = time_zone ?? "Europe/Berlin";
        const zoneError = assertZone(zone);
        if (zoneError) return fail(zoneError);
        rule.kind = "repeat";
        rule.every = every;
        rule.time = time.trim();
        rule.time_zone = zone;
        if (every === "weekly") {
          if (weekday === undefined) return fail("every='weekly' braucht weekday (0=Montag ... 6=Sonntag).");
          rule.weekday = weekday;
        } else if (weekday !== undefined) {
          return fail("weekday gehoert nur zu every='weekly'.");
        }
      } else {
        rule.kind = "repeat";
        rule.every_seconds = every_seconds;
      }
      if (max_runs !== undefined) rule.max_runs = max_runs;

      // ── Rolle muss existieren ───────────────────────────────────────────
      const roleRow = await supabase.from("roles").select("name,description")
        .eq("name", role).maybeSingle();
      if (roleRow.error) return fail("Rollen-Lesefehler: " + describeError(roleRow.error));
      if (!roleRow.data) {
        const all = await supabase.from("roles").select("name").order("name");
        const known = (all.data ?? []).map((r) => r.name as string).join(", ");
        return fail("Rolle '" + role + "' gibt es nicht. Bekannt: " + known);
      }

      const budget: Record<string, number> = {};
      if (rounds) budget.rounds = rounds;
      if (tokens) budget.tokens = tokens;

      // ── Definition + Vorlage. Kein halber Zustand: faellt die Vorlage aus,
      //    wird die Definition zurueckgenommen (Muster: run_submit). ────────
      const definition = await supabase
        .from("change_items")
        .insert({
          title: (title ?? clean.split("\n")[0]).slice(0, 120),
          entry_prompt: clean,
          state: "scheduled",
          is_template: true,
          schedule: rule,
          max_rounds: max_rounds ?? 2,
        })
        .select("id")
        .single();
      if (definition.error || !definition.data) {
        return fail("Definition konnte nicht angelegt werden: " + describeError(definition.error));
      }
      const definitionId = definition.data.id as string;

      const template = await supabase
        .from("workitems")
        .insert({
          change_item_id: definitionId,
          step_key: "aufgabe",
          type: "template",
          role,
          payload: { prompt: clean },
          budget,
          max_attempts: 2,
        })
        .select("id")
        .single();
      if (template.error || !template.data) {
        await supabase.from("change_items").delete().eq("id", definitionId);
        return fail("Vorlage konnte nicht angelegt werden (zurueckgenommen): "
                    + describeError(template.error));
      }

      log.info("schedule_create: " + definitionId + " role=" + role + " rule=" + JSON.stringify(rule));
      return text([
        "Stehende Aufgabe angelegt.",
        "change_item_id: " + definitionId,
        "regel:          " + JSON.stringify(rule),
        "rolle:          " + role,
        Object.keys(budget).length ? "budget:         " + JSON.stringify(budget) : "budget:         (Policy-Defaults)",
        "",
        "Die Sekretaerin stellt den ersten Termin beim naechsten Tick (max. 2 s) scharf.",
        "Kontrolle: schedule_list. Ist die Regel unbrauchbar, steht sie dort als failed",
        "mit schedule.error — sie laeuft dann nicht.",
      ].join("\n"));
    },
  );

  // ────────────────────────────────────────────────────────────────────────
  // schedule_list — stehende Aufgaben ansehen
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "schedule_list",
    {
      title: "List standing tasks",
      description:
        "Zeigt alle stehenden Aufgaben: Zustand, Regel, naechster Termin, wie viele Vorkommen " +
        "schon gelaufen sind und ob das letzte Vorkommen offen ist.\n\n" +
        "WHEN TO USE: Nach schedule_create zur Kontrolle; wenn der Boss wissen will, was " +
        "periodisch laeuft.",
      inputSchema: {
        include_cancelled: z.boolean().optional()
          .describe("Auch abgeschaltete und beendete zeigen. Default true."),
      },
    },
    async ({ include_cancelled }) => {
      let query = supabase
        .from("change_items")
        .select("id,title,state,schedule,next_run_at,last_run_at,run_count,created_at")
        .eq("is_template", true)
        .order("created_at", { ascending: false })
        .limit(100);
      if (include_cancelled === false) query = query.eq("state", "scheduled");

      const { data, error } = await query;
      if (error) return fail("Lesefehler: " + describeError(error));
      const rows = (data ?? []) as Record<string, unknown>[];
      if (rows.length === 0) return text("Keine stehenden Aufgaben.");

      const ids = rows.map((r) => r.id as string);
      const { data: instances } = await supabase
        .from("change_items")
        .select("source_change_item_id,state")
        .in("source_change_item_id", ids);
      const counts = new Map<string, number>();
      for (const i of (instances ?? []) as Record<string, unknown>[]) {
        const key = i.source_change_item_id as string;
        counts.set(key, (counts.get(key) ?? 0) + 1);
      }

      const lines = [
        "id".padEnd(38) + "zustand".padEnd(11) + "vorkommen".padStart(9) + "  naechster".padEnd(19) + "regel",
      ];
      for (const row of rows) {
        const rule = (row.schedule ?? {}) as Record<string, unknown>;
        const next = row.next_run_at
          ? new Date(row.next_run_at as string).toLocaleString("de-DE", { timeZone: "Europe/Berlin" })
          : "-";
        const ruleText = rule.error ? "FEHLER: " + String(rule.error) : JSON.stringify(rule);
        lines.push(
          String(row.id).padEnd(38) + String(row.state).padEnd(11)
          + String(counts.get(row.id as string) ?? 0).padStart(9) + "  "
          + next.padEnd(19) + ruleText,
        );
        lines.push("  " + String(row.title).slice(0, 100));
      }
      lines.push("", "Zustand: scheduled = aktiv | done/failed/cancelled = beendet.");
      lines.push("Details eines Vorkommens: run_status mit change_item_id des Vorkommens.");
      return text(lines.join("\n"));
    },
  );

  // ────────────────────────────────────────────────────────────────────────
  // schedule_cancel — abschalten
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "schedule_cancel",
    {
      title: "Cancel a standing task",
      description:
        "Schaltet eine stehende Aufgabe ab: kein weiterer Termin. Ein gerade laufendes " +
        "Vorkommen laeuft zu Ende — es ist ein eigener Lauf.\n\n" +
        "WHEN TO USE: Wenn der Boss eine Wiederholung beenden will.",
      inputSchema: {
        change_item_id: z.string().describe("UUID der stehenden Aufgabe aus schedule_create."),
      },
    },
    async ({ change_item_id }) => {
      const { data, error } = await supabase
        .from("change_items")
        .select("id,title,state,is_template")
        .eq("id", change_item_id)
        .maybeSingle();
      if (error) return fail("Lesefehler: " + describeError(error));
      if (!data) return fail("Unbekannte id " + change_item_id + ".");
      if (!data.is_template) return fail("Das ist keine stehende Aufgabe, sondern ein Lauf.");
      if (data.state !== "scheduled") {
        return text("Bereits beendet (state=" + data.state + ") — nichts geaendert.");
      }
      const updated = await supabase
        .from("change_items")
        .update({ state: "cancelled", next_run_at: null, finished_at: new Date().toISOString() })
        .eq("id", change_item_id)
        .eq("state", "scheduled")
        .select("id")
        .maybeSingle();
      if (updated.error) return fail("Abschalten fehlgeschlagen: " + describeError(updated.error));
      if (!updated.data) return text("Zustand hat sich waehrend des Schreibens geaendert — erneut lesen.");
      log.info("schedule_cancel: " + change_item_id);
      return text("Abgeschaltet: '" + String(data.title) + "' bekommt keinen Termin mehr.");
    },
  );

  // ────────────────────────────────────────────────────────────────────────
  // schedule_run — sofort einmal ausfuehren
  // ────────────────────────────────────────────────────────────────────────
  server.registerTool(
    "schedule_run",
    {
      title: "Run a standing task now",
      description:
        "Zieht den Termin einer stehenden Aufgabe auf jetzt. Das Vorkommen startet beim " +
        "naechsten Tick; die Regel bleibt sonst unveraendert (der uebernaechste Termin ist " +
        "wieder der regulaere).\n\n" +
        "WHEN TO USE: Um eine stehende Aufgabe sofort zu testen, ohne auf ihren Termin zu warten.",
      inputSchema: {
        change_item_id: z.string().describe("UUID der stehenden Aufgabe."),
      },
    },
    async ({ change_item_id }) => {
      const { data, error } = await supabase
        .from("change_items")
        .select("id,title,state,is_template,next_run_at")
        .eq("id", change_item_id)
        .maybeSingle();
      if (error) return fail("Lesefehler: " + describeError(error));
      if (!data) return fail("Unbekannte id " + change_item_id + ".");
      if (!data.is_template) return fail("Das ist keine stehende Aufgabe, sondern ein Lauf.");
      if (data.state !== "scheduled") return fail("Nicht aktiv (state=" + data.state + ").");
      if (!data.next_run_at) {
        return fail("Noch nicht scharfgestellt — die Sekretaerin setzt den ersten Termin beim " +
                    "naechsten Tick (max. 2 s). Gleich erneut versuchen.");
      }
      const updated = await supabase
        .from("change_items")
        .update({ next_run_at: new Date().toISOString() })
        .eq("id", change_item_id)
        .eq("state", "scheduled")
        .select("id")
        .maybeSingle();
      if (updated.error) return fail("Termin ziehen fehlgeschlagen: " + describeError(updated.error));
      if (!updated.data) return text("Zustand hat sich waehrend des Schreibens geaendert — erneut lesen.");
      return text("Termin gezogen: '" + String(data.title) + "' startet beim naechsten Tick.");
    },
  );
}
