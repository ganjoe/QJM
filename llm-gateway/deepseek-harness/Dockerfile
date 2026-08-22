FROM node:22-bookworm-slim

# Install system dependencies for agent tasks (git, curl, ca-certificates, python3)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install DeepSeek Harness CLI & Web UI globally
RUN npm install -g @deepseek-ai/dsh@latest

# Default workspace directory
WORKDIR /workspace

EXPOSE 3080

ENV PORT=3080
ENV HOST=0.0.0.0

CMD ["dsh", "web", "--host", "0.0.0.0", "--port", "3080", "--no-open"]
