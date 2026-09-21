#!/usr/bin/env bash
# ============================================================================
# mcp/agent-wiq/tests/rounds.sh — Runden-Mechanik von workitem_create
#
# Prueft deterministisch: ein review-Item legt mit workitem_create Items in
# Runde+1 an, inklusive Kanten, und die neue Runde ist ein eigener Namensraum.
# ============================================================================
set -euo pipefail

MCP_URL="http://127.0.0.1:8798/mcp"
DB="openbrain-db"
KEY="$(grep -oP '[0-9a-f]{64}' /home/daniel/.dsh/cordis.patch.yml | head -1)"
CI='eeeeeeee-0000-4000-8000-000000000001'
REVIEW='eeeeeeee-0000-4000-8000-000000000002'

psql_q() { docker exec -i "$DB" psql -U postgres -d postgres -At -v ON_ERROR_STOP=1 -c "$1"; }
mcp_call() {
  curl -s -m 30 -X POST "$MCP_URL" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "x-brain-key: $KEY" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" \
    | grep '^data:' | sed 's/^data: //'
}
cleanup() { psql_q "delete from change_items where id = '$CI';" >/dev/null 2>&1 || true; }
trap cleanup EXIT

cleanup
psql_q "insert into change_items (id, title, entry_prompt) values ('$CI','Runden-Test','egal');" >/dev/null
psql_q "insert into workitems (id, change_item_id, step_key, type, role, round) values ('$REVIEW','$CI','review-r1','review','lead_engineer',1);" >/dev/null

echo "== Review (Runde 1) plant Runde 2 =="
mcp_call workitem_create "{\"workitem_id\":\"$REVIEW\",\"items\":[
  {\"step_key\":\"zweiter-versuch\",\"type\":\"task\",\"role\":\"cco\",\"payload\":{\"prompt\":\"anderer Ansatz\"}},
  {\"step_key\":\"review-r2\",\"type\":\"review\",\"role\":\"lead_engineer\",\"depends_on\":[\"zweiter-versuch\"],\"context_refs\":[\"zweiter-versuch\"]}
]}" | python3 -c "import sys,json; print('   ' + json.load(sys.stdin)['result']['content'][0]['text'].replace(chr(10), chr(10)+'   '))"

echo "== Runden und Kanten in der DB =="
psql_q "select step_key || ' | runde ' || round from workitems where change_item_id='$CI' order by round, step_key;" | sed 's/^/   /'
echo "   -- bereit --"
psql_q "select step_key || ' (runde ' || round || ')' from ready_workitems where change_item_id='$CI' order by step_key;" | sed 's/^/   /'

echo "== Grenzfall: Verweis auf einen step_key aus Runde 1 =="
mcp_call workitem_create "{\"workitem_id\":\"$REVIEW\",\"items\":[{\"step_key\":\"x\",\"type\":\"task\",\"role\":\"cco\",\"depends_on\":[\"review-r1\"]}]}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('   isError=' + str(d['result'].get('isError', False)) + ' :: ' + d['result']['content'][0]['text'][:130])"

echo "== Grenzfall: doppelter step_key in derselben Runde =="
mcp_call workitem_create "{\"workitem_id\":\"$REVIEW\",\"items\":[{\"step_key\":\"zweiter-versuch\",\"type\":\"task\",\"role\":\"cco\"}]}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('   isError=' + str(d['result'].get('isError', False)) + ' :: ' + d['result']['content'][0]['text'][:130])"
