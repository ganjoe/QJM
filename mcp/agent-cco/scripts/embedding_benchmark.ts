// Embedding-Model-Benchmark — schnell, read-only.
// Kernidee: die Referenz (aktuelles Produktionsmodell, mxbai-embed-large) ist BEREITS in der DB
// und wird wiederverwendet. Nur die Kandidatenmodelle werden neu eingebettet. Daher Laufzeit
// in Minuten statt Stunden.
//
// Aufruf (isoliert, vom Host):
//   bash mcp/agent-cco/scripts/run_embedding_benchmark.sh \
//     --models=mxbai-embed-large --sample=60 --max-per-video=2
// (qwen3-embedding:* wurde mit der MXBAI-Migration 2026-09-18 aus Ollama entfernt;
//  als historische 8B-Referenz nur noch ueber die Zahlen in docs/embedding-benchmark.md
//  und backups/mxbai-migration-20260918/ verfuegbar.)
//
// Nur YT-Chunks (dort ist gespeicherter content == eingebetteter Text -> fairer Vergleich).
// Bewertung auf Video-Ebene (Unit = video_id), Golden-Set = scripts/embedding_golden.json.

// Letztes passendes Argument gewinnt: das Wrapper-Skript setzt Defaults VOR "$@", damit
// explizite Aufrufparameter (z.B. --cache=..., --out=...) die Defaults ueberschreiben.
function arg(name: string, def = ""): string {
  const p = "--" + name + "=";
  const hits = Deno.args.filter((a) => a.startsWith(p));
  return hits.length > 0 ? hits[hits.length - 1].slice(p.length) : def;
}

const SUPABASE_URL = (Deno.env.get("SUPABASE_URL") || "http://127.0.0.1:8001").replace(/\/+$/, "");
const SUPABASE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
const OLLAMA_URL = (arg("ollama", Deno.env.get("OLLAMA_URL") || "http://127.0.0.1:11434")).replace(/\/+$/, "");
const MODELS = arg("models", "mxbai-embed-large").split(",").map((s) => s.trim()).filter(Boolean);
const GOLDEN = arg("golden", "/app/scripts/embedding_golden.json");
const SAMPLE = parseInt(arg("sample", "40"));            // Distraktor-Chunks
const MAX_PER_VIDEO = parseInt(arg("max-per-video", "2")); // relevante Chunks pro Golden-Video
const TRUNCATE = parseInt(arg("truncate", "0"));          // >0 kuerzt Texte (dann Referenz neu einbetten)
const REFERENCE_DB = arg("reference-db", "1") !== "0";
// Aktuelle Referenz ist das PRODUKTIONSMODELL in der DB. Seit 2026-09-18: mxbai-embed-large.
// Historische 8B-Referenz (vor der Migration): --reference-model=qwen3-embedding:8b
const REFERENCE_MODEL = arg("reference-model", "mxbai-embed-large");
const CACHE = arg("cache", "/tmp/embbench");
const OUT = arg("out", CACHE + "/result.json");
// Batch 2 = Produktions-Setting des Gateways (EMBED_BATCH_SIZE). Die Kontextgrenze gilt
// pro Input, ein groesserer Batch bringt auf der CPU ohnehin kaum Durchsatz.
const BATCH = parseInt(arg("batch", "2"));
const SEED = parseInt(arg("seed", "12345"));
const K = parseInt(arg("k", "10"));
const USE_DB_REF = REFERENCE_DB && TRUNCATE === 0;

interface Doc { id: string; content: string; artifact_type: string; video_id: string | null; embedding?: string; }

const hdrs: Record<string, string> = { "Content-Type": "application/json" };
if (SUPABASE_KEY) { hdrs["apikey"] = SUPABASE_KEY; hdrs["Authorization"] = "Bearer " + SUPABASE_KEY; }

