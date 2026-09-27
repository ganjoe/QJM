/**
 * Chart-Builder-Aktionen fuer manage_chart_viewer.
 * Plan: CHARTVIEWER_CHART_BUILDER_API_PLAN.md (Phasen 1, 2, 5, 6).
 *
 * Die Lifecycle-Aktionen laufen ueber /api/command in den Chart-Agenten zurueck
 * und werden dort an die ControlService-Fassade delegiert (Single Facade: das
 * Control Panel des Viewers benutzt exakt dieselbe Implementierung). Die
 * Komfort-Aktionen (ADD/REMOVE/REORDER_PANE, DUPLICATE/DELETE_CHART) sind hier
 * aus GET_CHART_STATE + COMPOSE_CHART bzw. der PCA-Chart-API zusammengesetzt.
 *
 * Stil: bewusst ohne Template-Literale, damit die Datei einfach patchbar bleibt.
 */

import { PCA_SERVICE_URL } from "./shared.ts";
import { findChartReferences } from "./chart_refs.ts";

const CHART_VIEWER_API_URL =
  Deno.env.get("CHART_VIEWER_API_URL") || "http://host.docker.internal:8766";

export const CHART_BUILDER_ACTIONS = [
  "GET_CHART_STATE",
  "LIST_WINDOWS",
  "SAVE_CHART",
  "APPLY_CHART",
  "ADD_PANE",
  "REMOVE_PANE",
  "REORDER_PANES",
  "DUPLICATE_CHART",
  "DELETE_CHART",
  "DIFF_CHART",
  "LIST_TOPBAR_METRICS",
];

export interface ChartBuilderParams {
  window_id?: string;
  chart_id?: string;
  display_name?: string;
  description?: string;
  overwrite?: boolean;
  pane_preset_id?: string;
  pane_id?: string;
  after_pane_id?: string;
  pane_ids?: string[];
  scale?: string;
  weight?: number;
  source_chart_id?: string;
  new_chart_id?: string;
  force?: boolean;
  apply_to_window?: string;
  symbol?: string;
}

/** Eine Bridge-Aktion ausfuehren und den entpackten data-Block zurueckgeben. */
export async function callControlAction(
  action: string,
  params: Record<string, unknown>,
): Promise<any> {
  const res = await fetch(CHART_VIEWER_API_URL + "/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...params }),
  });
  if (!res.ok) {
    throw new Error(action + " rejected: HTTP " + res.status + " " + (await res.text()));
  }
  const payload = await res.json();
  if (payload && payload.ok === false) {
    const code = payload?.error?.code || "error";
    const message = payload?.error?.message || "unbekannter Fehler";
    throw new Error(action + " failed (" + code + "): " + message);
  }
  if (payload && payload.error) {
    throw new Error(action + " failed: " + payload.error);
  }
  return payload && payload.ok === true ? payload.data : payload;
}

const FENCE = String.fromCharCode(96).repeat(3);

function textPayload(lines: string[], json: unknown, warnings?: any[], skipped?: any[]) {
  const extras: string[] = [];
  const warn = warnings || [];
  const skip = skipped || [];
  if (warn.length > 0) {
    extras.push("", "⚠️ **Warnungen** (" + warn.length + "): " +
      warn.slice(0, 10).map((w) => (w?.code || "?") + (w?.pane_id ? " @" + w.pane_id : "") +
        (w?.detail ? " " + w.detail : "")).join("; ") +
      (warn.length > 10 ? " … +" + (warn.length - 10) + " weitere" : ""));
  }
  if (skip.length > 0) {
    extras.push("", "⏭ **Übersprungen** (" + skip.length + "): " +
      skip.map((s) => (s?.pane_preset_id || s?.symbol || "?") + " — " + (s?.reason || "")).join("; "));
  }
  return {
    content: [{
      type: "text",
      text: lines.join("\n") + extras.join("\n") + "\n\n" + FENCE + "json\n" +
        JSON.stringify(json, null, 2) + "\n" + FENCE,
    }],
  };
}

function paneRows(panes: any[]): string[] {
  return (panes || []).map((p) =>
    "| " + (p.pane_id ?? "") + " | " + (p.pane_preset_id ?? "") + " | " + (p.scale ?? "") +
    " | " + (p.weight ?? "") + " | " + JSON.stringify(p.overrides || {}) + " |"
  );
}

