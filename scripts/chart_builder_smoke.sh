#!/usr/bin/env bash
# Chart-Builder Smoke-Test (Vertrag: docs/architecture/chart-presets.md, Plan
# CHARTVIEWER_CHART_BUILDER_API_PLAN.md Abschnitt 5.3).
#
# READ-ONLY: listet Fenster, liest den Fensterinhalt, zeigt Topbar-Metriken und
# einen Diff gegen das gespeicherte Chart. Aendert nichts.
#
# Aufruf:  scripts/chart_builder_smoke.sh [window_id]
#          CHART_VIEWER_API_URL=http://host:8766 scripts/chart_builder_smoke.sh
#
# Hinweis: ADD_PANE / REMOVE_PANE / REORDER_PANES / DUPLICATE_CHART / DELETE_CHART
# sind MCP-Aktionen (mcp/agent-pca/tools/chart_builder.ts). Sie setzen sich aus
# den HTTP-Aktionen dieses Skripts zusammen (GET_CHART_STATE + COMPOSE_CHART
# bzw. der PCA-Chart-API).
#
# Mutierende HTTP-Schritte (bewusst nicht automatisch, Beispiel win_nvda_1d):
#   # Panes neu setzen (das ist, was ADD_PANE unter der Haube tut):
#   curl -s -X POST $BASE/api/command -H 'Content-Type: application/json' \
#     -d '{"action":"COMPOSE_CHART","window_id":"win_nvda_1d","panes":[
#          {"pane_id":"main","pane_preset_id":"default__main","scale":"linear","weight":7},
#          {"pane_id":"rs","pane_preset_id":"breadth_line__rs","scale":"linear","weight":3},
#          {"pane_id":"volume","pane_preset_id":"builtin:volume","scale":"linear","weight":2}]}'
#   # Draft speichern (overwrite=false schuetzt vor stillem Ueberschreiben):
#   curl -s -X POST $BASE/api/command -H 'Content-Type: application/json' \
#     -d '{"action":"SAVE_CHART","window_id":"win_nvda_1d","chart_id":"mein_chart","overwrite":false}'
#   # Aufraeumen / zurueck auf das gespeicherte Chart:
#   curl -s -X POST $BASE/api/command -H 'Content-Type: application/json' \
#     -d '{"action":"APPLY_CHART","window_id":"win_nvda_1d","chart_id":"default"}'

set -u
BASE="${CHART_VIEWER_API_URL:-http://127.0.0.1:8766}"
WINDOW="${1:-}"

post() {
  curl -s -m 30 -X POST "$BASE/api/command" -H 'Content-Type: application/json' -d "$1"
}

say() { printf '\n=== %s ===\n' "$1"; }

say "LIST_WINDOWS"
post '{"action":"LIST_WINDOWS"}' | python3 -m json.tool

if [ -z "$WINDOW" ]; then
  WINDOW="$(post '{"action":"LIST_WINDOWS"}' | python3 -c 'import json,sys
d = json.load(sys.stdin)
wins = [w for w in (d.get("data") or {}).get("windows", []) if w.get("kind") == "chart"]
print(wins[0]["window_id"] if wins else "")')"
fi
if [ -z "$WINDOW" ]; then
  echo "Kein Chartfenster offen - nichts weiter zu pruefen."
  exit 0
fi
echo "Fenster: $WINDOW"

say "GET_CHART_STATE $WINDOW"
STATE="$(post "{\"action\":\"GET_CHART_STATE\",\"window_id\":\"$WINDOW\"}")"
echo "$STATE" | python3 -m json.tool

say "LIST_TOPBAR_METRICS"
post '{"action":"LIST_TOPBAR_METRICS"}' | python3 -c 'import json,sys
d = json.load(sys.stdin)
metrics = ((d.get("data") or {}).get("metrics") or [])
print(str(len(metrics)) + " Metriken, z.B.: " + ", ".join(m["id"] for m in metrics[:12]))'

CHART="$(echo "$STATE" | python3 -c 'import json,sys
d = json.load(sys.stdin)
print((d.get("data") or {}).get("chart_id") or "")')"
if [ -n "$CHART" ]; then
  say "DIFF_CHART $WINDOW vs $CHART"
  post "{\"action\":\"DIFF_CHART\",\"window_id\":\"$WINDOW\",\"chart_id\":\"$CHART\"}" | python3 -m json.tool
else
  echo "Fenster zeigt einen unbenannten Draft - DIFF_CHART uebersprungen."
fi

printf '\nFertig (read-only).\n'