async function restGet(path: string): Promise<any[]> {
  const r = await fetch(SUPABASE_URL + "/rest/v1/" + path, { headers: hdrs });
  if (!r.ok) throw new Error("PostgREST " + r.status + ": " + (await r.text()).slice(0, 200));
  return await r.json();
}
async function restCount(path: string): Promise<number> {
  const r = await fetch(SUPABASE_URL + "/rest/v1/" + path + "&limit=1", { headers: { ...hdrs, Prefer: "count=exact", Range: "0-0" } });
  return parseInt((r.headers.get("content-range") || "0-0/0").split("/")[1] || "0", 10);
}
function textOf(d: Doc): string { return TRUNCATE > 0 ? d.content.slice(0, TRUNCATE) : d.content; }
// Modellspezifische Query-/Dokument-Prefixe. Ohne sie sind englische Encoder unfair benachteiligt.
const PREFIX_MAP: Record<string, { q: string; d: string }> = {
  "mxbai-embed-large": { q: "Represent this sentence for searching relevant passages: ", d: "" },
  "bge-large": { q: "Represent this sentence for searching relevant passages: ", d: "" },
  "nomic-embed-text": { q: "search_query: ", d: "search_document: " },
  "all-minilm": { q: "", d: "" },
};
function prefixFor(model: string): { q: string; d: string } { return PREFIX_MAP[model] || { q: "", d: "" }; }
function cosine(a: number[], b: number[]): number {
  let dot = 0, na = 0, nb = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
  const den = Math.sqrt(na) * Math.sqrt(nb);
  return den ? dot / den : 0;
}
// Echte CPU-Auslastung aus /proc/stat (host-weit, alle Threads).
async function cpuSnapshot(): Promise<{ agg: { total: number; idle: number }; cores: { total: number; idle: number }[] }> {
  const txt = await Deno.readTextFile("/proc/stat");
  const lines = txt.split("\n").filter((l) => l.startsWith("cpu"));
  const parse = (parts: string[]) => {
    const nums = parts.slice(1).map(Number);
    return { total: nums.reduce((a, b) => a + b, 0), idle: (nums[3] || 0) + (nums[4] || 0) };
  };
  return { agg: parse(lines[0].split(/\s+/)), cores: lines.slice(1).map((l) => parse(l.split(/\s+/))) };
}
function cpuDeltaPct(before: any, after: any): { avgPct: number; corePcts: number[] } {
  const totalDelta = after.agg.total - before.agg.total;
  const busyDelta = (after.agg.total - after.agg.idle) - (before.agg.total - before.agg.idle);
  const avgPct = totalDelta > 0 ? (busyDelta / totalDelta) * 100 : 0;
  const corePcts = after.cores.map((c: any, i: number) => {
    const b0 = before.cores[i] || { total: 0, idle: 0 };
    const t = c.total - b0.total;
    const b = (c.total - c.idle) - (b0.total - b0.idle);
    return t > 0 ? (b / t) * 100 : 0;
  });
  return { avgPct, corePcts };
}
// Periodische Messung waehrend fn(): Durchschnitt + Peak (Peak = momentane Auslastung).
async function sampleCpuDuring<T>(fn: () => Promise<T>): Promise<{ result: T; avgPct: number; peakPct: number; peakCoresBusy: number }> {
  let prev = await cpuSnapshot();
  const avgs: number[] = [];
  const corePeaks: number[] = new Array(prev.cores.length).fill(0);
  const timer = setInterval(async () => {
    try {
      const cur = await cpuSnapshot();
      const d = cpuDeltaPct(prev, cur);
      avgs.push(d.avgPct);
      d.corePcts.forEach((p, i) => { if (p > corePeaks[i]) corePeaks[i] = p; });
      prev = cur;
    } catch (_e) { /* ignorieren */ }
  }, 200);
  const result = await fn();
  clearInterval(timer);
  const last = cpuDeltaPct(prev, await cpuSnapshot());
  avgs.push(last.avgPct);
  last.corePcts.forEach((p, i) => { if (p > corePeaks[i]) corePeaks[i] = p; });
  const avgPct = avgs.length ? avgs.reduce((a, b) => a + b, 0) / avgs.length : 0;
  const peakPct = avgs.length ? Math.max(...avgs) : 0;
  const peakCoresBusy = corePeaks.filter((p) => p > 30).length;
  return { result, avgPct, peakPct, peakCoresBusy };
}
// Ollama 0.21.2 + mxbai: Ein Input mit > 512 Tokens kann HTTP 400 liefern
// ("the input length exceeds the context length") statt still zu trunkieren. Die Grenze gilt
// PRO INPUT, nicht pro Batch. Deshalb: Batch versuchen, bei Fehler jeden Text einzeln.
async function embedOnce(model: string, slice: string[]): Promise<number[][]> {
  const r = await fetch(OLLAMA_URL + "/api/embed", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model, input: slice, keep_alive: -1 }) });
  if (!r.ok) throw new Error("Ollama " + r.status + " (" + model + "): " + (await r.text()).slice(0, 200));
  const d = await r.json();
  const vecs = d?.embeddings;
  if (!Array.isArray(vecs) || vecs.length !== slice.length) throw new Error("Vektor-Anzahl " + (vecs?.length ?? 0) + " != " + slice.length + " (" + model + ")");
  return vecs;
}
async function embed(model: string, texts: string[]): Promise<{ vectors: (number[] | null)[]; seconds: number; failed: number }> {
  const out: (number[] | null)[] = [];
  let failed = 0;
  const t0 = Date.now();
  for (let i = 0; i < texts.length; i += BATCH) {
    const slice = texts.slice(i, i + BATCH);
    try {
      out.push(...await embedOnce(model, slice));
    } catch (e: any) {
      if (slice.length === 1) {
        console.error("  [" + model + "] Input uebersprungen (" + e.message.slice(0, 80) + ")");
        out.push(null); failed++;
      } else {
        for (const t of slice) {
          try { out.push((await embedOnce(model, [t]))[0]); }
          catch (e2: any) { console.error("  [" + model + "] Input uebersprungen (" + e2.message.slice(0, 80) + ")"); out.push(null); failed++; }
        }
      }
    }
    const done = Math.min(texts.length, i + BATCH);
    if (done % (BATCH * 5) === 0 || done === texts.length) console.error("  [" + model + "] " + done + "/" + texts.length);
  }
  return { vectors: out, seconds: (Date.now() - t0) / 1000, failed };
}
async function cachedEmbed(model: string, key: string, texts: string[]): Promise<{ vectors: (number[] | null)[]; seconds: number; cached: number; failed: number }> {
  const dir = CACHE + "/" + model.replace(/[^a-zA-Z0-9._-]/g, "_");
  await Deno.mkdir(dir, { recursive: true });
  const file = dir + "/" + key + ".json";
  let store: Record<string, number[]> = {};
  try { store = JSON.parse(await Deno.readTextFile(file)); } catch (_e) { /* leer */ }
  const missing: string[] = [];
  texts.forEach((t) => { if (!store[t]) missing.push(t); });
  let seconds = 0, failed = 0;
  if (missing.length > 0) {
    const res = await embed(model, missing);
    seconds = res.seconds; failed = res.failed;
    missing.forEach((t, i) => { const v = res.vectors[i]; if (v) store[t] = v; });
    await Deno.writeTextFile(file, JSON.stringify(store));
  }
  return { vectors: texts.map((t) => store[t] ?? null), seconds, cached: texts.length - missing.length, failed };
}
function metricsFor(qv: number[], pool: Doc[], poolVecs: Map<string, number[]>, relevant: Set<string>) {
  const best = new Map<string, number>();
  for (const d of pool) {
    const v = poolVecs.get(d.id); if (!v) continue;
    const u = d.video_id || d.id;
    const s = cosine(qv, v); const prev = best.get(u);
    if (prev === undefined || s > prev) best.set(u, s);
  }
  const ranked = [...best.entries()].sort((a, b) => b[1] - a[1]).map((e) => e[0]);
  const rank = ranked.findIndex((u) => relevant.has(u));
  const topk = ranked.slice(0, K);
  const hits = topk.filter((u) => relevant.has(u)).length;
  let dcg = 0; topk.forEach((u, i) => { if (relevant.has(u)) dcg += 1 / Math.log2(i + 2); });
  let idcg = 0; for (let i = 0; i < Math.min(relevant.size, K); i++) idcg += 1 / Math.log2(i + 2);
  return { recall: relevant.size ? Math.min(hits, relevant.size) / Math.min(K, relevant.size) : 0, ndcg: idcg ? dcg / idcg : 0, mrr: rank >= 0 ? 1 / (rank + 1) : 0 };
}

