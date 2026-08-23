FROM node:22-bookworm-slim

# Install system dependencies & socat for TCP bridge
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    python3 \
    socat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install DeepSeek Harness CLI & Web UI globally + MCP Client
RUN npm install -g @deepseek-ai/dsh@latest @deepseek-ai/dsh-mcp-client@latest

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Default workspace directory
WORKDIR /workspace

EXPOSE 3080 3443

ENTRYPOINT ["/entrypoint.sh"]
