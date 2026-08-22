# Stage 1: Build switchyard-server using latest Rust cargo
FROM rust:latest AS builder

WORKDIR /usr/src/switchyard
RUN cargo install --locked switchyard-server

# Stage 2: Minimal runtime image matching builder glibc
FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/cargo/bin/switchyard-server /usr/local/bin/switchyard-server

WORKDIR /app

EXPOSE 4000

ENV CONFIG_PATH=/config/routes.toml
ENV PORT=4000
ENV HOST=0.0.0.0

CMD ["sh", "-c", "switchyard-server --config ${CONFIG_PATH} --host ${HOST} --port ${PORT}"]
