import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { supabase, log } from "./shared.ts";

// ---------------------------------------------------------------------------
// Docker Engine API — Unix Socket Client
// ---------------------------------------------------------------------------

async function dockerRequest(method: string, path: string): Promise<{ status: number; body: string }> {
  const conn = await Deno.connect({ transport: "unix", path: "/var/run/docker.sock" });
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  const reqLine = `${method} ${path} HTTP/1.0\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n`;
  await conn.write(encoder.encode(reqLine));

  // Read the full response (HTTP/1.0 closes after response)
  const chunks: Uint8Array[] = [];
  const buf = new Uint8Array(16384);
  let n: number | null;
  while ((n = await conn.read(buf)) !== null) {
    chunks.push(buf.slice(0, n));
  }
  conn.close();

  // Combine chunks
  const totalLen = chunks.reduce((s, c) => s + c.length, 0);
  const full = new Uint8Array(totalLen);
  let offset = 0;
  for (const c of chunks) {
    full.set(c, offset);
    offset += c.length;
  }
  const raw = decoder.decode(full);

  // Split headers and body
  const sepIdx = raw.indexOf("\r\n\r\n");
  const headerBlock = sepIdx >= 0 ? raw.substring(0, sepIdx) : raw;
  const body = sepIdx >= 0 ? raw.substring(sepIdx + 4) : "";
  const statusLine = headerBlock.split("\r\n")[0];
  const status = parseInt(statusLine.split(" ")[1] || "0", 10);

  return { status, body };
}

async function dockerGetContainerState(containerName: string): Promise<{
  running: boolean;
  status: string;
  startedAt: string;
  error: string | null;
}> {
  try {
    const { status, body } = await dockerRequest("GET", `/containers/${containerName}/json`);
    if (status === 200) {
      const data = JSON.parse(body);
      const state = data.State || {};
      return {
        running: state.Running || false,
        status: state.Status || "unknown",
        startedAt: state.StartedAt || "",
        error: null,
      };
    } else if (status === 404) {
      return { running: false, status: "not_found", startedAt: "", error: `Container '${containerName}' nicht gefunden` };
    } else {
      return { running: false, status: "error", startedAt: "", error: `Docker API ${status}` };
    }
  } catch (err: any) {
    return { running: false, status: "error", startedAt: "", error: err.message };
  }
}

async function dockerContainerAction(containerName: string, action: "start" | "stop" | "restart"): Promise<{ ok: boolean; detail: string }> {
  try {
    const { status } = await dockerRequest("POST", `/containers/${containerName}/${action}`);
    if (status === 204 || status === 304) {
      return { ok: true, detail: `${action} erfolgreich für '${containerName}'` };
    } else {
      return { ok: false, detail: `Docker ${action} für '${containerName}' fehlgeschlagen (HTTP ${status})` };
    }
  } catch (err: any) {
    return { ok: false, detail: `Docker API Fehler: ${err.message}` };
  }
}

// ---------------------------------------------------------------------------
// Gateway Config Helpers (Supabase)
// ---------------------------------------------------------------------------

interface GatewayConfig {
  active_mode: "live" | "paper";
  live: { container_name: string; port: number; host: string };
  paper: { container_name: string; port: number; host: string };
}

const DEFAULT_CONFIG: GatewayConfig = {
  active_mode: "live",
  live: { container_name: "ib-gateway_live-ib-gateway-1", port: 4002, host: "10.20.0.23" },
  paper: { container_name: "ib-gateway_paper", port: 4001, host: "10.20.0.23" },
};

async function loadGatewayConfig(): Promise<GatewayConfig> {
  try {
    const { data } = await supabase
      .from("system_settings")
      .select("value")
      .eq("key", "ib_gateway_config")
      .single();
    if (data?.value) {
      return { ...DEFAULT_CONFIG, ...data.value } as GatewayConfig;
    }
  } catch (_e) {
    log.warn("[Gateway] Could not load config from DB, using defaults.");
  }
  return DEFAULT_CONFIG;
}

