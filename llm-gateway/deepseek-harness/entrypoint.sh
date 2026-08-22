#!/bin/sh
set -e

echo "Starting socat proxy on 0.0.0.0:3080 -> 127.0.0.1:3081..."
(while true; do socat TCP-LISTEN:3080,fork,reuseaddr TCP:127.0.0.1:3081 || true; sleep 1; done) &

echo "Starting DeepSeek Harness on 127.0.0.1:3081..."
exec dsh web --port 3081 --no-open
