import { z } from "zod";
import { log } from "./shared.ts";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

const PCA_SERVICE_URL = Deno.env.get("PCA_SERVICE_URL") || "http://qjm-pca-service:8794";

/**
 * Pane-Presets = der Inhalt EINES Panes (Rolle, Serien, Regeln, Zonen,
 * Referenzlinien). Chart-Presets (manage_chart_presets) verweisen per
 * pane_preset_id darauf; siehe docs/architecture/chart-presets.md.
 */
export function registerPaneTools(server: McpServer) {
    server.registerTool(
        "manage_pane_presets",
        {
            title: "Manage Pane Presets",
            description:
                "Create, read, update, or delete pane presets. Ein Pane-Preset ist der Inhalt EINES Panes: " +
                "Rolle (price|value|volume|any), Serien/Mitglieder (Features mit Style und Regeln), " +
                "Referenzlinien (refs), abgeleitete Serien (derives), Farbzonen (zones), Default-Skala und " +
                "optional ein eigener Renderer (kind=custom).\n\n" +
                "ABGRENZUNG DER EBENEN: Setup = Fenster-Slots + Geometrie | Chart-Preset = Inhalt EINES FENSTERS " +
                "(geordnete Panes + Topbar + X-Achsen-Pane) | Pane-Preset = Inhalt EINES PANES. " +
                "Chart-Presets (manage_chart_presets) verweisen ueber `panes` [{pane_id, pane_preset_id, " +
                "sort_order, weight, scale, overrides}] auf Pane-Presets; Pane-Presets selbst kennen keine " +
                "Fenster oder Topbar-Metriken.\n\n" +
                "ACTIONS:\n" +
                "- LIST: alle Pane-Presets kompakt (id, display_name, role, kind, member_count, summary).\n" +
                "- GET: ein Pane-Preset inkl. aufgeloester Serien. Erfordert `pane_preset_id`.\n" +
                "- CREATE: neues Pane-Preset anlegen. Erfordert `pane_preset_id`; die Id ist das Referenzziel " +
                "in Chart-Presets.\n" +
                "- UPDATE: vorhandenes Pane-Preset aendern; nur uebergebene Felder werden geschrieben " +
                "(eine mitgeschickte `members`-Liste ersetzt die bisherigen Mitglieder).\n" +
                "- DELETE / ARCHIVE: Pane-Preset entfernen (soft: archived=true, der Datensatz bleibt in der DB).\n\n" +
                "WHEN TO USE: wenn ein Serien-Bundle fuer ein Pane entstehen oder geaendert werden soll " +
                "(z. B. RS-Monitor mit Thresholds, Referenzlinien und Zonen) — oder wenn nachgeschaut werden " +
                "soll, welche Pane-Presets es gibt.\n" +
                "WHEN NOT TO USE: fuer Fensterinhalte/Chart-Presets (manage_chart_presets) und fuer das " +
                "Anzeigen von Charts (manage_chart_viewer).",
            inputSchema: {
                action: z.enum(["LIST", "GET", "CREATE", "UPDATE", "DELETE", "ARCHIVE"]).describe("The operation to perform."),
                pane_preset_id: z.string().optional().describe("ID des Pane-Presets (z. B. 'qmaggi__main', 'rs_monitor'). Required for GET, CREATE, UPDATE, DELETE, ARCHIVE."),
                display_name: z.string().optional().describe("Menschenlesbarer Name des Pane-Presets. Used in CREATE and UPDATE."),
                description: z.string().optional().describe("Beschreibung des Pane-Presets. Used in CREATE and UPDATE."),
                role: z.enum(["price", "value", "volume", "any"]).optional().describe(
                    "Rolle des Panes: 'price' = Kerzen/Instrument (Slot-Id 'main', Crosshair-Besitzer), " +
                    "'value' = eigene Y-Achse bei geteilter X-Achse, 'volume' = Histogramm des Fensters, " +
                    "'any' = neutral (Default). Used in CREATE and UPDATE."
                ),
                kind: z.enum(["indicator", "custom"]).optional().describe("'indicator' = Serien aus Features (Default), 'custom' = eigener Renderer ueber renderer/params. Used in CREATE and UPDATE."),
                default_scale: z.enum(["linear", "log"]).optional().describe("Default-Y-Skalierung dieses Panes (Default 'linear'). Used in CREATE and UPDATE."),
                members: z.array(
                    z.object({
                        feature_id: z.string().describe("Canonical Feature-ID (z. B. 'sma_10', 'ema_21', 'bb_20', 'ibd_rs', 'adr_20_sma')."),
                        sort_order: z.number().optional().describe("Reihenfolge der Serie (Default: Array-Index)."),
                        style_override: z.record(z.string(), z.any()).optional().describe("Style-Overrides (z. B. { color: '#FF00FF', width: 2, type: 'line' })."),
                        rules: z.record(z.string(), z.any()).optional().describe("Regeln der Serie (z. B. { thresholds: [{ above: 80, color: '#26A69A' }, { below: 30, color: '#EF5350' }] }).")
                    })
                ).optional().describe("Serien dieses Panes. Used in CREATE and UPDATE; bei UPDATE ersetzt die Liste die bisherigen Mitglieder."),
                refs: z.array(z.record(z.string(), z.any())).optional().describe("Horizontale Referenzlinien, z. B. [{ value: 80, label: '80', style: { color: '#787B86' } }]. Used in CREATE and UPDATE."),
                derives: z.array(z.record(z.string(), z.any())).optional().describe("Abgeleitete Serien, z. B. [{ id: 'revival', fn: 'cross_window', series: 'ibd_rs', low: 30, high: 80, within: 20 }]. Used in CREATE and UPDATE."),
                zones: z.array(z.record(z.string(), z.any())).optional().describe("Farbzonen, z. B. [{ from: 'revival', style: { color: '#26A69A', alpha: 38 }, label: '30->80' }]. Used in CREATE and UPDATE."),
                params: z.record(z.string(), z.any()).optional().describe("Parameter des Renderers (nur sinnvoll bei kind='custom'). Used in CREATE and UPDATE."),
                renderer: z.string().optional().describe("Renderer-Name fuer kind='custom' (z. B. 'rs_monitor'). Used in CREATE and UPDATE.")
            }
        },
        async ({ action, pane_preset_id, display_name, description, role, kind, default_scale, members, refs, derives, zones, params, renderer }: any) => {
            if (action === "LIST") {
                log.info("[manage_pane_presets] LIST pane presets");
                const res = await fetch(`${PCA_SERVICE_URL}/api/panes`);
                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{ type: "text", text: JSON.stringify(await res.json(), null, 2) }]
                };
            }

            if (!pane_preset_id) throw new Error("pane_preset_id is required for this action.");

            if (action === "GET") {
                log.info(`[manage_pane_presets] GET pane preset ${pane_preset_id}`);
                const res = await fetch(`${PCA_SERVICE_URL}/api/panes/${encodeURIComponent(pane_preset_id)}`);
                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{ type: "text", text: JSON.stringify(await res.json(), null, 2) }]
                };
            }

            if (action === "DELETE" || action === "ARCHIVE") {
                log.info(`[manage_pane_presets] ${action} pane preset ${pane_preset_id}`);
                const res = await fetch(`${PCA_SERVICE_URL}/api/panes/${encodeURIComponent(pane_preset_id)}`, { method: "DELETE" });
                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{
                        type: "text",
                        text: `Pane preset ${pane_preset_id} ${action === "DELETE" ? "deleted" : "archived"} (soft delete: archived=true, der Datensatz bleibt in der DB).`
                    }]
                };
            }

            if (action === "CREATE" || action === "UPDATE") {
                log.info(`[manage_pane_presets] ${action} pane preset ${pane_preset_id}`);

                const payload: Record<string, unknown> = { id: pane_preset_id };
                if (action === "CREATE") {
                    payload.display_name = display_name || pane_preset_id;
                    payload.description = description || "";
                    payload.role = role || "any";
                    payload.kind = kind || "indicator";
                    payload.default_scale = default_scale || "linear";
                } else {
                    // UPDATE ist partiell: nur ausdruecklich uebergebene Felder schreiben.
                    if (display_name !== undefined) payload.display_name = display_name;
                    if (description !== undefined) payload.description = description;
                    if (role !== undefined) payload.role = role;
                    if (kind !== undefined) payload.kind = kind;
                    if (default_scale !== undefined) payload.default_scale = default_scale;
                }

                if (members !== undefined) {
                    payload.members = members.map((m: any, idx: number) => ({
                        feature_id: m.feature_id,
                        sort_order: m.sort_order ?? idx,
                        style_override: m.style_override || {},
                        rules: m.rules || {}
                    }));
                }
                if (refs !== undefined) payload.refs = refs;
                if (derives !== undefined) payload.derives = derives;
                if (zones !== undefined) payload.zones = zones;
                if (params !== undefined) payload.params = params;
                if (renderer !== undefined) payload.renderer = renderer;

                const url = action === "CREATE"
                    ? `${PCA_SERVICE_URL}/api/panes`
                    : `${PCA_SERVICE_URL}/api/panes/${encodeURIComponent(pane_preset_id)}`;
                const res = await fetch(url, {
                    method: action === "CREATE" ? "POST" : "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });

                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{
                        type: "text",
                        text: `Pane preset ${pane_preset_id} successfully ${action === "CREATE" ? "created" : "updated"}.`
                    }]
                };
            }

            throw new Error("Invalid action.");
        }
    );
}
