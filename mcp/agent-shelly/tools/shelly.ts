// @ts-nocheck
declare const Deno: any;
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

let POWERMETER_API_URL = typeof Deno !== "undefined" ? (Deno.env.get("POWERMETER_API_URL") || "http://127.0.0.1:8800") : "http://127.0.0.1:8800";
if (POWERMETER_API_URL.includes(":8798")) {
  POWERMETER_API_URL = "http://127.0.0.1:8800";
}
const DEFAULT_HANDLE = typeof Deno !== "undefined" ? (Deno.env.get("TARGET_DEVICE_HANDLE") || "server-plug") : "server-plug";

async function apiRequest(endpoint: string, options: RequestInit = {}) {
  const url = `${POWERMETER_API_URL}${endpoint}`;
  try {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`HTTP ${res.status}: ${errText}`);
    }
    return await res.json();
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`Powermeter API Fehler (${url}): ${msg}`);
  }
}

export function registerShellyTools(server: McpServer) {
  // Tool 1: Live-Metriken abrufen
  server.tool(
    "shelly_get_metrics",
    "Liest die aktuellen Echtzeit-Messwerte (Leistung in Watt, Netzspannung in Volt, Stromstärke in Ampere, aufgelaufene kWh, Temperatur, Relais-Status) des Server-Plugs (oder eines angegebenen Handles) aus. " +
      "💡 NÜTZLICH FÜR PRO-WATT-OPTIMIERUNG & DEBUGGING: Damit prüfst du, ob gerade starke Verbraucher aktiv sind (z. B. GPU-/LLM-Last) und wie viel der Server real aus der Steckdose zieht. " +
      "Kombiniere es mit openbrain-cco 'get_system_metrics' (GPU-Auslastung/Watt) um Last und Wandverbrauch zu korrelieren — ideal, um ineffiziente Jobs zu erkennen. " +
      "Bei ~0 W ist kein relevanter Verbraucher aktiv (Idle).",
    {
      handle: z.string().optional().describe(`Geräte-Handle (Standard: '${DEFAULT_HANDLE}')`),
    },
    async ({ handle }) => {
      const target = handle || DEFAULT_HANDLE;
      try {
        const data = await apiRequest(`/api/devices/${target}/live`);
        const m = data.metrics;
        const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
        const lines = [
          `🔌 Shelly Metriken für '${target}' (${data.device?.name || target}):`,
          `  • Status:        ${m.online ? "🟢 ONLINE" : "🔴 OFFLINE"}`,
          `  • Relais:        ${m.output ? "⚡ EINGESCHALTET" : "⚪ AUSGESCHALTET"}`,
          `  • Leistung:      ${num(m.apower).toFixed(1)} W`,
          `  • Netzspannung:  ${num(m.voltage).toFixed(1)} V`,
          `  • Stromstärke:   ${num(m.current).toFixed(2)} A`,
          `  • Gesamtenergie: ${(num(m.aenergy_total) / 1000.0).toFixed(3)} kWh (${num(m.aenergy_total).toFixed(1)} Wh)`,
          `  • Temperatur:    ${num(m.temp_c).toFixed(1)} °C`,
          `  • Zeitstempel:   ${m.timestamp || m.ts || "unbekannt"}`,
        ];
        if (typeof m.kwh === "number" && typeof m.cost_eur === "number") {
          lines.push(`  • Kosten (1h):   ${m.cost_eur.toFixed(4)} € für ${m.kwh.toFixed(3)} kWh`);
        }
        const text = lines.join("\n");
        return { content: [{ type: "text", text }] };
      } catch (err) {
        return { isError: true, content: [{ type: "text", text: String(err) }] };
      }
    },
  );

  // Tool 2: Steckdose schalten (An / Aus)
  server.tool(
    "shelly_set_switch",
    "Schaltet das Relais der Steckdose ein (true) oder aus (false). VORSICHT: Wenn die Steckdose den Server versorgt, führt das Ausschalten zum sofortigen Stromverlust!",
    {
      on: z.boolean().describe("true für Einschalten, false für Ausschalten"),
      handle: z.string().optional().describe(`Geräte-Handle (Standard: '${DEFAULT_HANDLE}')`),
    },
    async ({ on, handle }) => {
      const target = handle || DEFAULT_HANDLE;
      try {
        const res = await apiRequest(`/api/devices/${target}/switch`, {
          method: "POST",
          body: JSON.stringify({ on }),
        });
        const text = `✅ Relais '${target}' erfolgreich auf ${on ? "EINGESCHALTET (AN)" : "AUSGESCHALTET (AUS)"} gesetzt.`;
        return { content: [{ type: "text", text }] };
      } catch (err) {
        return { isError: true, content: [{ type: "text", text: String(err) }] };
      }
    },
  );

  // Tool 3: Autonomer Server-Power-Cycle
  server.tool(
    "shelly_power_cycle",
    "Schaltet die Steckdose sofort AUS und nach 'restart_delay_seconds' (z. B. 5 Sekunden für schnellen Reboot oder 28.800 Sekunden für 8 Stunden) über den internen Hardware-Timer des Shelly-Chips autonom wieder EIN. Der Timer läuft vollkommen autark auf der Hardware des Shelly, auch wenn der Server offline ist.",
    {
      restart_delay_seconds: z.number().int().min(1).max(86400).describe("Dauer der Stromlosigkeit in Sekunden bis zum automatischen Wiedereinschalten (z. B. 5 für schnellen Reboot, 28800 für 8 Stunden)"),
      handle: z.string().optional().describe(`Geräte-Handle (Standard: '${DEFAULT_HANDLE}')`),
    },
    async ({ restart_delay_seconds, handle }) => {
      const target = handle || DEFAULT_HANDLE;
      try {
        const res = await apiRequest(`/api/devices/${target}/power-cycle`, {
          method: "POST",
          body: JSON.stringify({ restart_delay_seconds }),
        });
        const text = [
          `⚠️ Power-Cycle für '${target}' ausgelöst!`,
          `  • Status: Relais wurde sofort stromlos geschaltet.`,
          `  • Wiedereinschalten in: ${restart_delay_seconds} Sekunden (${(restart_delay_seconds / 3600).toFixed(2)} Std.)`,
          `  • Hinweis: Der Countdown läuft autonom auf dem Shelly ESP32-Chip. Das Mainboard startet nach Spannungsrückkehr automatisch (sofern AC Power Loss im BIOS aktiv ist).`,
        ].join("\n");
        return { content: [{ type: "text", text }] };
      } catch (err) {
        return { isError: true, content: [{ type: "text", text: String(err) }] };
      }
    },
  );

  // Tool 4: Verbrauchs-Historie abfragen (Parquet)
  server.tool(
    "shelly_get_consumption_history",
    "Liest aggregierte Verbrauchsdaten und berechnete Stromkosten aus den Parquet-Dateien für das gewählte Zeitfenster aus.",
    {
      range: z.enum(["live", "24h", "7d", "30d", "1y", "all"]).default("24h").describe("Zeitfenster: 'live' (letzte 60m), '24h' (1s/1m Daten), '7d'/'30d' (1m Rollups), '1y'/'all' (1h Rollups für alle Ewigkeit)"),
      handle: z.string().optional().describe(`Geräte-Handle (Standard: '${DEFAULT_HANDLE}')`),
    },
    async ({ range, handle }) => {
      const target = handle || DEFAULT_HANDLE;
      try {
        const res = await apiRequest(`/api/devices/${target}/history?range=${range}`);
        const text = [
          `📊 Verbrauchs-Historie für '${target}' (${range}):`,
          `  • Gesamtverbrauch: ${res.total_kwh.toFixed(3)} kWh`,
          `  • Spitzenleistung:  ${res.peak_watts.toFixed(1)} W`,
          `  • Gesamtkosten:     ${res.total_cost_eur.toFixed(2)} €`,
          `  • Datenpunkte:      ${res.points?.length || 0}`,
          range === "all" ? `  • Archivierung:     Permanente 1h-Parquet-Aggregation für alle Ewigkeit aktiv.` : "",
        ].filter(Boolean).join("\n");
        return { content: [{ type: "text", text }] };
      } catch (err) {
        return { isError: true, content: [{ type: "text", text: String(err) }] };
      }
    },
  );

  // Tool 5: Strompreis anpassen
  server.tool(
    "shelly_set_electricity_price",
    "Setzt den Strompreis in €/kWh für heute oder ein bestimmtes Datum. Der Preis wird täglich historisiert in electricity_prices.parquet gespeichert und für Kostenberechnungen herangezogen.",
    {
      price_eur_kwh: z.number().positive().describe("Strompreis in Euro pro Kilowattstunde (z. B. 0.35)"),
      date: z.string().optional().describe("Datum im Format YYYY-MM-DD (Standard: heute)"),
    },
    async ({ price_eur_kwh, date }) => {
      try {
        const res = await apiRequest("/api/pricing", {
          method: "POST",
          body: JSON.stringify({ price_eur_kwh, date }),
        });
        const text = `✅ Strompreis für ${res.date} erfolgreich auf ${res.price_eur_kwh.toFixed(4)} €/kWh gesetzt.`;
        return { content: [{ type: "text", text }] };
      } catch (err) {
        return { isError: true, content: [{ type: "text", text: String(err) }] };
      }
    },
  );

  // Tool 6: Hardware Notfall Auto-On konfigurieren
  server.tool(
    "shelly_configure_auto_on",
    "Konfiguriert den Failsafe-Wiedereinschalt-Timer im Flash-Speicher des Shelly. Selbst wenn das Relais versehentlich per Taster oder Netzwerkfehler ausgeschaltet wird, schaltet es sich nach 'delay_seconds' automatisch wieder ein.",
    {
      delay_seconds: z.number().int().min(1).max(3600).default(5).describe("Verzögerung in Sekunden bis zum automatischen Wiedereinschalten (Standard: 5s)"),
      handle: z.string().optional().describe(`Geräte-Handle (Standard: '${DEFAULT_HANDLE}')`),
    },
    async ({ delay_seconds, handle }) => {
      const target = handle || DEFAULT_HANDLE;
      try {
        const res = await apiRequest(`/api/devices/${target}/config/auto-on`, {
          method: "POST",
          body: JSON.stringify({ delay_seconds }),
        });
        const text = `✅ Hardware-Failsafe konfiguriert: Steckdose '${target}' schaltet sich bei jedem Ausschalten nach ${delay_seconds} Sekunden automatisch wieder ein.`;
        return { content: [{ type: "text", text }] };
      } catch (err) {
        return { isError: true, content: [{ type: "text", text: String(err) }] };
      }
    },
  );

  // Tool 7: Alle Geräte auflisten
  server.tool(
    "shelly_list_devices",
    "Listet alle in Powermeter registrierten Shelly-Steckdosen mit Handles, IP-Adressen, Namen und aktuellem Online-Status auf.",
    {},
    async () => {
      try {
        const devices = await apiRequest("/api/devices");
        if (!devices || devices.length === 0) {
          return { content: [{ type: "text", text: "Keine Shelly-Geräte registriert." }] };
        }
        const lines = ["📋 Registrierte Shelly-Geräte:"];
        for (const d of devices) {
          const isServer = d.is_server_plug ? " [SERVER-PLUG]" : "";
          const status = d.online ? "🟢 ONLINE" : "🔴 OFFLINE";
          const p = typeof d.latest?.apower === "number" ? `${d.latest.apower.toFixed(1)}W` : "---";
          lines.push(`  • ${d.handle}: ${d.name} (${d.ip}) - ${status} (${p})${isServer}`);
        }
        return { content: [{ type: "text", text: lines.join("\n") }] };
      } catch (err) {
        return { isError: true, content: [{ type: "text", text: String(err) }] };
      }
    },
  );
}
