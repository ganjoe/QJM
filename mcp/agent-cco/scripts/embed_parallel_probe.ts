// Kontrollierte Embedding-Probe: fester Input-Satz, konfigurierbare Batch-/Concurrency,
// CPU-Messung (host-weit via /proc/stat) pro Lauf, Wiederholungen.
function arg(name: string, def = ""): string { const p = "--" + name + "="; const h = Deno.args.find((a) => a.startsWith(p)); return h ? h.slice(p.length) : def; }
const URL = arg("url", "http://127.0.0.1:11434");
const MODEL = arg("model", "mxbai-embed-large");
const LABEL = arg("label", "");
const REQUESTS = parseInt(arg("requests", "48"));
const CONC = parseInt(arg("concurrency", "1"));
const BATCH = parseInt(arg("batch", "1"));
const CHARS = parseInt(arg("chars", "800"));
const REPS = parseInt(arg("repeats", "2"));

const base = "The quick brown fox jumps over the lazy dog. Earnings guidance and macro liquidity drive risk assets while the Fed signals policy. ";
const texts = Array.from({ length: REQUESTS }, (_, i) => (base + "[" + i + "] ").repeat(20).slice(0, CHARS));
const groups: string[][] = [];
for (let i = 0; i < texts.length; i += BATCH) groups.push(texts.slice(i, i + BATCH));

async function cpuSnap() {
  const txt = await Deno.readTextFile("/proc/stat");
  const line = txt.split("\n")[0].split(/\s+/);
  const nums = line.slice(1).map(Number);
  return { total: nums.reduce((a, b) => a + b, 0), idle: (nums[3] || 0) + (nums[4] || 0) };
}
function cpuPct(a: any, b: any) { const t = b.total - a.total; return t > 0 ? ((b.total - b.idle) - (a.total - a.idle)) / t * 100 : 0; }

async function oneRun(): Promise<number> {
  let gi = 0;
  const t0 = Date.now();
  async function worker() {
    while (gi < groups.length) {
      const g = groups[gi++];
      const r = await fetch(URL + "/api/embed", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: MODEL, input: g, keep_alive: -1 }) });
      if (!r.ok) throw new Error("HTTP " + r.status + " " + (await r.text()).slice(0, 100));
      await r.arrayBuffer();
    }
  }
  await Promise.all(Array.from({ length: Math.max(1, CONC) }, () => worker()));
  return (Date.now() - t0) / 1000;
}

for (let rep = 0; rep < REPS; rep++) {
  const before = await cpuSnap();
  let prev = before;
  const samples: number[] = [];
  const timer = setInterval(async () => { try { const cur = await cpuSnap(); samples.push(cpuPct(prev, cur)); prev = cur; } catch (_e) { /* ignore */ } }, 200);
  const sec = await oneRun();
  clearInterval(timer);
  samples.push(cpuPct(prev, await cpuSnap()));
  const avg = samples.reduce((a, b) => a + b, 0) / samples.length;
  const peak = Math.max(...samples);
  console.log(LABEL + " model=" + MODEL + " inputs=" + REQUESTS + " batch=" + BATCH + " conc=" + CONC + " rep=" + (rep + 1) + " -> " + sec.toFixed(2) + "s  " + (REQUESTS / sec).toFixed(2) + " docs/s  CPU avg/peak " + avg.toFixed(1) + "/" + peak.toFixed(1));
}