/** Pane-Liste eines Fensters in die COMPOSE-Form bringen. */
function statePanes(state: any): any[] {
  return (state?.panes || []).map((p: any) => {
    const pane: any = {
      pane_id: p.pane_id,
      pane_preset_id: p.pane_preset_id,
      scale: p.scale,
      weight: p.weight,
    };
    if (p.overrides && Object.keys(p.overrides).length > 0) pane.overrides = p.overrides;
    return pane;
  });
}

async function getChartState(windowId: string): Promise<any> {
  if (!windowId) throw new Error("Parameter 'window_id' is required.");
  return await callControlAction("GET_CHART_STATE", { window_id: windowId });
}

/** Fensterinhalt neu rendern (Draft) und dabei Chart-Metadaten erhalten. */
async function composeWindow(windowId: string, state: any, panes: any[]): Promise<any> {
  const params: Record<string, unknown> = { window_id: windowId, panes };
  if (state?.chart_id) params.base_chart_id = state.chart_id;
  if (Array.isArray(state?.topbar_metrics) && state.topbar_metrics.length > 0) {
    params.topbar_metrics = state.topbar_metrics;
  }
  return await callControlAction("COMPOSE_CHART", params);
}

function insertIndex(panes: any[], afterPaneId?: string): number {
  if (afterPaneId) {
    const idx = panes.findIndex((p) => p.pane_id === afterPaneId);
    if (idx < 0) {
      throw new Error("ADD_PANE: after_pane_id '" + afterPaneId + "' gibt es in diesem Fenster nicht.");
    }
    return idx + 1;
  }
  const volumeIdx = panes.findIndex((p) => p.pane_preset_id === "builtin:volume");
  return volumeIdx >= 0 ? volumeIdx : panes.length;
}

/**
 * Setup-Namen finden, die ein Chart referenzieren (fuer DELETE_CHART ohne force).
 * Die eigentliche Suche liegt in chart_refs.ts (rein und dort auch getestet).
 * Rueckgabe null = nicht pruefbar (Agent nicht erreichbar) - dann gilt der
 * Schutz als "unbekannt" und es wird ohne force nicht geloescht.
 */
async function setupsReferencing(chartId: string): Promise<string[] | null> {
  try {
    const res = await fetch(CHART_VIEWER_API_URL + "/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "LIST_SETUPS" }),
    });
    if (!res.ok) return null;
    const payload = await res.json();
    if (!Array.isArray(payload?.setups)) return null;
    return findChartReferences(payload.setups, chartId);
  } catch {
    return null;
  }
}

