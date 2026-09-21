// Chunk-Size-Experiment — read-only. Re-chunkt YT-Transkripte mit mehreren Groessen und
// evaluiert Retrieval (Video-Ebene) auf dem Golden-Set. Schreibt NICHTS in die DB.
//
// Aufruf (isoliert vom Host):
//   docker/deno ... embedding_chunk_experiment.ts --sizes=800,1200,1600 --overlap=200 \
//     --models=qwen3-embedding:0.6b,mxbai-embed-large --distractor-videos=16
//
// Fairness: pro Video wird derselbe Textabschnitt (max-chars-per-video) verarbeitet; die
// Chunk-Anzahl variiert also mit der Groesse (das ist der reale Trade-off).

function arg(name: string, def = ""): string {
  const p = "--" + name + "=";
  const hit = Deno.args.find((a) => a.startsWith(p));
  return hit ? hit.slice(p.length) : def;
}
const SUPABASE_URL = (Deno.env.get("SUPABASE_URL") || "http://127.0.0.1:8001").replace(/\/+$/, "");
const SUPABASE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
const OLLAMA_URL = (arg("ollama", Deno.env.get("OLLAMA_URL") || "http://127.0.0.1:11434")).replace(/\/+$/, "");
const GOLDEN = arg("golden", "/app/scripts/embedding_golden.json");
const SIZES = arg("sizes", "800,1200,1600").split(",").map((s) => parseInt(s)).filter((n) => n > 0);
const OVERLAP = parseInt(arg("overlap", "200"));
const MODELS = arg("models", "mxbai-embed-large").split(",").map((s) => s.trim()).filter(Boolean);
const N_DISTRACTORS = parseInt(arg("distractor-videos", "16"));
const MAX_CHARS = parseInt(arg("max-chars-per-video", "4000"));
const CACHE = arg("cache", "/tmp/chunkexp");
const OUT = arg("out", CACHE + "/result.json");
const BATCH = parseInt(arg("batch", "8"));
const K = parseInt(arg("k", "10"));

interface Video { video_id: string; channel: string; title: string; upload_date: string | null; transcript: string; }
interface Doc { id: string; text: string; video_id: string; }

const hdrs: Record<string, string> = { "Content-Type": "application/json" };
if (SUPABASE_KEY) { hdrs["apikey"] = SUPABASE_KEY; hdrs["Authorization"] = "Bearer " + SUPABASE_KEY; }
async function restGet(path: string): Promise<any[]> {
  const r = await fetch(SUPABASE_URL + "/rest/v1/" + path, { headers: hdrs });
  if (!r.ok) throw new Error("PostgREST " + r.status + ": " + (await r.text()).slice(0, 200));
  return await r.json();
}
function cosine(a: number[], b: number[]): number {
  let dot = 0, na = 0, nb = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
  const den = Math.sqrt(na) * Math.sqrt(nb);
  return den ? dot / den : 0;
}
const PREFIX_MAP: Record<string, { q: string; d: string }> = {
  "mxbai-embed-large": { q: "Represent this sentence for searching relevant passages: ", d: "" },
  "bge-large": { q: "Represent this sentence for searching relevant passages: ", d: "" },
  "nomic-embed-text": { q: "search_query: ", d: "search_document: " },
};
function prefixFor(model: string): { q: string; d: string } { return PREFIX_MAP[model] || { q: "", d: "" }; }

