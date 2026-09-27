/**
 * Tests fuer die Setup->Chart-Referenzsuche (DELETE_CHART-Schutz).
 *
 * Bewusst ohne Imports: Deno.test ist global, die Assertions sind handgeschrieben -
 * damit laeuft der Test ohne Netz, ohne npm-Cache und ohne Env.
 *
 *   deno test tools/chart_refs_test.ts
 */

import { chartReferences, findChartReferences } from "./chart_refs.ts";

function assert(condition: unknown, message: string): void {
  if (!condition) throw new Error("Assertion fehlgeschlagen: " + message);
}

function assertEquals(actual: unknown, expected: unknown, message: string): void {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) throw new Error("Assertion fehlgeschlagen: " + message + " (ist " + a + ", erwartet " + b + ")");
}

const SETUPS = [
  {
    setup_name: "desk",
    variants: [
      {
        id: "v1",
        monitor_count: 2,
        windows: [
          { window_id: "win_a", chart_id: "momentum", symbol: "NVDA" },
          { window_id: "win_b", chart_id: "default", symbol: "AMD" },
        ],
      },
    ],
  },
  { setup_name: "leer", variants: [{ id: "v2", monitor_count: 1, windows: [] }] },
];

Deno.test("chartReferences findet chart_id strukturell", () => {
  assert(chartReferences(SETUPS[0], "momentum"), "momentum steckt in desk");
  assert(chartReferences(SETUPS[0], "default"), "default steckt in desk");
  assert(!chartReferences(SETUPS[0], "gibt_es_nicht"), "unbekanntes Chart darf nicht matchen");
  assert(!chartReferences(SETUPS[1], "momentum"), "leeres Setup darf nicht matchen");
});

Deno.test("chartReferences findet auch nackte Strings und tiefe Listen", () => {
  assert(chartReferences({ charts: ["a", "b", "c"] }, "b"), "String in Liste");
  assert(chartReferences([{ x: [{ y: { chart_id: "tief" } }] }], "tief"), "verschachtelt");
  assert(!chartReferences({ charts: ["a"], chart_id: "a" }, "b"), "kein Zufallstreffer");
});

Deno.test("findChartReferences liefert die Setup-Namen (dedupliziert, sortiert)", () => {
  assertEquals(findChartReferences(SETUPS, "momentum"), ["desk"], "ein Treffer");
  assertEquals(findChartReferences(SETUPS, "default"), ["desk"], "zweiter Treffer im selben Setup");
  assertEquals(findChartReferences(SETUPS, "gibt_es_nicht"), [], "kein Treffer");
  assertEquals(findChartReferences(undefined, "momentum"), [], "kaputte Antwort");
  assertEquals(
    findChartReferences([{ name: "b_setup", variants: [{ windows: [{ chart_id: "x" }] }] },
                         { name: "a_setup", windows: [{ chart_id: "x" }] }], "x"),
    ["a_setup", "b_setup"],
    "sortiert und dedupliziert",
  );
});
