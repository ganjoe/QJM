#!/usr/bin/env bash
# E2E-Test: create -> Datei -> read -> Traversal-Negativtest. Raeumt am Ende auf.
set -euo pipefail
DIR="/home/daniel/QJM/dsh_playground/drawio"
URL="http://127.0.0.1:8796/mcp"
KEY="$(grep -oE '[0-9a-f]{64}' /home/daniel/.dsh/cordis.patch.yml | head -1)"
NAME="qjm-drawio-e2e.drawio"

call() {
  local tool="$1"; local args="$2"
  curl -s -m 30 -X POST "$URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "x-brain-key: $KEY" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" \
    | grep '^data:' | sed 's/^data: //'
}

cleanup() { rm -f "$DIR/$NAME"; }
trap cleanup EXIT

echo "== create =="
call create_drawio_diagram "{\"title\":\"qjm-drawio-e2e\",\"nodes\":[{\"id\":\"a\",\"label\":\"A\"},{\"id\":\"b\",\"label\":\"B\"}],\"edges\":[{\"source\":\"a\",\"target\":\"b\"}],\"overwrite\":true}" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['content'][0]['text'][:300])"

echo "== Datei vorhanden =="
test -f "$DIR/$NAME" && echo "   ok: $DIR/$NAME"

echo "== read =="
call read_drawio_diagram "{\"filename\":\"$NAME\"}" | python3 -c "import sys,json; t=json.load(sys.stdin)['result']['content'][0]['text']; assert '<mxfile' in t; print('   ok, XML len', len(t))"

echo "== Traversal-Negativtest =="
call read_drawio_diagram "{\"filename\":\"../../etc/passwd\"}" | python3 -c "import sys,json; d=json.load(sys.stdin)['result']; assert d.get('isError') is True; print('   ok, abgelehnt:', d['content'][0]['text'][:120])"

echo "== fertig =="