// ── Pool (nur YT, mit gespeichertem 8B-Vektor) ──────────────────────
const golden = JSON.parse(await Deno.readTextFile(GOLDEN));
const queries: { query: string; relevant: string[] }[] = golden.queries;
const relVideos = new Set<string>();
for (const q of queries) for (const id of q.relevant) if (!/^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}$/.test(id)) relVideos.add(id);

const relDocs: Doc[] = [];
for (const vid of relVideos) {
  const rows = await restGet("agent_workspace?select=id,content,artifact_type,video_id,embedding&artifact_type=eq.yt_chunk&video_id=eq." + encodeURIComponent(vid) + "&embedding=not.is.null&limit=1000");
  relDocs.push(...rows.sort((a: any, b: any) => String(a.id).localeCompare(String(b.id))).slice(0, MAX_PER_VIDEO));
}
const total = await restCount("agent_workspace?select=id&embedding=not.is.null&artifact_type=eq.yt_chunk");
const offset = SEED % Math.max(1, total - SAMPLE); // deterministisch, damit Laeufe vergleichbar sind
const distractors = SAMPLE > 0
  ? await restGet("agent_workspace?select=id,content,artifact_type,video_id,embedding&embedding=not.is.null&artifact_type=eq.yt_chunk&order=id.asc&limit=" + SAMPLE + "&offset=" + offset)
  : [];
const byId = new Map<string, Doc>();
for (const d of [...relDocs, ...distractors]) if (d.content) byId.set(d.id, d);
const pool = [...byId.values()];
const queryTexts = queries.map((q) => q.query);
console.error("[bench] Pool: " + pool.length + " YT-Chunks (relevante: " + relDocs.length + "), Queries: " + queries.length + ", K=" + K + ", DB-Referenz: " + USE_DB_REF);

