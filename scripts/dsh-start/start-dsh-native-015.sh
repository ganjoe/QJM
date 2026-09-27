#!/bin/bash
set -e

# Force headless / web mode:
# Prevent DSH from thinking it has a local monitor, which triggers
# zenity desktop popups and xdg-open desktop launches that freeze remote web sessions.
unset DISPLAY WAYLAND_DISPLAY XAUTHORITY

export PATH="/home/daniel/.local/bin:/home/daniel/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export MCP_ACCESS_KEY="${MCP_ACCESS_KEY:-2902dfd74d9d845813783ebb94878f3be17d07dcec6113b792dde4bf9f2b9b1d}"
export DSH_HOME="${DSH_HOME:-${HOME}/.dsh}"

# Default workspace to QJM rather than all of $HOME (prevents slow scanning of .cache, node_modules etc.)
WORKSPACE_DIR="${WORKSPACE_DIR:-/home/daniel/QJM}"
DSH_PORT="${DSH_PORT:-3090}"
HTTP_PROXY_PORT="${HTTP_PROXY_PORT:-3091}"
HTTPS_PROXY_PORT="${HTTPS_PROXY_PORT:-3444}"
PROXY_RETRY_DELAY="${PROXY_RETRY_DELAY:-1}"

SSL_DIR="${DSH_HOME}/ssl"
COMBINED_FILE="${SSL_DIR}/server.pem"

# Clean any lingering proxy listeners
fuser -k "${HTTP_PROXY_PORT}/tcp" 2>/dev/null || true
fuser -k "${HTTPS_PROXY_PORT}/tcp" 2>/dev/null || true

# 1. Start HTTP proxy (0.0.0.0:3091 -> 127.0.0.1:3090)
(while true; do socat "TCP-LISTEN:${HTTP_PROXY_PORT},fork,reuseaddr" "TCP:127.0.0.1:${DSH_PORT}" || true; sleep "${PROXY_RETRY_DELAY}"; done) &
HTTP_PID=$!

# 2. Start HTTPS proxy (0.0.0.0:3444 -> 127.0.0.1:3090)
HTTPS_PID=""
if [ -f "${COMBINED_FILE}" ]; then
    (while true; do socat "OPENSSL-LISTEN:${HTTPS_PROXY_PORT},cert=${COMBINED_FILE},verify=0,fork,reuseaddr" "TCP:127.0.0.1:${DSH_PORT}" || true; sleep "${PROXY_RETRY_DELAY}"; done) &
    HTTPS_PID=$!
fi

cleanup() {
    echo "Stopping DSH and proxies..."
    if [ -n "${DSH_PID:-}" ]; then
        kill -TERM "${DSH_PID}" 2>/dev/null || true
    fi
    if [ -n "${HTTP_PID:-}" ]; then
        kill -TERM "${HTTP_PID}" 2>/dev/null || true
    fi
    if [ -n "${HTTPS_PID:-}" ]; then
        kill -TERM "${HTTPS_PID}" 2>/dev/null || true
    fi
    fuser -k "${HTTP_PROXY_PORT}/tcp" 2>/dev/null || true
    fuser -k "${HTTPS_PROXY_PORT}/tcp" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting Native DeepSeek Harness on 127.0.0.1:${DSH_PORT} in ${WORKSPACE_DIR} (Headless Web Mode)..."
cd "${WORKSPACE_DIR}"

/home/daniel/.local/bin/dsh --profile web --host 127.0.0.1 --port "${DSH_PORT}" --no-open \
    --trusted-host localhost \
    --trusted-host "localhost:${DSH_PORT}" \
    --trusted-host "localhost:${HTTP_PROXY_PORT}" \
    --trusted-host "localhost:${HTTPS_PROXY_PORT}" \
    --trusted-host 127.0.0.1 \
    --trusted-host "127.0.0.1:${DSH_PORT}" \
    --trusted-host "127.0.0.1:${HTTP_PROXY_PORT}" \
    --trusted-host "127.0.0.1:${HTTPS_PROXY_PORT}" \
    --trusted-host daniel-pc \
    --trusted-host "daniel-pc:${DSH_PORT}" \
    --trusted-host "daniel-pc:${HTTP_PROXY_PORT}" \
    --trusted-host "daniel-pc:${HTTPS_PROXY_PORT}" \
    --trusted-host 10.20.0.23 \
    --trusted-host "10.20.0.23:${DSH_PORT}" \
    --trusted-host "10.20.0.23:${HTTP_PROXY_PORT}" \
    --trusted-host "10.20.0.23:${HTTPS_PROXY_PORT}" \
    --trusted-host 10.223.123.1 \
    --trusted-host "10.223.123.1:${DSH_PORT}" \
    --trusted-host "10.223.123.1:${HTTP_PROXY_PORT}" \
    --trusted-host "10.223.123.1:${HTTPS_PROXY_PORT}" &
DSH_PID=$!

wait "${DSH_PID}"
