FROM node:22-bookworm-slim

# Install system dependencies & socat for TCP bridge
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    git \
    curl \
    ca-certificates \
    python3 \
    ripgrep \
    socat \
    openssl \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /bin/bash /usr/local/bin/bash && \
    ln -sf /usr/bin/rg /usr/bin/ripgrep && \
    ln -sf /usr/bin/rg /usr/local/bin/ripgrep

ENV SHELL=/bin/bash
ENV PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

WORKDIR /app

# Install DeepSeek Harness CLI & Web UI globally + MCP Client + landlock sandbox binary
RUN npm install -g @deepseek-ai/dsh@next @deepseek-ai/dsh-mcp-client@next && \
    cd /usr/local/lib/node_modules/@deepseek-ai/dsh && \
    npm install @deepseek-ai/node-addon-landlock-run-linux-x64 --no-save && \
    chmod +x node_modules/@deepseek-ai/node-addon-landlock-run-linux-x64/bin/landlock-run 2>/dev/null || true

# Patch DSH to allow LAN access for loopback-restricted APIs
RUN sed -i 's/isTrustedApiRequest(request, \[\])/isTrustedApiRequest(request, typeof trustedHosts !== "undefined" ? trustedHosts : this.trustedHosts)/g' /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-client-connection/lib/index.js

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Default workspace directory
WORKDIR /workspace

EXPOSE 3080 3443

ENTRYPOINT ["/entrypoint.sh"]