const results: any[] = [];
for (const model of MODELS) {
  let poolVecs: Map<string, number[]>;
  let qVecs: number[][];
  let dim = 0, poolEmbedded = 0, embSeconds: number | null = null, cpuPct = 0, cpuPeak = 0, coresBusy = 0, usedDb = false;
  const pf = prefixFor(model);

  if (USE_DB_REF && model === REFERENCE_MODEL) {
    usedDb = true;
    poolVecs = new Map(pool.map((d) => [d.id, JSON.parse(d.embedding || "[]")]));
    dim = poolVecs.get(pool[0].id)?.length ?? 0;
    const qv = await cachedEmbed(model, "queries", queryTexts.map((t) => pf.q + t));
    qVecs = qv.vectors.map((v) => v ?? []);
  } else {
    const t0 = Date.now();
    const sampled = await sampleCpuDuring(() => cachedEmbed(model, "pool", pool.map((d) => pf.d + textOf(d))));
    embSeconds = (Date.now() - t0) / 1000;
    const pe = sampled.result;
    poolEmbedded = pool.length - pe.cached;
    const qv = await cachedEmbed(model, "queries", queryTexts.map((t) => pf.q + t));
    poolVecs = new Map(pool.flatMap((d, i) => (pe.vectors[i] ? [[d.id, pe.vectors[i] as number[]]] : [])));
    qVecs = qv.vectors.map((v) => v ?? []); dim = pe.vectors.find((v) => v)?.length ?? 0;
    if (pe.failed > 0) console.error("[bench] WARNUNG: " + pe.failed + " Pool-Texte konnten nicht eingebettet werden (Kontextgrenze).");
    cpuPct = sampled.avgPct; cpuPeak = sampled.peakPct; coresBusy = sampled.peakCoresBusy;
  }

  let sumR = 0, sumN = 0, sumM = 0;
  const perQuery: any[] = [];
  for (let i = 0; i < queries.length; i++) {
    const m = metricsFor(qVecs[i], pool, poolVecs, new Set(queries[i].relevant));
    sumR += m.recall; sumN += m.ndcg; sumM += m.mrr;
    perQuery.push({ query: queries[i].query, recall_at_k: +m.recall.toFixed(4), ndcg_at_k: +m.ndcg.toFixed(4), mrr: +m.mrr.toFixed(4) });
  }
  const n = queries.length;
  const row = { model, dimension: dim, reference_from_db: usedDb, pool_docs: pool.length, pool_embedded: poolEmbedded, embed_seconds: embSeconds === null ? null : +embSeconds.toFixed(1), docs_per_s: embSeconds && poolEmbedded > 0 ? +(poolEmbedded / embSeconds).toFixed(3) : null, avg_cpu_pct: +cpuPct.toFixed(1), peak_cpu_pct: +cpuPeak.toFixed(1), cores_busy: coresBusy, recall_at_k: +(sumR / n).toFixed(4), ndcg_at_k: +(sumN / n).toFixed(4), mrr: +(sumM / n).toFixed(4), per_query: perQuery };
  results.push(row);
  console.error("[bench] " + model + (usedDb ? " (DB)" : "") + " -> Recall@" + K + " " + row.recall_at_k + " | nDCG@" + K + " " + row.ndcg_at_k + " | MRR " + row.mrr + " | " + dim + " Dim" + (embSeconds !== null ? " | " + row.docs_per_s + " docs/s | CPU " + row.avg_cpu_pct + "% auf " + coresBusy + "/32 Threads" : ""));
}

await Deno.mkdir(CACHE, { recursive: true });
await Deno.writeTextFile(OUT, JSON.stringify({ generated_at: new Date().toISOString(), pool_size: pool.length, queries: queries.length, k: K, results }, null, 2));
console.log("\n=== Embedding-Benchmark (K=" + K + ", Pool=" + pool.length + " YT-Chunks) ===");
console.log("Modell".padEnd(24) + "Dim".padStart(6) + "  Recall@" + K + "   nDCG@" + K + "      MRR  docs/s  CPU avg/peak  Thr");
for (const r of results) {
  console.log(r.model.padEnd(24) + String(r.dimension).padStart(6) + "  " + String(r.recall_at_k).padStart(8) + "  " + String(r.ndcg_at_k).padStart(8) + "  " + String(r.mrr).padStart(8) + "  " + String(r.docs_per_s ?? "-").padStart(6) + "  " + String((r.avg_cpu_pct ?? "-") + "/" + (r.peak_cpu_pct ?? "-")).padStart(11) + "  " + String(r.cores_busy ?? "-").padStart(3) + (r.reference_from_db ? "  [DB]" : ""));
}
console.log("\nJSON: " + OUT);
