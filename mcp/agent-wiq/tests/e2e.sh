#!/usr/bin/env bash
# ============================================================================
# mcp/agent-wiq/tests/e2e.sh — End-to-End-Test des Workitem-MCP-Servers
#
#   bash mcp/agent-wiq/tests/e2e.sh
#
# Legt einen Fixture-Lauf an, spielt Planung -> Ausfuehrung -> Abschluss ueber
# das MCP-Protokoll durch und raeumt am Ende wieder auf.
# ============================================================================
set -euo pipefail

MCP_URL="http://127.0.0.1:8798/mcp"
DB="openbrain-db"
KEY="$(grep -oP '[0-9a-f]{64}' /home/daniel/.dsh/cordis.patch.yml | head -1)"
CI='cccccccc-0000-4000-8000-000000000001'
INITIAL='dddddddd-0000-4000-8000-000000000001'

psql_q() { docker exec -i "$DB" psql -U postgres -d postgres -At -v ON_ERROR_STOP=1 -c "$1"; }

mcp_call() {  # $1 = tool, $2 = arguments-json
  curl -s -m 30 -X POST "$MCP_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "x-brain-key: $KEY" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" \
    | grep '^data:' | sed 's/^data: //'
}

cleanup() { psql_q "delete from change_items where id = '$CI';" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== 0) Fixture aufraeumen und neu anlegen =="
cleanup
psql_q "insert into change_items (id, title, entry_prompt) values ('$CI', 'E2E: Sentiment SPX', 'Wie ist das Sentiment?');" >/dev/null
psql_q "insert into workitems (id, change_item_id, step_key, type, role) values ('$INITIAL', '$CI', 'initial', 'initial', 'lead_engineer');" >/dev/null
echo "   ok"

echo "== 1) workitem_get(initial) =="
mcp_call workitem_get "{\"workitem_id\":\"$INITIAL\"}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
body = d['result']['content'][0]['text']
print('   ' + body.replace(chr(10), chr(10) + '   ')[:400])
assert 'initial' in body, 'step_key fehlt'
"

echo "== 2) workitem_create(initial) — Planung =="
mcp_call workitem_create "{\"workitem_id\":\"$INITIAL\",\"items\":[
  {\"step_key\":\"analyse-social\",\"type\":\"task\",\"role\":\"cco\",\"payload\":{\"prompt\":\"Lies die X-Posts von heute.\"}},
  {\"step_key\":\"bewertung\",\"type\":\"review\",\"role\":\"lead_engineer\",\"depends_on\":[\"analyse-social\"],\"context_refs\":[\"analyse-social\"],\"payload\":{\"prompt\":\"Bewerte, ob der Boss-Prompt erfuellt ist.\"}}
]}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('   ' + d['result']['content'][0]['text'].replace(chr(10), chr(10) + '   '))
"

echo "== 3) Zustand in der DB =="
psql_q "select step_key || ' ' || type || ' ' || status || ' round=' || round from workitems where change_item_id='$CI' order by step_key;" | sed 's/^/   /'
echo "   -- bereit laut View --"
psql_q "select step_key from ready_workitems where change_item_id='$CI' order by step_key;" | sed 's/^/   /'

echo "== 4) INITIAL abschliessen, Task in Lease nehmen (wie es die Sekretaerin taete) =="
psql_q "update workitems set status='done', finished_at=now() where id='$INITIAL';" >/dev/null
TASK_ID=$(psql_q "select id from workitems where change_item_id='$CI' and step_key='analyse-social';")
psql_q "update workitems set status='running', lease_owner='e2e', lease_expires_at=now()+interval '5 min', started_at=now(), attempts=1 where id='$TASK_ID';" >/dev/null
echo "   task=$TASK_ID"

echo "== 5) workitem_finish(task, done, result) =="
mcp_call workitem_finish "{\"workitem_id\":\"$TASK_ID\",\"status\":\"done\",\"result\":{\"sentiment\":\"bullish\",\"posts\":42}}" \
  | python3 -c "import sys,json; print('   ' + json.load(sys.stdin)['result']['content'][0]['text'])"

echo "== 6) workitem_results(task) =="
mcp_call workitem_results "{\"workitem_ids\":[\"$TASK_ID\"]}" \
  | python3 -c "import sys,json; print('   ' + json.load(sys.stdin)['result']['content'][0]['text'].replace(chr(10), chr(10) + '   '))"

echo "== 7) Fehlerpfad: workitem_finish auf bereits abgeschlossenem Item =="
mcp_call workitem_finish "{\"workitem_id\":\"$TASK_ID\",\"status\":\"failed\",\"result\":{}}" \
  | python3 -c "import sys,json; print('   ' + json.load(sys.stdin)['result']['content'][0]['text'])"

echo "== 8) Guard: ein task-Item darf nicht planen =="
mcp_call workitem_create "{\"workitem_id\":\"$TASK_ID\",\"items\":[{\"step_key\":\"x\",\"type\":\"task\",\"role\":\"cco\"}]}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('   isError=' + str(d['result'].get('isError', False)) + ' :: ' + d['result']['content'][0]['text'][:120])"

echo "== 9) Review ist bereit, obwohl nur ein Task existiert =="
psql_q "select step_key from ready_workitems where change_item_id='$CI' order by step_key;" | sed 's/^/   /'

echo "== fertig (Fixture wird beim Beenden entfernt) =="
