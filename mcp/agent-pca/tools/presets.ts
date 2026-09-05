import { z } from "zod";
import { log } from "./shared.ts";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

const PCA_SERVICE_URL = Deno.env.get("PCA_SERVICE_URL") || "http://qjm-pca-service:8794";

export function registerPresetTools(server: McpServer) {
    server.registerTool(
        "manage_chart_presets",
        {
            title: "Manage Chart Presets",
            description: "Create, read, update, or delete dynamic chart presets (e.g. creating a preset with specific indicators like sma_10, ema_21, bb_20 and specific colors).",
            inputSchema: {
                action: z.enum(["CREATE", "GET", "LIST", "UPDATE", "DELETE"]).describe("The operation to perform."),
                preset_id: z.string().optional().describe("ID of the preset (e.g. 'qmaggi', 'momentum'). Required for CREATE, GET, UPDATE, DELETE."),
                display_name: z.string().optional().describe("Human readable name for the preset. Used in CREATE and UPDATE."),
                description: z.string().optional().describe("Description of the preset. Used in CREATE and UPDATE."),
                topbar_metrics: z.array(z.string()).optional().describe("List of topbar metrics (e.g. 'ibd_rs', 'minervini'). Used in CREATE and UPDATE."),
                members: z.array(
                    z.object({
                        feature_id: z.string().describe("The canonical feature ID (e.g. 'sma_10', 'ema_20', 'bb_20', 'adr_1_pct', 'adr_20_sma')."),
                        sort_order: z.number().default(0).describe("Order in the overlay list."),
                        style_override: z.record(z.any()).optional().describe("Style overrides (e.g. { color: '#FF00FF', width: 2 }).")
                    })
                ).optional().describe("List of indicators for this preset. Used in CREATE and UPDATE.")
            }
        },
        async ({ action, preset_id, display_name, description, topbar_metrics, members }: any) => {
            if (action === "LIST") {
                log.info("[manage_chart_presets] LIST presets");
                const res = await fetch(`${PCA_SERVICE_URL}/api/presets`);
                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{ type: "text", text: JSON.stringify(await res.json(), null, 2) }]
                };
            }

            if (!preset_id) throw new Error("preset_id is required for this action.");

            if (action === "GET") {
                log.info(`[manage_chart_presets] GET preset ${preset_id}`);
                const res = await fetch(`${PCA_SERVICE_URL}/api/presets/${preset_id}`);
                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{ type: "text", text: JSON.stringify(await res.json(), null, 2) }]
                };
            }

            if (action === "DELETE") {
                log.info(`[manage_chart_presets] DELETE preset ${preset_id}`);
                const res = await fetch(`${PCA_SERVICE_URL}/api/presets/${preset_id}`, { method: "DELETE" });
                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{ type: "text", text: `Preset ${preset_id} successfully deleted.` }]
                };
            }

            if (action === "CREATE" || action === "UPDATE") {
                if (!members) throw new Error("members array is required for CREATE and UPDATE");
                
                log.info(`[manage_chart_presets] ${action} preset ${preset_id}`);
                const payload = {
                    id: preset_id,
                    display_name: display_name || preset_id,
                    description: description || "",
                    topbar_metrics: topbar_metrics || [],
                    members: members.map((m: any, idx: number) => ({
                        feature_id: m.feature_id,
                        sort_order: m.sort_order ?? idx,
                        style_override: m.style_override || {}
                    }))
                };

                const url = action === "CREATE" ? `${PCA_SERVICE_URL}/api/presets` : `${PCA_SERVICE_URL}/api/presets/${preset_id}`;
                const method = action === "CREATE" ? "POST" : "PUT";
                
                const res = await fetch(url, {
                    method,
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });

                if (!res.ok) throw new Error(`API error: ${await res.text()}`);
                return {
                    content: [{ type: "text", text: `Preset ${preset_id} successfully ${action === "CREATE" ? "created" : "updated"}.` }]
                };
            }

            throw new Error("Invalid action.");
        }
    );
}
