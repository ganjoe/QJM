#!/usr/bin/env bash
# Traegt den globalen DSH-Client fuer den QJM draw.io-MCP-Server in
# ~/.dsh/cordis.patch.yml ein (idempotent).
#
#   bash scripts/install-drawio-mcp-global.sh
#
# Danach den DSH-Host neu starten, damit neue Sessions die draw.io-Tools sehen.
set -euo pipefail

PATCH="$HOME/.dsh/cordis.patch.yml"
if [ ! -f "$PATCH" ]; then
  echo "Nicht gefunden: $PATCH" >&2
  exit 1
fi

if grep -q 'mcp-openbrain-drawio' "$PATCH"; then
  echo "mcp-openbrain-drawio ist bereits in $PATCH eingetragen."
else
  cat >> "$PATCH" <<'YAML'

  - id: mcp-openbrain-drawio
    name: "@deepseek-ai/dsh-mcp-client"
    config:
      serverName: openbrain-drawio
      transport: streamable-http
      url: !!js process.env.OPENBRAIN_DRAWIO_MCP_URL || "http://127.0.0.1:8796"
      headers:
        x-brain-key: !!js process.env.MCP_ACCESS_KEY || "2902dfd74d9d845813783ebb94878f3be17d07dcec6113b792dde4bf9f2b9b1d"
YAML
  echo "Eingetragen in $PATCH"
fi

echo "Verifikation:"
grep -n -A 8 'mcp-openbrain-drawio' "$PATCH" || true
echo
echo "Jetzt den DSH-Host neu starten."
