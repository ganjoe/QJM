#!/bin/bash
# =============================================================================
# start-dsh-native-017.sh - DSH 0.1.7-rc.2 aus eigenem Checkout mit eigenem Home
#
# Zweck: Parallelinstanz (Testkanal auf 3092) ODER Produktion auf 3090
# (switch_dsh.sh --to 017 setzt dafuer die Umgebungsvariablen).
#
# Parameter (Umgebung, sonst Defaults):
#   DSH_CHECKOUT        Default /home/daniel/deepseek-harness-017
#   DSH_HOME            Default /home/daniel/.dsh-017
#   DSH_PORT            Default 3092
#   HTTP_PROXY_PORT     Default 3093
#   HTTPS_PROXY_PORT    Default 3445
#   WORKSPACE_DIR       Default /home/daniel/QJM
#
# Die 0.1.5-Produktion (~/.dsh, Port 3090) wird von diesem Skript NIE angefasst.
# =============================================================================
set -e

unset DISPLAY WAYLAND_DISPLAY XAUTHORITY

export PATH="/home/daniel/.local/bin:/home/daniel/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
[ -n "$MCP_ACCESS_KEY" ] || MCP_ACCESS_KEY="2902dfd74d9d845813783ebb94878f3be17d07dcec6113b792dde4bf9f2b9b1d"
export MCP_ACCESS_KEY

[ -n "$DSH_CHECKOUT" ] || DSH_CHECKOUT="/home/daniel/deepseek-harness-017"
[ -n "$DSH_HOME" ] || DSH_HOME="/home/daniel/.dsh-017"
[ -n "$DSH_PORT" ] || DSH_PORT="3092"
[ -n "$HTTP_PROXY_PORT" ] || HTTP_PROXY_PORT="3093"
[ -n "$HTTPS_PROXY_PORT" ] || HTTPS_PROXY_PORT="3445"
[ -n "$WORKSPACE_DIR" ] || WORKSPACE_DIR="/home/daniel/QJM"

if [ "$DSH_HOME" = "/home/daniel/.dsh" ]; then
  echo "ABBRUCH: DSH_HOME zeigt auf das Produktions-Home (~/.dsh). Das ist Aufgabe von ~/start-dsh-native.sh." >&2
  exit 1
fi
if [ ! -f "$DSH_CHECKOUT/apps/cli/lib/bin.js" ]; then
  echo "ABBRUCH: $DSH_CHECKOUT ist nicht gebaut (apps/cli/lib/bin.js fehlt)." >&2
  exit 1
fi

SSL_DIR="$DSH_HOME/ssl"
COMBINED_FILE="$SSL_DIR/server.pem"

fuser -k "$HTTP_PROXY_PORT/tcp" 2>/dev/null || true
fuser -k "$HTTPS_PROXY_PORT/tcp" 2>/dev/null || true

# HTTP-Proxy: 0.0.0.0:HTTP_PROXY_PORT -> 127.0.0.1:DSH_PORT
( while true; do socat "TCP-LISTEN:$HTTP_PROXY_PORT,fork,reuseaddr" "TCP:127.0.0.1:$DSH_PORT" || true; sleep 1; done ) &
HTTP_PID=$!

HTTPS_PID=""
if [ -f "$COMBINED_FILE" ]; then
  ( while true; do socat "OPENSSL-LISTEN:$HTTPS_PROXY_PORT,cert=$COMBINED_FILE,verify=0,fork,reuseaddr" "TCP:127.0.0.1:$DSH_PORT" || true; sleep 1; done ) &
  HTTPS_PID=$!
fi

cleanup() {
  echo "Stoppe DSH-017 und Proxies..."
  [ -n "$DSH_PID" ] && kill -TERM "$DSH_PID" 2>/dev/null || true
  [ -n "$HTTP_PID" ] && kill -TERM "$HTTP_PID" 2>/dev/null || true
  [ -n "$HTTPS_PID" ] && kill -TERM "$HTTPS_PID" 2>/dev/null || true
  fuser -k "$HTTP_PROXY_PORT/tcp" 2>/dev/null || true
  fuser -k "$HTTPS_PROXY_PORT/tcp" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

TRUSTED=""
for h in localhost 127.0.0.1 daniel-pc 10.20.0.23 10.223.123.1; do
  TRUSTED="$TRUSTED --trusted-host $h"
  TRUSTED="$TRUSTED --trusted-host $h:$DSH_PORT"
  TRUSTED="$TRUSTED --trusted-host $h:$HTTP_PROXY_PORT"
  TRUSTED="$TRUSTED --trusted-host $h:$HTTPS_PROXY_PORT"
done

echo "Starte DSH 0.1.7-rc.2 auf 127.0.0.1:$DSH_PORT (Home $DSH_HOME, Checkout $DSH_CHECKOUT) ..."
cd "$WORKSPACE_DIR"

node "$DSH_CHECKOUT/apps/cli/lib/bin.js" --profile web --host 127.0.0.1 --port "$DSH_PORT" --no-open $TRUSTED &
DSH_PID=$!

wait "$DSH_PID"
