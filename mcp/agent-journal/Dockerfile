FROM denoland/deno:latest

WORKDIR /app

# Copy dependency manifest first for layer caching
COPY deno.json ./
RUN deno install

# Application code & prompts
COPY index.ts ./
COPY tools/ ./tools/
COPY prompts/ ./prompts/

EXPOSE 8801

CMD ["run", "-A", "index.ts"]
