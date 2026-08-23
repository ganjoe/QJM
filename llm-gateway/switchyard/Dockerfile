# Stage 1: Build switchyard-server using latest Rust cargo
FROM rust:latest AS builder

WORKDIR /usr/src/switchyard
RUN cargo install --locked switchyard-server

# Stage 2: Unified Gateway runtime (Switchyard Rust engine + Embeddings Router)
FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    python3 \
    python3-pip \
    python3-fastapi \
    python3-uvicorn \
    python3-httpx \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/cargo/bin/switchyard-server /usr/local/bin/switchyard-server

WORKDIR /app
COPY gateway.py /app/gateway.py

EXPOSE 4000

ENV CONFIG_PATH=/config/routes.toml
ENV PORT=4000
ENV HOST=0.0.0.0

CMD ["python3", "/app/gateway.py"]