// identisch zur Produktionsfunktion in yt_ingestion_worker.ts
function chunkTranscript(transcript: string, size: number, overlap: number): string[] {
  if (!transcript || transcript.trim().length === 0) return [];
  const text = transcript.trim();
  if (text.length <= size) return [text];
  const chunks: string[] = [];
  let startIndex = 0;
  while (startIndex < text.length) {
    let endIndex = startIndex + size;
    if (endIndex >= text.length) { chunks.push(text.substring(startIndex).trim()); break; }
    const slice = text.substring(startIndex, endIndex);
    const lastBreak = Math.max(slice.lastIndexOf("\n"), slice.lastIndexOf(". "), slice.lastIndexOf("? "), slice.lastIndexOf("! "), slice.lastIndexOf(" "));
    if (lastBreak > size * 0.6) endIndex = startIndex + lastBreak + 1;
    const chunk = text.substring(startIndex, endIndex).trim();
    if (chunk.length > 0) chunks.push(chunk);
    startIndex = Math.max(startIndex + 1, endIndex - overlap);
  }
  return chunks;
}
function headerOf(v: Video): string {
  return "[Video: \"" + v.title + "\" | Kanal: " + v.channel + (v.upload_date ? " | Upload: " + String(v.upload_date).slice(0, 10) : "") + "]";
}
async function embed(model: string, texts: string[]): Promise<{ vectors: number[][]; seconds: number }> {
  const out: number[][] = [];
  const t0 = Date.now();
  for (let i = 0; i < texts.length; i += BATCH) {
    const slice = texts.slice(i, i + BATCH);
    const r = await fetch(OLLAMA_URL + "/api/embed", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model, input: slice, keep_alive: -1 }) });
    if (!r.ok) throw new Error("Ollama " + r.status + ": " + (await r.text()).slice(0, 160));
    const d = await r.json();
    if (!Array.isArray(d?.embeddings) || d.embeddings.length !== slice.length) throw new Error("Vektor-Anzahl " + (d?.embeddings?.length ?? 0) + " != " + slice.length);
    out.push(...d.embeddings);
  }
  return { vectors: out, seconds: (Date.now() - t0) / 1000 };
}
async function cachedEmbed(model: string, key: string, texts: string[]): Promise<{ vectors: number[][]; seconds: number }> {
  const dir = CACHE + "/" + model.replace(/[^a-zA-Z0-9._-]/g, "_");
  await Deno.mkdir(dir, { recursive: true });
  const file = dir + "/" + key + ".json";
  let store: Record<string, number[]> = {};
  try { store = JSON.parse(await Deno.readTextFile(file)); } catch (_e) { /* leer */ }
  const missing = texts.filter((t) => !store[t]);
  let seconds = 0;
  if (missing.length > 0) {
    const res = await embed(model, missing);
    seconds = res.seconds;
    missing.forEach((t, i) => { store[t] = res.vectors[i]; });
    await Deno.writeTextFile(file, JSON.stringify(store));
  }
  return { vectors: texts.map((t) => store[t]), seconds };
}
function metrics(pool: Doc[], vecs: Map<string, number[]>, qv: number[], relevant: Set<string>): { recall1: number; recall5: number; recall: number; ndcg: number; mrr: number } {
  const best = new Map<string, number>();
  for (const d of pool) {
    const v = vecs.get(d.id); if (!v) continue;
    const s = cosine(qv, v); const prev = best.get(d.video_id);
    if (prev === undefined || s > prev) best.set(d.video_id, s);
  }
  const ranked = [...best.entries()].sort((a, b) => b[1] - a[1]).map((e) => e[0]);
  const rank = ranked.findIndex((v) => relevant.has(v));
  const rAt = (k: number) => { if (relevant.size === 0) return 0; const h = ranked.slice(0, k).filter((v) => relevant.has(v)).length; return Math.min(h, relevant.size) / Math.min(k, relevant.size); };
  const topk = ranked.slice(0, K);
  let dcg = 0; topk.forEach((v, i) => { if (relevant.has(v)) dcg += 1 / Math.log2(i + 2); });
  let idcg = 0; for (let i = 0; i < Math.min(relevant.size, K); i++) idcg += 1 / Math.log2(i + 2);
  return { recall1: rAt(1), recall5: rAt(5), recall: rAt(K), ndcg: idcg ? dcg / idcg : 0, mrr: rank >= 0 ? 1 / (rank + 1) : 0 };
}

// ── Videos laden: alle Golden- + N Distraktor-Videos ──────────────────
const golden = JSON.parse(await Deno.readTextFile(GOLDEN));
const queries: { query: string; relevant: string[] }[] = golden.queries;
const goldenVideos = new Set<string>();
for (const q of queries) for (const id of q.relevant) if (!/^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}$/.test(id)) goldenVideos.add(id);

// Query-Begriffe je Golden-Video -> bestes Textfenster waehlen, damit die Antwort enthalten ist.
const termsByVideo = new Map<string, Set<string>>();
for (const q of queries) {
  const terms = q.query.toLowerCase().split(/[^a-z0-9$]+/).filter((t) => t.length > 3);
  for (const id of q.relevant) {
    if (/^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}$/.test(id)) continue;
    const s = termsByVideo.get(id) || new Set<string>();
    terms.forEach((t) => s.add(t));
    termsByVideo.set(id, s);
  }
}
function bestWindow(transcript: string, terms: Set<string>, maxChars: number): string {
  if (!terms || terms.size === 0 || transcript.length <= maxChars) return transcript.slice(0, maxChars);
  const lower = transcript.toLowerCase();
  let bestStart = 0, bestScore = -1;
  for (let start = 0; start + maxChars <= transcript.length; start += 250) {
    const win = lower.slice(start, start + maxChars);
    let score = 0;
    for (const t of terms) if (win.includes(t)) score++;
    if (score > bestScore) { bestScore = score; bestStart = start; }
  }
  return transcript.slice(bestStart, bestStart + maxChars);
}

const distractorIds = (await restGet("yt_videos?select=video_id&transcript=not.is.null&order=video_id.asc&limit=" + N_DISTRACTORS + "&offset=200"))
  .map((r: any) => r.video_id).filter((id: string) => !goldenVideos.has(id));
const ids = [...new Set([...goldenVideos, ...distractorIds])];
const rows = await restGet("yt_videos?select=video_id,channel,title,upload_date,transcript&video_id=in.(" + ids.join(",") + ")");
const videos: Video[] = rows.filter((v: any) => v.transcript && v.transcript.length > 0);
console.error("[chunkexp] " + videos.length + " Videos (" + goldenVideos.size + " golden + " + distractorIds.length + " Distraktoren)");