async function saveGatewayConfig(cfg: GatewayConfig): Promise<void> {
  const { error } = await supabase
    .from("system_settings")
    .upsert({ key: "ib_gateway_config", value: cfg }, { onConflict: "key" });
  if (error) throw new Error(`DB update failed: ${error.message}`);
}

async function setGatewayStatus(connected: boolean): Promise<void> {
  await supabase
    .from("system_settings")
    .upsert(
      { key: "ib_gateway_status", value: { connected }, updated_at: new Date().toISOString() },
      { onConflict: "key" }
    );
}

// ---------------------------------------------------------------------------
// MCP Tool Registration
// ---------------------------------------------------------------------------

export function registerGatewayTools(server: McpServer) {
  server.registerTool(
    "manage_ib_gateway",
    {
      title: "Manage IB Gateway (Live/Paper)",
      description:
        "Manage the Interactive Brokers / CapTrader Gateway. " +
        "GET_STATUS: Query the current trading mode and Docker status of both gateway containers. " +
        "SWITCH_MODE: Switch between 'live' and 'paper' trading mode (stops old container, starts new, updates DB). " +
        "RESTART_GATEWAY: Restart the active or specified gateway container. " +
        "START_GATEWAY / STOP_GATEWAY: Start or stop a specific gateway container.",
      inputSchema: {
        action: z
          .enum(["GET_STATUS", "SWITCH_MODE", "RESTART_GATEWAY", "START_GATEWAY", "STOP_GATEWAY"])
          .describe("The action to perform"),
        target_mode: z
          .enum(["live", "paper"])
          .optional()
          .describe("Required for SWITCH_MODE. The target trading mode."),
        target_container: z
          .enum(["active", "live", "paper"])
          .optional()
          .default("active")
          .describe("Which container to target for START/STOP/RESTART. Default: active."),
      },
    },
    async (params: any) => {
      const { action, target_mode, target_container } = params;

      try {
        const cfg = await loadGatewayConfig();

        // Resolve container name from target
        function resolveContainer(target: string): string {
          if (target === "live") return cfg.live.container_name;
          if (target === "paper") return cfg.paper.container_name;
          // "active" — use current mode
          return cfg[cfg.active_mode].container_name;
        }

        // ── GET_STATUS ─────────────────────────────────────────────
        if (action === "GET_STATUS") {
          const [liveState, paperState] = await Promise.all([
            dockerGetContainerState(cfg.live.container_name),
            dockerGetContainerState(cfg.paper.container_name),
          ]);

          // Check DB connection status
          let dbConnected = false;
          try {
            const { data } = await supabase
              .from("system_settings")
              .select("value")
              .eq("key", "ib_gateway_status")
              .single();
            dbConnected = data?.value?.connected || false;
          } catch (_e) { /* ignore */ }

          const lines = [
            `🏦 **IBKR Gateway Status**`,
            ``,
            `**Aktiver Modus:** ${cfg.active_mode.toUpperCase()}`,
            `**API-Verbindung:** ${dbConnected ? "🟢 Verbunden" : "🔴 Getrennt"}`,
            ``,
            `**Live-Gateway** (\`${cfg.live.container_name}\`):`,
            `  Docker: ${liveState.running ? "🟢 Running" : "⬛ Stopped"} (${liveState.status})`,
            `  Host: ${cfg.live.host}:${cfg.live.port}`,
            liveState.error ? `  ⚠️ ${liveState.error}` : "",
            ``,
            `**Paper-Gateway** (\`${cfg.paper.container_name}\`):`,
            `  Docker: ${paperState.running ? "🟢 Running" : "⬛ Stopped"} (${paperState.status})`,
            `  Host: ${cfg.paper.host}:${cfg.paper.port}`,
            paperState.error ? `  ⚠️ ${paperState.error}` : "",
          ].filter(Boolean);

          return { content: [{ type: "text", text: lines.join("\n") }] };
        }

        // ── SWITCH_MODE ────────────────────────────────────────────
        if (action === "SWITCH_MODE") {
          if (!target_mode) {
            return { content: [{ type: "text", text: "Fehler: 'target_mode' ('live' oder 'paper') ist erforderlich für SWITCH_MODE." }], isError: true };
          }
          if (cfg.active_mode === target_mode) {
            return { content: [{ type: "text", text: `Modus ist bereits '${target_mode.toUpperCase()}'. Keine Änderung nötig.` }] };
          }

          const oldMode = cfg.active_mode;
          const oldContainer = cfg[oldMode].container_name;
          const newContainer = cfg[target_mode].container_name;

          const steps: string[] = [`🔄 Trading Mode: ${oldMode.toUpperCase()} → ${target_mode.toUpperCase()}`];

          // 1. Stop old container
          const stopResult = await dockerContainerAction(oldContainer, "stop");
          steps.push(`1. Stop ${oldContainer}: ${stopResult.ok ? "✅" : "⚠️ " + stopResult.detail}`);

          // 2. Start new container
          const startResult = await dockerContainerAction(newContainer, "start");
          steps.push(`2. Start ${newContainer}: ${startResult.ok ? "✅" : "❌ " + startResult.detail}`);

          if (!startResult.ok) {
            return { content: [{ type: "text", text: steps.join("\n") + "\n\n❌ Moduswechsel fehlgeschlagen — neuer Container konnte nicht gestartet werden." }], isError: true };
          }

          // 3. Update DB
          cfg.active_mode = target_mode;
          await saveGatewayConfig(cfg);
          steps.push("3. DB aktualisiert: ✅");

          // 4. Reset connection status
          await setGatewayStatus(false);
          steps.push("4. Verbindungsstatus zurückgesetzt (wartet auf IBKR-Reconnect)");

          log.info(`[Gateway] Switched mode: ${oldMode} → ${target_mode}`);
          return { content: [{ type: "text", text: steps.join("\n") }] };
        }

        // ── RESTART_GATEWAY ────────────────────────────────────────
        if (action === "RESTART_GATEWAY") {
          const containerName = resolveContainer(target_container || "active");
          const result = await dockerContainerAction(containerName, "restart");
          await setGatewayStatus(false);
          log.info(`[Gateway] Restarted container: ${containerName}`);
          return {
            content: [{
              type: "text",
              text: result.ok
                ? `🔄 Container '${containerName}' wurde neu gestartet. Warte auf IBKR-Reconnect...`
                : `❌ Restart fehlgeschlagen: ${result.detail}`,
            }],
            isError: !result.ok,
          };
        }

        // ── START_GATEWAY ──────────────────────────────────────────
        if (action === "START_GATEWAY") {
          const containerName = resolveContainer(target_container || "active");
          const result = await dockerContainerAction(containerName, "start");
          log.info(`[Gateway] Started container: ${containerName}`);
          return {
            content: [{
              type: "text",
              text: result.ok
                ? `▶️ Container '${containerName}' wurde gestartet.`
                : `❌ Start fehlgeschlagen: ${result.detail}`,
            }],
            isError: !result.ok,
          };
        }

        // ── STOP_GATEWAY ───────────────────────────────────────────
        if (action === "STOP_GATEWAY") {
          const containerName = resolveContainer(target_container || "active");
          const result = await dockerContainerAction(containerName, "stop");
          await setGatewayStatus(false);
          log.info(`[Gateway] Stopped container: ${containerName}`);
          return {
            content: [{
              type: "text",
              text: result.ok
                ? `⏹️ Container '${containerName}' wurde gestoppt.`
                : `❌ Stop fehlgeschlagen: ${result.detail}`,
            }],
            isError: !result.ok,
          };
        }

        return { content: [{ type: "text", text: `Unbekannte Aktion: ${action}` }], isError: true };
      } catch (err: any) {
        log.error(`[Gateway] Error in manage_ib_gateway: ${err.message}`);
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );
}
