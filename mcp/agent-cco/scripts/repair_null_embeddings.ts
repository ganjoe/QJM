// Maintenance: re-embed yt_chunks that are marked 'embedded' but have a NULL vector.
// Cause: historical partial embedding responses (now guarded in the workers).
// Run inside the CCO container:
//   deno run -A /app/scripts/repair_null_embeddings.ts
import { supabase, getDocumentEmbeddingsDetailed, log } from "../tools/shared.ts";

const { data, error } = await supabase
  .from("agent_workspace")
  .select("id, content, video_id, chunk_index")
  .eq("artifact_type", "yt_chunk")
  .eq("status", "embedded")
  .is("embedding", null);

if (error) throw new Error(error.message);
if (!data || data.length === 0) {
  log.info("[repair_null_embeddings] Nichts zu tun — keine NULL-Vektoren.");
  Deno.exit(0);
}

log.info(`[repair_null_embeddings] ${data.length} NULL-Vektoren werden neu berechnet...`);
const BATCH = 8;
let ok = 0;
for (let i = 0; i < data.length; i += BATCH) {
  const slice = data.slice(i, i + BATCH);
  const vecs = (await getDocumentEmbeddingsDetailed(slice.map((r: any) => r.content), "x_post")).vectors;
  if (!Array.isArray(vecs) || vecs.length !== slice.length) {
    throw new Error(`Vektor-Anzahl ${vecs?.length ?? 0} != ${slice.length} Inputs`);
  }
  for (let j = 0; j < slice.length; j++) {
    const v = vecs[j];
    if (!Array.isArray(v) || v.length === 0) {
      log.error(`Leerer Vektor fuer ${slice[j].id} — uebersprungen.`);
      continue;
    }
    const { error: upErr } = await supabase
      .from("agent_workspace")
      .update({ embedding: v })
      .eq("id", slice[j].id);
    if (upErr) {
      log.error(`Update fehlgeschlagen fuer ${slice[j].id}: ${upErr.message}`);
      continue;
    }
    ok++;
  }
}
log.info(`[repair_null_embeddings] ${ok}/${data.length} repariert.`);