const results: any[] = [];
for (const size of SIZES) {
  // Pool fuer diese Groesse bauen (gleicher Textabschnitt pro Video)
  const pool: Doc[] = [];
  let totalChunks = 0;
  for (const v of videos) {
    const isGolden = goldenVideos.has(v.video_id);
    const slice = isGolden
      ? bestWindow(v.transcript, termsByVideo.get(v.video_id) || new Set<string>(), MAX_CHARS)
      : v.transcript.slice(0, MAX_CHARS);
    const chunks = chunkTranscript(slice, size, OVERLAP);
    totalChunks += chunks.length; // gleiche Basis wie totalChars (gekappter Abschnitt) -> korrekte Dichte
    const header = headerOf(v);
    const take = chunks.slice(0, 12);
    take.forEach((c, i) => pool.push({ id: v.video_id + "#" + i, text: header + "\n\n" + c, video_id: v.video_id }));
  }
  const totalChars = videos.reduce((a, v) => a + Math.min(v.transcript.length, MAX_CHARS), 0);
  console.error("[chunkexp] size=" + size + " pool=" + pool.length + " chunks/Video~" + (totalChunks / videos.length).toFixed(1));

  for (const model of MODELS) {
    const pf = prefixFor(model);
    try {
      const pe = await cachedEmbed(model, "pool_" + size + "_" + OVERLAP, pool.map((d) => pf.d + d.text));
      const qv = await cachedEmbed(model, "queries_" + size, queries.map((q) => pf.q + q.query));
      const vecs = new Map<string, number[]>(); pool.forEach((d, i) => vecs.set(d.id, pe.vectors[i]));
      let s1 = 0, s5 = 0, sr = 0, sn = 0, sm = 0;
      for (let i = 0; i < queries.length; i++) {
        const m = metrics(pool, vecs, qv.vectors[i], new Set(queries[i].relevant));
        s1 += m.recall1; s5 += m.recall5; sr += m.recall; sn += m.ndcg; sm += m.mrr;
      }
      const n = queries.length, dim = pe.vectors[0]?.length ?? 0;
      const estMb = (totalChunks / Math.max(1, totalChars)) * 135043467 * dim * 4 / 1e6;
      const row = { size, overlap: OVERLAP, model, dimension: dim, pool_chunks: pool.length, chunks_per_video: +(totalChunks / videos.length).toFixed(1), recall_at_1: +(s1 / n).toFixed(4), recall_at_5: +(s5 / n).toFixed(4), recall_at_k: +(sr / n).toFixed(4), ndcg_at_k: +(sn / n).toFixed(4), mrr: +(sm / n).toFixed(4), embed_seconds: +pe.seconds.toFixed(1), docs_per_s: +(pool.length / Math.max(0.1, pe.seconds)).toFixed(3), est_vec_mb_full: Math.round(estMb) };
      results.push(row);
      console.error("[chunkexp] size=" + size + " " + model + " -> R@1 " + row.recall_at_1 + " R@5 " + row.recall_at_5 + " R@10 " + row.recall_at_k + " nDCG " + row.ndcg_at_k + " MRR " + row.mrr + " (" + row.docs_per_s + " docs/s)");
    } catch (e: any) {
      results.push({ size, overlap: OVERLAP, model, error: String(e.message).slice(0, 160) });
      console.error("[chunkexp] size=" + size + " " + model + " SKIPPED: " + e.message);
    }
  }
}
await Deno.mkdir(CACHE, { recursive: true });
await Deno.writeTextFile(OUT, JSON.stringify({ generated_at: new Date().toISOString(), videos: videos.length, max_chars_per_video: MAX_CHARS, k: K, results }, null, 2));

console.log("\n=== Chunk-Size-Experiment (K=" + K + ", Overlap=" + OVERLAP + ", " + videos.length + " Videos) ===");
console.log("Size  Modell".padEnd(26) + "Dim   R@1    R@5   R@10   nDCG    MRR  chk/Vid  docs/s est.VecMB");
for (const r of results) {
  if (r.error) { console.log(String(r.size).padEnd(6) + r.model.padEnd(20) + "SKIPPED: " + r.error); continue; }
  console.log(String(r.size).padEnd(6) + r.model.padEnd(20) + String(r.dimension).padStart(5) + "  " + String(r.recall_at_1).padStart(5) + "  " + String(r.recall_at_5).padStart(5) + "  " + String(r.recall_at_k).padStart(5) + "  " + String(r.ndcg_at_k).padStart(5) + "  " + String(r.mrr).padStart(5) + "  " + String(r.chunks_per_video).padStart(7) + "  " + String(r.docs_per_s).padStart(7) + "  " + String(r.est_vec_mb_full).padStart(8));
}
console.log("\nJSON: " + OUT);
