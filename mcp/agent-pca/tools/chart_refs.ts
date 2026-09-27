/**
 * Setup -> Chart-Referenzen (Grundlage fuer den DELETE_CHART-Schutz).
 *
 * Bewusst ohne Importe: die Funktionen sind rein und damit ohne MCP-Client,
 * Netz oder Env testbar (siehe chart_refs_test.ts).
 */

/** Tiefe Suche: referenziert dieser Setup-Eintrag das Chart? */
export function chartReferences(node: unknown, chartId: string): boolean {
  if (typeof node === "string") return node === chartId;
  if (node === null || typeof node !== "object") return false;
  if (Array.isArray(node)) return node.some((item) => chartReferences(item, chartId));
  if ((node as Record<string, unknown>).chart_id === chartId) return true;
  return Object.values(node as Record<string, unknown>).some((value) =>
    chartReferences(value, chartId)
  );
}

/** Namen aller Setups, die das Chart referenzieren (dedupliziert, sortiert). */
export function findChartReferences(setups: unknown, chartId: string): string[] {
  const hits = new Set<string>();
  for (const entry of Array.isArray(setups) ? setups : []) {
    if (!chartReferences(entry, chartId)) continue;
    const record = (entry ?? {}) as Record<string, unknown>;
    hits.add(String(record.name || record.setup_name || record.id || "?"));
  }
  return [...hits].sort();
}
