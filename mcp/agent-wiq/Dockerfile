FROM denoland/deno:latest

WORKDIR /app

COPY deno.json ./
RUN deno install

COPY index.ts ./
COPY tools/ ./tools/

EXPOSE 8798

CMD ["run", "-A", "index.ts"]
