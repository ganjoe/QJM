// MXBAI-Migration: Re-Embed der kleinen Nebentabellen (open_brain, x_users, yt_channels).
//
// Diese drei Tabellen haben KEINEN eigenen Worker — ihre Vektoren entstehen in den Tools
// (capture_thought, manage_influencers ADD, manage_youtube_channels ADD). Nach dem
// Modellwechsel muessen sie einmalig neu berechnet werden.
//
// Die Texte werden EXAKT so aufgebaut wie in den Tools (sonst passen Query und Dokument
// nicht mehr zusammen) und mit fitForEmbedding auf das 512-Token-Fenster gekuerzt.
//
// Aufruf im CCO-Container (idempotent, beliebig wiederholbar):
//   docker exec llm-gw-mcp-cco deno run -A /app/scripts/reembed_misc_tables.ts
import { supabase, getEmbeddingsBatch, fitForEmbedding, log, EMBED_DIM } from "../tools/shared.ts";

const BATCH = 8;

interface Job {
  table: "open_brain" | "x_users" | "yt_channels";
  key: string;
  text: (row: any) => string;
}

const JOBS: Job[] = [
  {
    table: "open_brain",
    key: "id",
    text: (r) => fitForEmbedding(r.content || ""),
  },
  {
    table: "x_users",
    key: "username",
    text: (r) => fitForEmbedding(`username: ${r.username} screen_name: ${r.screen_name || ""} notes: ${r.notes || ""}`),
  },
  {
    table: "yt_channels",
    key: "handle",
    text: (r) => fitForEmbedding(`handle: ${r.handle} title: ${r.title || ""} notes: ${r.notes || ""}`),
  },
];

let failures = 0;

for (const job of JOBS) {
  const cols = job.table === "open_brain"
    ? "id, content"
    : job.table === "x_users"
    ? "username, screen_name, notes"
    : "handle, title, notes";

  const { data, error } = await supabase.from(job.table).select(cols);
  if (error) {
    log.error(`[${job.table}] Laden fehlgeschlagen: ${error.message}`);
    failures++;
    continue;
  }
  const rows = (data || []) as any[];
  if (rows.length === 0) {
    log.info(`[${job.table}] Keine Zeilen — uebersprungen.`);
    continue;
  }

  let ok = 0;
  for (let i = 0; i < rows.length; i += BATCH) {
    const slice = rows.slice(i, i + BATCH);
    const texts = slice.map(job.text);
    const vectors = await getEmbeddingsBatch(texts, "x_post");

    if (!Array.isArray(vectors) || vectors.length !== slice.length) {
      throw new Error(`[${job.table}] Vektor-Anzahl ${vectors?.length ?? 0} != ${slice.length}`);
    }
    for (let j = 0; j < slice.length; j++) {
      const v = vectors[j];
      if (!Array.isArray(v) || v.length !== EMBED_DIM) {
        log.error(`[${job.table}] Ungueltige Dimension (${v?.length ?? 0} != ${EMBED_DIM}) fuer ${slice[j][job.key]} — uebersprungen.`);
        failures++;
        continue;
      }
      const { error: upErr } = await supabase.from(job.table).update({ embedding: v }).eq(job.key, slice[j][job.key]);
      if (upErr) {
        log.error(`[${job.table}] Update fehlgeschlagen fuer ${slice[j][job.key]}: ${upErr.message}`);
        failures++;
        continue;
      }
      ok++;
    }
  }
  log.info(`[${job.table}] ${ok}/${rows.length} Vektoren neu berechnet.`);
}

if (failures > 0) {
  log.error(`[reembed_misc_tables] ${failures} Fehler — Skript erneut ausfuehren.`);
  Deno.exit(1);
}
log.info("[reembed_misc_tables] Fertig.");