/** Bridge-/Komfort-Aktion ausfuehren; null = nicht zustaendig (Alt-Pfad greift). */
export async function runChartBuilderAction(
  action: string,
  params: ChartBuilderParams,
): Promise<any | null> {
  if (!CHART_BUILDER_ACTIONS.includes(action)) return null;
  const windowId = (params.window_id || "").trim();
  const chartId = (params.chart_id || "").trim();

  if (action === "GET_CHART_STATE") {
    if (!windowId) throw new Error("Parameter 'window_id' is required for GET_CHART_STATE.");
    const state = await callControlAction(action, { window_id: windowId });
    const rows = paneRows(state?.panes || []);
    const lines = [
      "📋 **GET_CHART_STATE** — " + (state?.display_name || "• unbenannt") +
        (state?.draft ? " (Draft, nicht gespeichert)" : ""),
      "",
      "Symbol '" + (state?.symbol || "") + "' · Timeframe '" + (state?.timeframe || "") +
        "' · chart_id '" + (state?.chart_id ?? "—") + "' · Topbar: " +
        ((state?.topbar_metrics || []).join(", ") || "—"),
      "",
      "| pane_id | pane_preset_id | scale | weight | overrides |",
      "| :-- | :-- | :-- | --: | :-- |",
      ...(rows.length > 0 ? rows : ["| — | keine Panes | — | — | — |"]),
    ];
    return textPayload(lines, { status: "success", action, state });
  }

  if (action === "LIST_WINDOWS") {
    const data = await callControlAction(action, {});
    const windows: any[] = data?.windows || [];
    const rows = windows.map((w) =>
      "| " + w.window_id + " | " + w.kind + " | " + (w.symbol || "—") + " | " + (w.timeframe || "—") +
      " | " + (w.chart_id ?? "—") + " | " + (w.draft ? "Draft" : "gespeichert") + " | " + w.pane_count + " |"
    );
    const lines = [
      "🪟 **LIST_WINDOWS** — " + windows.length + " offene(s) Fenster",
      "",
      "| window_id | kind | symbol | timeframe | chart_id | Zustand | Panes |",
      "| :-- | :-- | :-- | :-- | :-- | :-- | --: |",
      ...(rows.length > 0 ? rows : ["| — | keine Fenster offen | — | — | — | — | — |"]),
    ];
    return textPayload(lines, { status: "success", action, ...data });
  }

  if (action === "SAVE_CHART") {
    if (!windowId) throw new Error("Parameter 'window_id' is required for SAVE_CHART.");
    if (!chartId) throw new Error("Parameter 'chart_id' is required for SAVE_CHART.");
    const saved = await callControlAction(action, {
      window_id: windowId,
      chart_id: chartId,
      display_name: params.display_name,
      description: params.description,
      overwrite: params.overwrite,
    });
    const lines = [
      "💾 **SAVE_CHART** — Chart '" + saved?.chart_id + "' " +
        (saved?.created ? "angelegt" : "aktualisiert"),
      "",
      "Fenster '" + windowId + "' zeigt jetzt den gespeicherten Stand (draft=false).",
    ];
    return textPayload(lines, { status: "success", action, ...saved });
  }

  if (action === "APPLY_CHART") {
    if (!windowId) throw new Error("Parameter 'window_id' is required for APPLY_CHART.");
    if (!chartId) throw new Error("Parameter 'chart_id' is required for APPLY_CHART.");
    const applied = await callControlAction(action, { window_id: windowId, chart_id: chartId });
    const lines = [
      "🖼 **APPLY_CHART** — Chart '" + chartId + "' auf Fenster '" + windowId + "' angewendet",
    ];
    return textPayload(lines, { status: "success", action, ...applied },
      applied?.warnings, applied?.skipped);
  }

  if (action === "ADD_PANE") {
    if (!windowId) throw new Error("Parameter 'window_id' is required for ADD_PANE.");
    const presetId = (params.pane_preset_id || "").trim();
    if (!presetId) throw new Error("Parameter 'pane_preset_id' is required for ADD_PANE.");
    const state = await getChartState(windowId);
    const panes = statePanes(state);
    const newPane: any = { pane_preset_id: presetId };
    if (params.pane_id) {
      const wanted = String(params.pane_id).trim();
      if (panes.some((p) => p.pane_id === wanted)) {
        throw new Error("ADD_PANE: pane_id '" + wanted + "' ist in diesem Fenster schon belegt.");
      }
      newPane.pane_id = wanted;
    }
    if (params.scale) newPane.scale = params.scale;
    if (params.weight) newPane.weight = params.weight;
    const after = params.after_pane_id ? String(params.after_pane_id) : "";
    panes.splice(insertIndex(panes, after), 0, newPane);
    const render = await composeWindow(windowId, state, panes);
    const lines = [
      "➕ **ADD_PANE** — '" + presetId + "' in Fenster '" + windowId + "'",
      "",
      "Panes jetzt: " + panes.map((p) => p.pane_id || p.pane_preset_id).join(" → "),
      "Draft — mit SAVE_CHART speichern, sonst stirbt er mit dem Fenster.",
    ];
    return textPayload(
      lines,
      { status: "success", action, window_id: windowId, panes },
      render?.warnings,
      render?.skipped,
    );
  }

  if (action === "REMOVE_PANE") {
    if (!windowId) throw new Error("Parameter 'window_id' is required for REMOVE_PANE.");
    const paneId = (params.pane_id || "").trim();
    if (!paneId) throw new Error("Parameter 'pane_id' is required for REMOVE_PANE.");
    if (paneId === "main") {
      throw new Error("REMOVE_PANE: das Preispane ('main') kann nicht entfernt werden.");
    }
    const state = await getChartState(windowId);
    const panes = statePanes(state);
    if (!panes.some((p) => p.pane_id === paneId)) {
      throw new Error("REMOVE_PANE: Pane '" + paneId + "' gibt es in diesem Fenster nicht.");
    }
    const remaining = panes.filter((p) => p.pane_id !== paneId);
    const render = await composeWindow(windowId, state, remaining);
    const lines = [
      "➖ **REMOVE_PANE** — '" + paneId + "' aus Fenster '" + windowId + "' entfernt",
      "",
      "Panes jetzt: " + remaining.map((p) => p.pane_id || p.pane_preset_id).join(" → "),
    ];
    return textPayload(
      lines,
      { status: "success", action, window_id: windowId, panes: remaining },
      render?.warnings,
      render?.skipped,
    );
  }

  if (action === "REORDER_PANES") {
    if (!windowId) throw new Error("Parameter 'window_id' is required for REORDER_PANES.");
    const order = Array.isArray(params.pane_ids) ? params.pane_ids.map((p) => String(p)) : [];
    if (order.length === 0) throw new Error("Parameter 'pane_ids' is required for REORDER_PANES.");
    const state = await getChartState(windowId);
    const panes = statePanes(state);
    const current = panes.map((p) => p.pane_id);
    const sameSet = current.length === order.length && current.every((id) => order.includes(id));
    if (!sameSet) {
      throw new Error("REORDER_PANES: pane_ids muss genau die aktuellen Panes enthalten (" +
        current.join(", ") + ").");
    }
    if (order[0] !== "main") {
      throw new Error("REORDER_PANES: das Preispane 'main' muss an Position 0 bleiben.");
    }
    const byId = new Map(panes.map((p) => [p.pane_id, p]));
    const ordered = order.map((id) => byId.get(id));
    const render = await composeWindow(windowId, state, ordered);
    const lines = [
      "↕️ **REORDER_PANES** — " + order.join(" → "),
    ];
    return textPayload(
      lines,
      { status: "success", action, window_id: windowId, pane_ids: order },
      render?.warnings,
      render?.skipped,
    );
  }

  if (action === "DIFF_CHART") {
    if (!windowId) throw new Error("Parameter 'window_id' is required for DIFF_CHART.");
    if (!chartId) throw new Error("Parameter 'chart_id' is required for DIFF_CHART.");
    const diff = await callControlAction(action, { window_id: windowId, chart_id: chartId });
    const lines = [
      "🔍 **DIFF_CHART** — Fenster '" + windowId + "' vs. Chart '" + chartId + "'",
      "",
      diff?.same ? "Kein Unterschied — Anwenden wuerde nichts aendern." : "Unterschiede gefunden:",
    ];
    if (diff?.added?.length) {
      lines.push("", "**Neu:** " + diff.added.map((p: any) => p.pane_id + " (" + p.pane_preset_id + ")").join(", "));
    }
    if (diff?.removed?.length) {
      lines.push("", "**Entfernt:** " + diff.removed.map((p: any) => p.pane_id + " (" + p.pane_preset_id + ")").join(", "));
    }
    if (diff?.reordered?.length) {
      lines.push("", "**Reihenfolge:** " + diff.reordered.map((p: any) => p.pane_id + " " + p.from + "→" + p.to).join(", "));
    }
    if (diff?.changed?.length) {
      lines.push("", "**Geaendert:** " + diff.changed.map((c: any) => c.pane_id + " (" + Object.keys(c.fields).join(", ") + ")").join("; "));
    }
    if (diff?.topbar_metrics) {
      lines.push("", "**Topbar-Metriken:** Fenster " + JSON.stringify(diff.topbar_metrics.window) +
        " vs. Chart " + JSON.stringify(diff.topbar_metrics.chart));
    }
    return textPayload(lines, { status: "success", action, ...diff });
  }

  if (action === "DUPLICATE_CHART") {
    const sourceId = (params.source_chart_id || "").trim();
    const targetId = (params.new_chart_id || "").trim();
    if (!sourceId || !targetId) {
      throw new Error("Parameter 'source_chart_id' und 'new_chart_id' sind fuer DUPLICATE_CHART Pflicht.");
    }
    const srcRes = await fetch(PCA_SERVICE_URL + "/api/charts/" + encodeURIComponent(sourceId));
    if (!srcRes.ok) {
      throw new Error("DUPLICATE_CHART: Quellchart '" + sourceId + "' nicht gefunden (HTTP " + srcRes.status + ").");
    }
    const source = await srcRes.json();
    const payload = {
      id: targetId,
      display_name: params.display_name ||
        (source?.display_name ? source.display_name + " (Kopie)" : targetId),
      description: source?.description || "",
      topbar_metrics: source?.topbar_metrics || [],
      x_axis_pane: source?.x_axis_pane ?? null,
      panes: (source?.panes || []).map((p: any) => ({
        pane_id: p.pane_id,
        pane_preset_id: p.pane_preset_id || p?.preset?.id,
        sort_order: p.sort_order ?? 0,
        weight: p.weight ?? 2,
        scale: p.scale || "linear",
        overrides: p.overrides || {},
      })),
    };
    const createRes = await fetch(PCA_SERVICE_URL + "/api/charts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!createRes.ok) {
      throw new Error("DUPLICATE_CHART: anlegen fehlgeschlagen (HTTP " + createRes.status + "): " +
        (await createRes.text()));
    }
    const lines = [
      "🧬 **DUPLICATE_CHART** — '" + sourceId + "' → '" + targetId + "'",
      "",
      payload.panes.length + " Pane(s) uebernommen.",
    ];
    if (params.apply_to_window) {
      await callControlAction("APPLY_CHART", {
        window_id: String(params.apply_to_window),
        chart_id: targetId,
      });
      lines.push("Angewendet auf Fenster '" + params.apply_to_window + "'.");
    }
    return textPayload(lines, { status: "success", action, chart: payload });
  }

  if (action === "DELETE_CHART") {
    if (!chartId) throw new Error("Parameter 'chart_id' is required for DELETE_CHART.");
    if (!params.force) {
      const refs = await setupsReferencing(chartId);
      if (refs === null) {
        throw new Error("DELETE_CHART: Setup-Referenzen nicht pruefbar (Chart-Agent nicht erreichbar). " +
          "Mit force=true trotzdem loeschen.");
      }
      if (refs.length > 0) {
        throw new Error("DELETE_CHART: Chart '" + chartId + "' wird von Setup(s) referenziert (" +
          refs.join(", ") + "). Mit force=true trotzdem loeschen.");
      }
    }
    const res = await fetch(PCA_SERVICE_URL + "/api/charts/" + encodeURIComponent(chartId), {
      method: "DELETE",
    });
    if (!res.ok) {
      throw new Error("DELETE_CHART: HTTP " + res.status + " " + (await res.text()));
    }
    const lines = ["🗑 **DELETE_CHART** — '" + chartId + "' geloescht"];
    return textPayload(lines, { status: "success", action, chart_id: chartId });
  }

  if (action === "LIST_TOPBAR_METRICS") {
    const data = await callControlAction(action, params.symbol ? { symbol: params.symbol } : {});
    const metrics: any[] = data?.metrics || [];
    const rows = metrics.map((m: any) =>
      "| " + m.id + " | " + m.display_name + " | " + (m.calc_type ?? "—") + " | " +
      (m.has_data_for_symbol === null || m.has_data_for_symbol === undefined
        ? "—"
        : (m.has_data_for_symbol ? "ja" : "nein")) + " |"
    );
    const lines = [
      "📈 **LIST_TOPBAR_METRICS** — " + metrics.length + " Metrik(en)" +
        (data?.symbol ? " fuer " + data.symbol : ""),
      "",
      "Topbar-Metriken sind Feature-Spalten. Vorrang: chart.topbar_metrics bestimmt den " +
        "Metrik-Streifen bei COMPOSE/APPLY/DISPLAY; SET_TOPBAR-Bloecke sind fensterlokal " +
        "und ueberleben ein Re-Render.",
      "",
      "| id | display_name | calc_type | Daten fuers Symbol |",
      "| :-- | :-- | :-- | :-- |",
      ...(rows.length > 0 ? rows.slice(0, 60) : ["| — | keine Metriken gefunden | — | — |"]),
    ];
    if (rows.length > 60) {
      lines.push("", "… " + (rows.length - 60) + " weitere (vollstaendig im JSON unten).");
    }
    return textPayload(lines, { status: "success", action, ...data });
  }

  return null;
}
