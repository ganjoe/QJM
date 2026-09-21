#!/usr/bin/env bash
# Diagnose: ein Tool des draw.io-MCP-Servers aufrufen.
#   bash call.sh tools/list
#   bash call.sh tools/call create_drawio_diagram '{"title":"demo","nodes":[{"id":"a","label":"A"}]}'
set -eo pipefail
URL="http://127.0.0.1:8796/mcp"
KEY="$(grep -oE '[0-9a-f]{64}' /home/daniel/.dsh/cordis.patch.yml | head -1)"
METHOD="$1"
TOOL="$2"
ARGS="$3"
if [ -z "$ARGS" ]; then ARGS='{}'; fi

if [ "$METHOD" = "tools/list" ]; then
  BODY='{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
else
  BODY="{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$TOOL\",\"arguments\":$ARGS}}"
fi

curl -s -m 60 -X POST "$URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "x-brain-key: $KEY" \
  -d "$BODY" \
  | grep '^data:' | sed 's/^data: //' \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
if 'error' in d:
    print('FEHLER:', json.dumps(d['error'])[:400]); raise SystemExit(1)
r = d.get('result', {})
if 'tools' in r:
    for t in r['tools']: print(' ', t['name'])
else:
    print(r['content'][0]['text'])
"
