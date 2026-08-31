import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { log, supabase } from "./shared.ts";

// ---------------------------------------------------------------------------
// Docker Engine API — Unix Socket Client
// ---------------------------------------------------------------------------

async function dockerRequest(method: string, path: string): Promise<{ status: number; body: string }> {
  const conn = await Deno.connect({ transport: "unix", path: "/var/run/docker.sock" });
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  const reqLine = `${method} ${path} HTTP/1.0\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n`;
  await conn.write(encoder.encode(reqLine));

  const chunks: Uint8Array[] = [];
  const buf = new Uint8Array(16384);
  let n: number | null;
  while ((n = await conn.read(buf)) !== null) {
    chunks.push(buf.slice(0, n));
  }
  conn.close();

  const totalLen = chunks.reduce((s, c) => s + c.length, 0);
  const full = new Uint8Array(totalLen);
  let offset = 0;
  for (const c of chunks) {
    full.set(c, offset);
    offset += c.length;
  }
  const raw = decoder.decode(full);

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
// MCP Tool Registration
// ---------------------------------------------------------------------------

export function registerIbkrSyncTools(server: McpServer) {
  server.registerTool(
    "manage_ibkr_sync",
    {
      title: "Manage IBKR Sync Daemon",
      description: "Manage the ibkr-sync daemon container that synchronizes trades between the database and the IB Gateway. Use this to check its status, restart it, or start/stop it.",
      inputSchema: {
        action: z.enum(["GET_STATUS", "RESTART", "START", "STOP"]).describe("The action to perform on the ibkr-sync container"),
      },
    },
    async (params: any) => {
      const { action } = params;
      const containerName = "ibkr-sync";

      try {
        if (action === "GET_STATUS") {
          const state = await dockerGetContainerState(containerName);

          let pendingTradesInfo = "Unbekannt";
          try {
            const { data, error } = await supabase
              .from("pta_execution_log")
              .select("id, ticker, action, quantity, event_type")
              .is("broker_order_id", null)
              .in("event_type", ["ORDER_SUBMITTED", "CANCEL_REQUESTED"]);

            if (error) {
              pendingTradesInfo = `Fehler beim Abrufen der Trades: ${error.message}`;
            } else if (data && data.length > 0) {
              const tradeLines = data.map(t => `  - ${t.action || t.event_type} ${t.quantity ? t.quantity + 'x ' : ''}${t.ticker || 'Unknown'}`);
              pendingTradesInfo = `Es gibt **${data.length} ausstehende Trade-Aktionen** in der Queue:\n${tradeLines.join('\n')}`;
            } else {
              pendingTradesInfo = `Es gibt aktuell **keine** ausstehenden Trades in der Queue.`;
            }
          } catch (e: any) {
             pendingTradesInfo = `Fehler beim Abrufen der Trades: ${e.message}`;
          }

          const lines = [
            `🔄 **IBKR Sync Daemon Status**`,
            ``,
            `**Container:** \`${containerName}\``,
            `**Status:** ${state.running ? "🟢 Running" : "⬛ Stopped"} (${state.status})`,
            state.startedAt ? `**Gestartet:** ${state.startedAt}` : "",
            state.error ? `⚠️ **Fehler:** ${state.error}` : "",
            ``,
            pendingTradesInfo
          ].filter(Boolean);

          return { content: [{ type: "text", text: lines.join("\n") }] };
        }

        if (action === "RESTART") {
          const result = await dockerContainerAction(containerName, "restart");
          log.info(`[Sync Daemon] Restarted container: ${containerName}`);
          return {
            content: [{
              type: "text",
              text: result.ok
                ? `🔄 Container '${containerName}' wurde neu gestartet.`
                : `❌ Restart fehlgeschlagen: ${result.detail}`,
            }],
            isError: !result.ok,
          };
        }

        if (action === "START") {
          const result = await dockerContainerAction(containerName, "start");
          log.info(`[Sync Daemon] Started container: ${containerName}`);
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

        if (action === "STOP") {
          const result = await dockerContainerAction(containerName, "stop");
          log.info(`[Sync Daemon] Stopped container: ${containerName}`);
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
        log.error(`[Sync Daemon] Error in manage_ibkr_sync: ${err.message}`);
        return { content: [{ type: "text", text: `Fehler: ${err.message}` }], isError: true };
      }
    }
  );
}
