import { z } from "zod";
import { log } from "./shared.ts";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

const PCA_SERVICE_URL = Deno.env.get("PCA_SERVICE_URL") || "http://qjm-pca-service:8794";

export function registerPresetTools(server: McpServer) {
    server.registerTool(
        "manage_chart_presets",
        {
            title: "Manage Chart Presets",
            description:
                "Create, read, update, or delete chart presets. Ein Chart-Preset ist der Inhalt EINES FENSTERS: " +
                "geordnete Panes (Verweise auf Pane-Presets), Topbar-Metriken und die X-Achsen-Pane. " +
                "Die Inhalte der einzelnen Panes (Serien, Regeln, Zonen, Referenzlinien) liegen in Pane-Presets " +
                "und werden mit `manage_pane_presets` verwaltet.\n\n" +
                "EBENEN: Setup = Fenster-Slots + Geometrie | Chart-Preset = geordnete Panes + Topbar + x_axis_pane | " +
                "Pane-Preset = Serien + Regeln.\n\n" +
                "ACTIONS:\n" +
                "- LIST: alle Chart-Presets kompakt -> GET /api/charts.\n" +
                "- GET: ein Chart-Preset inkl. aufgeloester Panes/Serien -> GET /api/charts/{id}. Erfordert `preset_id`.\n" +
                "- CREATE: neues Chart-Preset -> POST /api/charts. Erfordert `preset_id` und `panes\".\n" +
                "- UPDATE: Chart-Preset aendern -> PUT /api/charts/{id} (nur uebergebene Felder werden geschrieben).\n" +
                "- DELETE: Chart-Preset loeschen -> DELETE /api/charts/{id}.\n" +
                "- SET_PANE_SCALE: Y-Skalierung eines Panes setzen -> PATCH /api/charts/{id}/pane_scales " +
                "(erfordert `pane` und optional `scale\").\n\n" +
                "NEUE STRUKTUR (`panes`): [{pane_id, pane_preset_id, sort_order, weight, scale, overrides}]. " +
                "`pane_id` ist die Slot-Id im Fenster ('main', 'rs', 'volume'); fehlt sie, nimmt der Service die " +
                "Pane-Preset-Id (bei Kollision mit Suffix '_2'). Genau ein Pane hat role=price und pane_id='main'; " +
                "fehlt eines, wird das erste Pane zum Preispane. 'builtin:volume' ist ein virtuelles Pane " +
                "(eingebautes Volumen-Histogramm, kein DB-Eintrag). Die Pane-Liste ist VOLLSTAENDIG: ein " +
                "fehlendes builtin:volume wird NICHT ergaenzt (sonst waere es nicht entfernbar); nur die " +
                "flache Altform (members) bekommt es bei der Uebersetzung explizit.\n\n" +
                "KOMPATIBILITAET (Altform): Wird `members` ohne `panes` geschickt, geht der Request an die alte " +
                "flache API /api/presets (Adapter auf dieselben Tabellen); `members` (inkl. `pane`-String), " +
                "`topbar_metrics` und `pane_scales` funktionieren dort unveraendert weiter. Sobald `panes` gesetzt " +
                "ist, gilt die neue Struktur; ein mitgeschicktes `pane_scales` wird dann auf die Pane-Eintraege " +
                "abgebildet, und `pane_scales` ohne `panes` wird per PATCH /api/charts/{id}/pane_scales nachgezogen.\n\n" +
                "WHEN TO USE: wenn ein Fensterinhalt (welche Panes, welche Reihenfolge/Gewichte, welche Topbar) " +
                "angelegt oder geaendert werden soll — oder um zu sehen, welche Chart-Presets es gibt.\n" +
                "WHEN NOT TO USE: fuer einzelne Panes/Serien (manage_pane_presets) und fuer das Anzeigen von " +
                "Charts (manage_chart_viewer).",
            inputSchema: {
                action: z.enum(["CREATE", "GET", "LIST", "UPDATE", "DELETE", "SET_PANE_SCALE"]).describe("The operation to perform."),
                preset_id: z.string().optional().describe("ID des Chart-Presets (z. B. 'qmaggi', 'momentum'). Required for CREATE, GET, UPDATE, DELETE."),
                display_name: z.string().optional().describe("Human readable name for the preset. Used in CREATE and UPDATE."),
                description: z.string().optional().describe("Description of the preset. Used in CREATE and UPDATE."),
                x_axis_pane: z.string().optional().describe(
                    "Pane-Id, die die X-Achse (Zeitachse) traegt, z. B. 'main' oder 'rs'. Leer/null = das unterste Pane. Used in CREATE and UPDATE."
                ),
                topbar_metrics: z.array(z.string()).optional().describe("List of topbar metrics (e.g. 'ibd_rs', 'minervini'). Used in CREATE and UPDATE."),
                panes: z.array(
                    z.object({
                        pane_id: z.string().optional().describe("Slot-Id im Fenster ('main', 'rs', 'volume', ...). Fehlt sie, nimmt der Service die pane_preset_id (Kollision -> Suffix '_2')."),
                        pane_preset_id: z.string().describe("Id des Pane-Presets (pca_pane_presets.id) oder 'builtin:volume'."),
                        sort_order: z.number().optional().describe("Reihenfolge im Fenster (Default: Array-Index)."),
                        weight: z.number().optional().describe("Relative Hoehe des Panes (Default 2; Preispane typischerweise 7)."),
                        scale: z.enum(["linear", "log"]).optional().describe("Y-Achsen-Skalierung dieses Panes (Default 'linear')."),
                        overrides: z.record(z.string(), z.any()).optional().describe("Pane-spezifische Ueberschreibungen (Titel, Style).")
                    })
                ).optional().describe("Geordnete Panes des Fensters (neue Struktur, /api/charts). Used in CREATE and UPDATE."),
                members: z.array(
                    z.object({
                        feature_id: z.string().describe("The canonical feature ID (e.g. 'sma_10', 'ema_20', 'bb_20', 'adr_1_pct', 'adr_20_sma')."),
                        sort_order: z.number().default(0).describe("Order in the overlay list."),
                        style_override: z.record(z.string(), z.any()).optional().describe("Style overrides (e.g. { color: '#FF00FF', width: 2 })."),
                        pane: z.string().optional().describe("Target pane: 'main' = Overlay im Chartfenster, beliebiger Name (z. B. 'rs') = eigene Subpane mit eigener Y-Achse, 'none' = nur Topbar-Metrik. Weglassen = aus plot_type des Features ableiten.")
                    })
                ).optional().describe("Altform: flache Indikatorliste. Wird ohne 'panes' an /api/presets geschickt (Kompatibilitaet). Used in CREATE and UPDATE."),
                pane_scales: z.record(z.string(), z.enum(["linear", "log"])).optional().describe(
                    "Y-Achsen-Skalierung je Pane, z. B. { main: 'log', volume: 'linear' }. Panes ohne Eintrag sind linear. " +
                    "In der neuen Struktur wird das auf die 'panes'-Eintraege (scale) abgebildet. " +
                    "Wird ueblicherweise vom Chart-Viewer selbst gesetzt (LOG/LIN-Taste im Pane). Used in CREATE and UPDATE."
                ),
                pane: z.string().optional().describe("Pane-ID fuer action=SET_PANE_SCALE, z. B. 'main', 'volume', 'rs'."),
                scale: z.enum(["linear", "log"]).optional().describe("Ziel-Skalierung fuer action=SET_PANE_SCALE (Default 'linear').")
            }
        },
        async ({ action, preset_id, display_name, description, x_axis_pane, topbar_metrics, panes, members, pane_scales, pane, scale }: any) => {
            // Kompatibilitaetsweg (Altverhalten): flache Mitgliederliste ohne panes
            // => alte API /api/presets. Sonst gilt die neue Chart-Struktur /api/charts.
            const legacy = panes === undefined && members !== undefined;
            const api = legacy ? "presets" : "charts";

            if (action === "LIST") {
                log.info(`[manage_chart_presets] LIST ${api}`);
                const res = await fetch(`${PCA_SERVICE_URL}/api/${api}`);
                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{ type: "text", text: JSON.stringify(await res.json(), null, 2) }]
                };
            }

            if (!preset_id) throw new Error("preset_id is required for this action.");

            if (action === "SET_PANE_SCALE") {
                if (!pane) throw new Error("pane is required for SET_PANE_SCALE.");
                log.info(`[manage_chart_presets] SET_PANE_SCALE ${preset_id}: ${pane} -> ${scale || "linear"} (${api})`);
                const res = await fetch(`${PCA_SERVICE_URL}/api/${api}/${encodeURIComponent(preset_id)}/pane_scales`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ pane_scales: { [pane]: scale || "linear" } })
                });
                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{ type: "text", text: JSON.stringify(await res.json(), null, 2) }]
                };
            }

            if (action === "GET") {
                log.info(`[manage_chart_presets] GET ${api} ${preset_id}`);
                const res = await fetch(`${PCA_SERVICE_URL}/api/${api}/${encodeURIComponent(preset_id)}`);
                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{ type: "text", text: JSON.stringify(await res.json(), null, 2) }]
                };
            }

            if (action === "DELETE") {
                log.info(`[manage_chart_presets] DELETE ${api} ${preset_id}`);
                const res = await fetch(`${PCA_SERVICE_URL}/api/${api}/${encodeURIComponent(preset_id)}`, { method: "DELETE" });
                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{ type: "text", text: `Chart preset ${preset_id} successfully deleted.` }]
                };
            }

            if (action === "CREATE" || action === "UPDATE") {
                if (action === "CREATE" && panes === undefined && members === undefined) {
                    throw new Error("panes array (or legacy members array) is required for CREATE");
                }

                log.info(`[manage_chart_presets] ${action} ${api} ${preset_id}`);
                const payload: Record<string, unknown> = { id: preset_id };

                if (action === "CREATE") {
                    payload.display_name = display_name || preset_id;
                    payload.description = description || "";
                    payload.topbar_metrics = topbar_metrics || [];
                } else {
                    // UPDATE ist partiell: nur ausdruecklich uebergebene Felder schreiben.
                    if (display_name !== undefined) payload.display_name = display_name;
                    if (description !== undefined) payload.description = description;
                    if (topbar_metrics !== undefined) payload.topbar_metrics = topbar_metrics;
                }
                if (x_axis_pane !== undefined) payload.x_axis_pane = x_axis_pane;

                if (legacy) {
                    payload.members = members.map((m: any, idx: number) => ({
                        feature_id: m.feature_id,
                        sort_order: m.sort_order ?? idx,
                        style_override: m.style_override || {},
                        pane: m.pane ?? null
                    }));
                    // Nur senden, wenn ausdruecklich angegeben: ein UPDATE ohne
                    // pane_scales darf die im Viewer gesetzten Y-Skalen nicht loeschen.
                    if (pane_scales) payload.pane_scales = pane_scales;
                } else if (panes !== undefined) {
                    payload.panes = panes.map((p: any, idx: number) => {
                        const entry: Record<string, unknown> = {
                            pane_preset_id: p.pane_preset_id,
                            sort_order: p.sort_order ?? idx
                        };
                        if (p.pane_id !== undefined) entry.pane_id = p.pane_id;
                        if (p.weight !== undefined) entry.weight = p.weight;
                        if (p.overrides !== undefined) entry.overrides = p.overrides;
                        const mappedScale = p.scale ?? (pane_scales ? pane_scales[p.pane_id ?? p.pane_preset_id] : undefined);
                        if (mappedScale !== undefined) entry.scale = mappedScale;
                        return entry;
                    });
                }

                const url = action === "CREATE"
                    ? `${PCA_SERVICE_URL}/api/${api}`
                    : `${PCA_SERVICE_URL}/api/${api}/${encodeURIComponent(preset_id)}`;
                const method = action === "CREATE" ? "POST" : "PUT";

                const res = await fetch(url, {
                    method,
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });

                if (!res.ok) throw new Error(`API error: ${await res.text()}`);

                // pane_scales ohne panes: dafuer gibt es den PATCH-Endpunkt (kein stiller Datenverlust).
                if (!legacy && panes === undefined && pane_scales && Object.keys(pane_scales).length > 0) {
                    const patchRes = await fetch(`${PCA_SERVICE_URL}/api/charts/${encodeURIComponent(preset_id)}/pane_scales`, {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ pane_scales })
                    });
                    if (!patchRes.ok) throw new Error(`API error (pane_scales): ${await patchRes.text()}`);
                }

                return {
                    content: [{
                        type: "text",
                        text: `Chart preset ${preset_id} successfully ${action === "CREATE" ? "created" : "updated"}` +
                            `${legacy ? " (legacy /api/presets)" : " (/api/charts)"}.`
                    }]
                };
            }

            throw new Error("Invalid action.");
        }
    );
}
