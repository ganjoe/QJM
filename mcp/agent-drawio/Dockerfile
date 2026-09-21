FROM denoland/deno:latest

WORKDIR /app

COPY deno.json ./
RUN deno install

COPY index.ts ./
COPY stdio.ts ./
COPY tools/ ./tools/

# Der Container laeuft als uid/gid 1000 (Compose user:), damit die erzeugten
# .drawio-Dateien im dsh_playground dem Nutzer gehoeren. Daher Code und
# Deno-Cache auf 1000 uebergeben.
RUN chown -R 1000:1000 /app /deno-dir

EXPOSE 8796

CMD ["run", "-A", "index.ts"]
