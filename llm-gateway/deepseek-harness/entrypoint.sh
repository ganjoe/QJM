#!/bin/sh
set -e

# Ensure all files and directories created by DSH are readable and writable by the host user
umask 0000

export SHELL=/bin/bash
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
ln -sf /bin/bash /usr/local/bin/bash 2>/dev/null || true
ln -sf /usr/bin/rg /usr/bin/ripgrep 2>/dev/null || true
ln -sf /usr/bin/rg /usr/local/bin/ripgrep 2>/dev/null || true
ln -sf /usr/bin/rg /usr/local/bin/rg 2>/dev/null || true

# DSH requires .credentials.yaml to be owner-only readable (mode 600)
[ -f /root/.dsh/.credentials.yaml ] && chmod 600 /root/.dsh/.credentials.yaml 2>/dev/null || true
[ -f /root/.config/dsh/.credentials.yaml ] && chmod 600 /root/.config/dsh/.credentials.yaml 2>/dev/null || true

SSL_DIR="/root/.config/dsh/ssl"
CERT_FILE="${SSL_DIR}/cert.pem"
KEY_FILE="${SSL_DIR}/key.pem"
COMBINED_FILE="${SSL_DIR}/server.pem"

# Auto-generate self-signed SSL certificate with SAN if missing
if [ ! -f "${COMBINED_FILE}" ]; then
    echo "Generating self-signed TLS/SSL certificate for HTTPS..."
    mkdir -p "${SSL_DIR}"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "${KEY_FILE}" \
        -out "${CERT_FILE}" \
        -subj "/CN=10.20.0.23" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:10.20.0.23,IP:10.0.0.1,IP:192.168.1.1" 2>/dev/null || \
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "${KEY_FILE}" \
        -out "${CERT_FILE}" \
        -subj "/CN=10.20.0.23" 2>/dev/null

    cat "${CERT_FILE}" "${KEY_FILE}" > "${COMBINED_FILE}"
    chmod 644 "${CERT_FILE}" "${COMBINED_FILE}" 2>/dev/null || true
    chmod 600 "${KEY_FILE}" 2>/dev/null || true
    echo "TLS certificate ready in ${SSL_DIR}."
fi

# HTTP Proxy on 3080 -> 127.0.0.1:3081
echo "Starting HTTP proxy on 0.0.0.0:3080 -> 127.0.0.1:3081..."
(while true; do socat TCP-LISTEN:3080,fork,reuseaddr TCP:127.0.0.1:3081 || true; sleep 1; done) &

# HTTPS Proxy on 3443 -> 127.0.0.1:3081
echo "Starting HTTPS proxy on 0.0.0.0:3443 -> 127.0.0.1:3081..."
(while true; do socat OPENSSL-LISTEN:3443,cert="${COMBINED_FILE}",verify=0,fork,reuseaddr TCP:127.0.0.1:3081 || true; sleep 1; done) &

echo "Starting DeepSeek Harness on 127.0.0.1:3081..."
exec dsh web --port 3081 --no-open \
    --trusted-host localhost \
    --trusted-host localhost:3080 \
    --trusted-host localhost:3443 \
    --trusted-host localhost:3081 \
    --trusted-host 127.0.0.1 \
    --trusted-host 127.0.0.1:3080 \
    --trusted-host 127.0.0.1:3443 \
    --trusted-host 127.0.0.1:3081 \
    --trusted-host 10.20.0.23 \
    --trusted-host 10.20.0.23:3080 \
    --trusted-host 10.20.0.23:3443 \
    --trusted-host 10.20.0.23:3081 \
    --trusted-host 10.223.123.1 \
    --trusted-host 10.223.123.1:3080 \
    --trusted-host 10.223.123.1:3443 \
    --trusted-host 10.223.123.1:3081
