/**
 * Standalone X Multi-Level Alpha-Influencer Finder
 *
 * Findet hochqualitative Signal-Accounts ("Alpha-Influencer") über mehrere Ebenen
 * ausgehend von deinen in Supabase registrierten Influencern.
 *
 * Strikte Persistenz-Garantie:
 * - Vor jedem Schreibvorgang wird ein automatisches .bak Backup erstellt.
 * - Bereits abgefragte Accounts werden in 'queriedAccounts' geführt und NIEMALS doppelt abgefragt.
 * - Bei geänderten Parametern (z.B. Erhöhung von --expand-top oder --depth) werden vorhandene
 *   Daten stets inkrementell ergänzt und niemals überschrieben.
 */
import { createClient, SupabaseClient } from "npm:@supabase/supabase-js@2.47.10";

// --- Hilfsfunktionen für .env Auto-Discovery ---
function loadEnvFile(path: string): Record<string, string> {
  try {
    const content = Deno.readTextFileSync(path);
    const env: Record<string, string> = {};
    for (const line of content.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const idx = trimmed.indexOf("=");
      if (idx > 0) {
        const key = trimmed.slice(0, idx).trim();
        let val = trimmed.slice(idx + 1).trim();
        if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
          val = val.slice(1, -1);
        }
        env[key] = val;
      }
    }
    return env;
  } catch {
    return {};
  }
}

interface AppEnv {
  twitterApiIoKey: string;
  xBearerToken: string;
  supabaseUrl: string;
  supabaseKey: string;
  supabase: SupabaseClient;
  source: string;
}

function resolveAppEnv(): AppEnv {
  const scriptDir = new URL(".", import.meta.url).pathname;
  const candidates = [
    `${scriptDir}../../../llm-gateway/.env`,
    `${scriptDir}../../llm-gateway/.env`,
    "/home/daniel/QJM/llm-gateway/.env",
    `${scriptDir}../.env`,
    `${scriptDir}.env`,
    "./.env",
  ];

  let twitterApiIoKey = Deno.env.get("TWITTER_API_IO_KEY") || Deno.env.get("TWITTERAPI_IO_KEY") || "";
  let xBearerToken = Deno.env.get("X_BEARER_TOKEN") || "";
  let supabaseUrl = Deno.env.get("SUPABASE_URL") || "";
  let supabaseKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
  let source = "Deno.env";

  for (const p of candidates) {
    const env = loadEnvFile(p);
    if (!twitterApiIoKey && (env.TWITTER_API_IO_KEY || env.TWITTERAPI_IO_KEY)) {
      twitterApiIoKey = env.TWITTER_API_IO_KEY || env.TWITTERAPI_IO_KEY;
    }
    if (!xBearerToken && env.X_BEARER_TOKEN) xBearerToken = env.X_BEARER_TOKEN;
    if (!supabaseUrl && env.SUPABASE_URL) supabaseUrl = env.SUPABASE_URL;
    if (!supabaseKey && env.SUPABASE_SERVICE_ROLE_KEY) supabaseKey = env.SUPABASE_SERVICE_ROLE_KEY;
    if (env.TWITTER_API_IO_KEY || env.X_BEARER_TOKEN || env.SUPABASE_SERVICE_ROLE_KEY) source = p;
  }

  if (!supabaseUrl || supabaseUrl.includes("host.docker.internal")) {
    supabaseUrl = "http://10.20.0.23:8001";
  }

  const supabase: SupabaseClient = createClient(supabaseUrl, supabaseKey);

  return { twitterApiIoKey, xBearerToken, supabaseUrl, supabaseKey, supabase, source };
}

// --- Supabase Client Helper ---
interface RegisteredUser {
  username: string;
  screen_name: string;
  x_id: string;
}

async function fetchRegisteredInfluencers(supabase: SupabaseClient): Promise<RegisteredUser[]> {
  const { data, error } = await supabase
    .from("x_users")
    .select("username, screen_name, x_id")
    .eq("is_active", true);

  if (error) {
    throw new Error(`Fehler beim Abrufen der Influencer aus Supabase: ${error.message}`);
  }

  const list: RegisteredUser[] = (data || []) as RegisteredUser[];
  return list.filter((u) => u.username && u.x_id);
}

// --- TwitterAPI.io Fetcher ---
async function fetchFollowingsViaTwitterApiIo(
  userId: string,
  username: string,
  apiKey: string,
  delayMs: number,
  maxResults: number = 0, // 0 = VOLLSTÄNDIG / UNBEGRENZT
): Promise<{ followings: any[]; costUsd: number }> {
  const allFollowings: any[] = [];
  let cursor = "";
  let hasNext = true;
  const pageSize = 200; // Maximaler Batch-Wert von TwitterAPI.io

  while (hasNext) {
    if (delayMs > 0 && allFollowings.length > 0) {
      await new Promise((r) => setTimeout(r, delayMs));
    }

    let url = `https://api.twitterapi.io/twitter/user/followings?pageSize=${pageSize}`;
    if (username) {
      url += `&userName=${username.replace("@", "").trim()}`;
    } else if (userId) {
      url += `&userId=${userId}`;
    }
    if (cursor) {
      url += `&cursor=${encodeURIComponent(cursor)}`;
    }

    const res = await fetch(url, {
      headers: {
        "x-api-key": apiKey,
        "User-Agent": "XAlphaInfluencerFinder/1.0",
      },
    });

    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`TwitterAPI.io HTTP ${res.status}: ${errText}`);
    }

    const data = await res.json();
    const rawList = data.followings || data.users || data.data || [];
    for (const u of rawList) {
      const uId = u.id || u.userId || u.rest_id;
      const uHandle = u.userName || u.username || u.screen_name;
      const uName = u.name || uHandle;
      if (uId && uHandle) {
        allFollowings.push({
          id: String(uId),
          username: uHandle,
          name: uName,
        });
      }
    }

    const hasNextFlag = Boolean(data.has_next_page && data.next_cursor && data.next_cursor !== cursor);
    if (maxResults > 0 && allFollowings.length >= maxResults) {
      hasNext = false;
    } else {
      hasNext = hasNextFlag;
    }
    cursor = data.next_cursor || "";

    if (allFollowings.length > 0 && allFollowings.length % 600 === 0) {
      console.log(`      ... ${allFollowings.length} Followings geladen`);
    }
  }

  const costUsd = (allFollowings.length / 1000) * 0.01;
  return { followings: allFollowings, costUsd };
}

// --- Offizielle X API v2 (Fallback) ---
let lastRequestTime = 0;
let rateLimitRemaining: number | null = null;
let rateLimitResetEpoch: number | null = null;
let rateLimitLimit: number | null = null;

async function throttledXFetch(url: string, bearerToken: string, delayMs: number): Promise<Response> {
  const now = Date.now();
  const elapsed = now - lastRequestTime;
  if (elapsed < delayMs) {
    await new Promise((r) => setTimeout(r, delayMs - elapsed));
  }

  if (rateLimitRemaining !== null && rateLimitRemaining <= 1 && rateLimitResetEpoch) {
    const sleepMs = Math.max(1000, rateLimitResetEpoch * 1000 - Date.now() + 1000);
    console.warn(`⏳ [Rate Limiter] Kontingent fast erschöpft (${rateLimitRemaining} übrig). Warte ${Math.round(sleepMs / 1000)}s bis Reset...`);
    await new Promise((r) => setTimeout(r, sleepMs));
  }

  lastRequestTime = Date.now();
  const res = await fetch(url, {
    headers: {
      Authorization: `Bearer ${bearerToken}`,
      "User-Agent": "XAlphaInfluencerFinder/1.0",
    },
  });

  const remHeader = res.headers.get("x-rate-limit-remaining");
  const resetHeader = res.headers.get("x-rate-limit-reset");
  const limitHeader = res.headers.get("x-rate-limit-limit");

  if (remHeader !== null) rateLimitRemaining = parseInt(remHeader, 10);
  if (resetHeader !== null) rateLimitResetEpoch = parseInt(resetHeader, 10);
  if (limitHeader !== null) rateLimitLimit = parseInt(limitHeader, 10);

  return res;
}

async function fetchFollowingsViaOfficialX(
  userId: string,
  bearerToken: string,
  delayMs: number,
  maxResults: number = 0,
): Promise<{ followings: any[]; costUsd: number }> {
  const allFollowings: any[] = [];
  let nextToken = "";
  let hasNext = true;

  while (hasNext) {
    let url = `https://api.twitter.com/2/users/${userId}/following?max_results=1000`;
    if (nextToken) {
      url += `&pagination_token=${encodeURIComponent(nextToken)}`;
    }
    const res = await throttledXFetch(url, bearerToken, delayMs);
    if (!res.ok) {
      throw new Error(`X API Followings Fehler (${res.status}): ${await res.text()}`);
    }
    const data = await res.json();
    const list = data.data || [];
    for (const u of list) {
      if (u.id && u.username) {
        allFollowings.push({
          id: String(u.id),
          username: u.username,
          name: u.name || u.username,
        });
      }
    }
    nextToken = data.meta?.next_token || "";
    if (maxResults > 0 && allFollowings.length >= maxResults) {
      hasNext = false;
    } else {
      hasNext = Boolean(nextToken);
    }
  }

  const costUsd = allFollowings.length * 0.01;
  return { followings: allFollowings, costUsd };
}

// --- CLI Hilfe & Argument-Parsing ---
function printHelp() {
  console.log(`
Verwendung:
  ./run_alpha_finder.sh [Optionen]

Optionen:
  --depth <N>                Analysetiefe / Level (Standard: 1 = nur Basis-Influencer; 2 = Top-Alpha Followings)
  --expand-top <N>           Anzahl Top-Accounts aus vorheriger Ebene für die nächste Ebene (Standard: 20)
  --expand-min-score <M>     Mindest-Score in vorheriger Ebene für Expansion (Standard: 2)
  --batch-pause <N>          Pausiert nach jeweils N API-Abfragen für Bestätigung (Standard: 100, 0 = aus)
  --max-results <N>          Max. Followings pro Influencer (Standard: 0 = VOLLSTÄNDIG / UNBEGRENZT)
  --delay-ms <N>             Pause zwischen API-Aufrufen in ms (Standard: 500 bei TwitterAPI, 1500 bei X)
  --top <N>                  Anzahl der am Ende angezeigten Accounts (Standard: 25)
  --seed <handle/id>         Gezielt nur diesen einen Influencer untersuchen (z. B. --seed steipete)
  --rescan, --force          Bereits abgefragte Seeds erneut vollständig abrufen
  --no-prompt                Keine interaktiven Bestätigungen (für Skripte / Daemons)
  --help, -h                 Zeigt diese Hilfe an

Beispiele:
  ./run_alpha_finder.sh                                  (Ebene 1 vollständig ohne Followings-Limit abfragen)
  ./run_alpha_finder.sh --rescan                         (Alle Seeds erneut ohne 500er-Kappung vollständig laden)
  ./run_alpha_finder.sh --seed steipete --rescan         (Nur @steipete vollständig neu laden)
  ./run_alpha_finder.sh --depth 2 --expand-top 50        (Ebene 2 mit den Top 50 Accounts analysieren)
  ./run_alpha_finder.sh --batch-pause 0                  (Durchlaufen lassen ohne Pause-Bestätigung)
`);
}

interface CliConfig {
  maxDepth: number;
  expandTop: number;
  expandMinScore: number;
  batchPause: number;
  maxResults: number; // 0 = unbegrenzt
  delayMs?: number;
  topN: number;
  onlySeed?: string;
  rescan: boolean;
  noPrompt: boolean;
}

function parseCliArgs(args: string[]): CliConfig | null {
  let maxDepth = parseInt(Deno.env.get("MAX_DEPTH") || "1", 10);
  let expandTop = parseInt(Deno.env.get("EXPAND_TOP") || "20", 10);
  let expandMinScore = parseInt(Deno.env.get("EXPAND_MIN_SCORE") || "2", 10);
  let batchPause = parseInt(Deno.env.get("BATCH_PAUSE_SIZE") || "100", 10);
  let maxResults = Deno.env.get("X_MAX_RESULTS") ? parseInt(Deno.env.get("X_MAX_RESULTS")!, 10) : 0; // 0 = unbegrenzt
  let delayMs: number | undefined = Deno.env.get("X_REQUEST_DELAY_MS") ? parseInt(Deno.env.get("X_REQUEST_DELAY_MS")!, 10) : undefined;
  let topN = parseInt(Deno.env.get("TOP_N") || "25", 10);
  let onlySeed: string | undefined = undefined;
  let rescan = false;
  let noPrompt = false;

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--help" || arg === "-h") {
      printHelp();
      return null;
    } else if (arg === "--depth" && i + 1 < args.length) {
      maxDepth = parseInt(args[++i], 10);
    } else if (arg === "--expand-top" && i + 1 < args.length) {
      expandTop = parseInt(args[++i], 10);
    } else if (arg === "--expand-min-score" && i + 1 < args.length) {
      expandMinScore = parseInt(args[++i], 10);
    } else if (arg === "--batch-pause" && i + 1 < args.length) {
      batchPause = parseInt(args[++i], 10);
    } else if (arg === "--max-results" && i + 1 < args.length) {
      maxResults = parseInt(args[++i], 10);
    } else if (arg === "--delay-ms" && i + 1 < args.length) {
      delayMs = parseInt(args[++i], 10);
    } else if (arg === "--top" && i + 1 < args.length) {
      topN = parseInt(args[++i], 10);
    } else if (arg === "--seed" && i + 1 < args.length) {
      onlySeed = args[++i].replace(/^@/, "").trim().toLowerCase();
    } else if (arg === "--rescan" || arg === "--force") {
      rescan = true;
    } else if (arg === "--no-prompt") {
      noPrompt = true;
    }
  }

  return { maxDepth, expandTop, expandMinScore, batchPause, maxResults, delayMs, topN, onlySeed, rescan, noPrompt };
}

// --- Multi-Level State Typen ---
interface AccountScore {
  id: string;
  username: string;
  name: string;
  count: number;
  followedBy?: string[];
}

interface LevelData {
  level: number;
  name: string;
  seedCount: number;
  seedIds: string[];
  processedIds: string[];
  scores: Record<string, AccountScore>;
}

interface StateFile {
  mode: string;
  provider: string;
  updatedAt: string;
  totalApiCalls: number;
  totalProfilesFetched: number;
  estimatedCostUsd: number;
  queriedAccounts: string[];
  levels: Record<string, LevelData>;
  processed: string[];
  scores: Record<string, AccountScore>;
}

// --- Hauptprogramm ---
async function main() {
  console.log("=========================================================================");
  console.log("             🚀 X Multi-Level Alpha-Influencer Finder 🚀                 ");
  console.log("=========================================================================\n");

  const config = parseCliArgs(Deno.args);
  if (!config) return;

  const env = resolveAppEnv();
  const twitterApiKey = env.twitterApiIoKey;

  const activeProvider: "twitterapi" | "x" = twitterApiKey ? "twitterapi" : "x";

  if (activeProvider === "x" && !env.xBearerToken) {
    console.error("❌ Kritischer Fehler: Weder TWITTER_API_IO_KEY noch X_BEARER_TOKEN gefunden!");
    Deno.exit(1);
  }

  const defaultDelayMs = activeProvider === "twitterapi" ? 500 : 1500;
  const effectiveMaxResults = config.maxResults;
  const effectiveDelayMs = config.delayMs ?? defaultDelayMs;

  console.log(`🔑 Konfiguration: ${env.source}`);
  if (activeProvider === "twitterapi") {
    console.log(`✨ Provider: TwitterAPI.io (0,01 $ pro 1.000 Followings)`);
  } else {
    console.log(`⚠️  Provider: Offizielle X API v2 (0,01 $ pro Profil)`);
  }
  const maxResultsStr = effectiveMaxResults > 0 ? `${effectiveMaxResults} Followings` : "Unbegrenzt (alle Followings)";
  console.log(`🎯 Tiefe: ${config.maxDepth} Ebene(n) | Expand-Top: ${config.expandTop} (Min-Score: ${config.expandMinScore}) | Delay: ${effectiveDelayMs}ms | Max-Followings: ${maxResultsStr}`);
  if (config.onlySeed) console.log(`🎯 Gezielter Filter auf Seed: @${config.onlySeed}`);
  if (config.rescan) console.log(`🔄 Rescan-Modus aktiv: Bereits abgefragte Accounts werden erneut vollständig gecrawlt!`);
  console.log("");

  // 1. Basis-Influencer aus Supabase laden (Ebene 0 Seed)
  let baseInfluencers: RegisteredUser[] = [];
  try {
    baseInfluencers = await fetchRegisteredInfluencers(env.supabase);
    console.log(`📊 Basis: ${baseInfluencers.length} registrierte Influencer in Supabase:`);
    for (const inf of baseInfluencers) {
      console.log(`   - @${inf.username} (${inf.screen_name || "N/A"}) [ID: ${inf.x_id}]`);
    }
  } catch (e: any) {
    console.error(`❌ Konnte Basis-Influencer nicht aus Supabase laden: ${e.message}`);
    return;
  }

  const allSeedIdsEver = new Set<string>(baseInfluencers.map((b) => b.x_id));

  // 2. State laden und abwärtskompatibel migrieren
  const scriptDir = new URL(".", import.meta.url).pathname;
  const stateFilePath = `${scriptDir}../alpha_state_supabase_registered.json`;
  const backupFilePath = `${stateFilePath}.bak`;

  let state: StateFile = {
    mode: "supabase_registered",
    provider: activeProvider,
    updatedAt: new Date().toISOString(),
    totalApiCalls: 0,
    totalProfilesFetched: 0,
    estimatedCostUsd: 0,
    queriedAccounts: [],
    levels: {},
    processed: [],
    scores: {},
  };

  try {
    const raw = await Deno.readTextFile(stateFilePath);
    const parsed = JSON.parse(raw);
    state = { ...state, ...parsed };

    // Migration von alter flacher Struktur zur Multi-Level Struktur falls nötig
    if (!state.levels || Object.keys(state.levels).length === 0) {
      state.levels = {
        "1": {
          level: 1,
          name: "Basis-Influencer (Ebene 1)",
          seedCount: baseInfluencers.length,
          seedIds: baseInfluencers.map((b) => b.x_id),
          processedIds: Array.from(parsed.processed || []),
          scores: parsed.scores || {},
        },
      };
    }

    // Globales queriedAccounts-Register füllen (lokale JSON + Supabase)
    const allQueried = new Set<string>(state.queriedAccounts || []);
    if (parsed.processed) {
      for (const p of parsed.processed) allQueried.add(p);
    }
    for (const lvl of Object.values(state.levels)) {
      for (const p of lvl.processedIds || []) allQueried.add(p);
    }

    // Zusätzlicher DB-Abgleich: Alle bereits in Supabase erfassten Seeds laden
    try {
      const { data: dbScanned, error: dbErr } = await env.supabase.from("alpha_scanned_accounts").select("x_id");
      if (!dbErr && dbScanned) {
        for (const row of dbScanned) {
          allQueried.add(row.x_id);
        }
      }
    } catch {
      // Offline fallback
    }

    state.queriedAccounts = Array.from(allQueried);

    console.log(`\n📂 Gespeicherten Zustand geladen:`);
    console.log(`   - Bereits global abgefragte Accounts: ${state.queriedAccounts.length}`);
    for (const [lvlNum, lvlData] of Object.entries(state.levels)) {
      console.log(`   - Ebene ${lvlNum}: ${lvlData.processedIds.length}/${lvlData.seedCount || lvlData.seedIds?.length || "?"} verarbeitet | ${Object.keys(lvlData.scores).length} Kandidaten`);
    }
  } catch {
    console.log(`\n🆕 Neuer Analyse-Lauf.`);
    const allQueried = new Set<string>();
    try {
      const { data: dbScanned } = await env.supabase.from("alpha_scanned_accounts").select("x_id");
      if (dbScanned) {
        for (const row of dbScanned) allQueried.add(row.x_id);
      }
    } catch {
      // Offline fallback
    }
    state.queriedAccounts = Array.from(allQueried);
    state.levels["1"] = {
      level: 1,
      name: "Basis-Influencer (Ebene 1)",
      seedCount: baseInfluencers.length,
      seedIds: baseInfluencers.map((b) => b.x_id),
      processedIds: [],
      scores: {},
    };
  }

  // Automatisches Backup & Speichern
  const saveState = async () => {
    state.updatedAt = new Date().toISOString();
    state.provider = activeProvider;
    // Abwärtskompatibilität für bestehende Tools: Level 1 immer auf oberster Ebene spiegeln
    if (state.levels["1"]) {
      state.processed = state.levels["1"].processedIds;
      state.scores = state.levels["1"].scores;
    }
    try {
      // Sicherheits-Backup der Vorgänger-Version
      try {
        await Deno.copyFile(stateFilePath, backupFilePath);
      } catch {
        // Erste Datei
      }
      await Deno.writeTextFile(stateFilePath, JSON.stringify(state, null, 2));

      // Browser JS-Export für CORS-freies direktes Öffnen der Datenbank:
      try {
        const jsData = `window.ALPHA_STATE = ${JSON.stringify(state)};\n`;
        await Deno.writeTextFile(stateFilePath.replace(/\.json$/, "_data.js"), jsData);
        await Deno.writeTextFile(`${scriptDir}../../alpha_state_data.js`, jsData);
      } catch {
        // Ignorieren falls Pfad abweicht
      }
    } catch (err: any) {
      console.error(`⚠️  Fehler beim Speichern: ${err.message}`);
    }
  };

  // Signal-Handler für Strg+C
  try {
    Deno.addSignalListener("SIGINT", async () => {
      console.log("\n\n🛑 [Abbruch durch Benutzer via Strg+C]");
      console.log("💾 Sichere Zwischenergebnisse...");
      await saveState();
      console.log(`✅ Stand gesichert in: ${stateFilePath}`);
      printSummary(state, config.topN);
      Deno.exit(0);
    });
  } catch {
    // ignore
  }

  const queriedGlobalSet = new Set<string>(state.queriedAccounts);
  let batchCallCounter = 0;

  // --- Multi-Level Traversierung ---
  for (let currentLevel = 1; currentLevel <= config.maxDepth; currentLevel++) {
    const levelKey = String(currentLevel);
    console.log(`\n=========================================================================`);
    console.log(`                    🔍 ANALYSE EBENE ${currentLevel} von ${config.maxDepth}                      `);
    console.log(`=========================================================================`);

    if (!state.levels[levelKey]) {
      state.levels[levelKey] = {
        level: currentLevel,
        name: currentLevel === 1 ? "Basis-Influencer (Ebene 1)" : `Top-Alpha Followings (Ebene ${currentLevel})`,
        seedCount: 0,
        seedIds: [],
        processedIds: [],
        scores: {},
      };
    }

    const lvl = state.levels[levelKey];
    const lvlProcessedSet = new Set<string>(lvl.processedIds);

    // Seeds für diese Ebene bestimmen
    interface TargetSeed {
      id: string;
      username: string;
      name?: string;
    }
    let currentSeeds: TargetSeed[] = [];

    if (currentLevel === 1) {
      currentSeeds = baseInfluencers.map((b) => ({ id: b.x_id, username: b.username, name: b.screen_name }));
      lvl.seedCount = currentSeeds.length;
      lvl.seedIds = currentSeeds.map((s) => s.id);
    } else {
      // Ebene 2+: Seeds kommen aus den stärksten Alpha-Kandidaten der VORHERIGEN Ebene
      const prevLevelKey = String(currentLevel - 1);
      const prevLevel = state.levels[prevLevelKey];
      if (!prevLevel || Object.keys(prevLevel.scores).length === 0) {
        console.log(`⚠️  Ebene ${currentLevel - 1} hat noch keine Ergebnisse. Überspringe Ebene ${currentLevel}.`);
        break;
      }

      // Filter: Mindest-Score & Sortierung
      const candidates = Object.values(prevLevel.scores)
        .filter((c) => c.count >= config.expandMinScore)
        .sort((a, b) => b.count - a.count);

      // Top N auswählen
      const topSelected = candidates.slice(0, config.expandTop);

      currentSeeds = topSelected
        .filter((c) => !allSeedIdsEver.has(c.id)) // Keine bereits in Ebene 0 vorhandenen Influencer
        .map((c) => ({ id: c.id, username: c.username, name: c.name }));

      lvl.seedCount = currentSeeds.length;
      lvl.seedIds = currentSeeds.map((s) => s.id);

      // Seeds zu allSeedIdsEver hinzufügen, damit Rückbezüge gefiltert werden
      for (const s of currentSeeds) allSeedIdsEver.add(s.id);

      console.log(`🌱 Aus Ebene ${currentLevel - 1} wurden die Top ${currentSeeds.length} Alpha-Accounts (Score >= ${config.expandMinScore}) als Basis gewählt:`);
      const preview = currentSeeds.slice(0, 10).map((s) => `@${s.username}`).join(", ");
      console.log(`   ${preview}${currentSeeds.length > 10 ? ", ..." : ""}`);
    }

    if (config.onlySeed) {
      const filtered = currentSeeds.filter(
        (s) => s.username.toLowerCase() === config.onlySeed || s.id === config.onlySeed
      );
      if (filtered.length > 0) {
        currentSeeds = filtered;
      } else {
        console.log(`⏩ Seed '@${config.onlySeed}' ist in Ebene ${currentLevel} nicht enthalten.`);
        continue;
      }
    }

    const lvlScoresMap = new Map<string, AccountScore>(Object.entries(lvl.scores));

    // Seeds abarbeiten
    let levelRemainingCount = 0;
    for (const seed of currentSeeds) {
      const isAlreadyDone = !config.rescan && (lvlProcessedSet.has(seed.id) || queriedGlobalSet.has(seed.id));
      if (!isAlreadyDone) {
        levelRemainingCount++;
      }
    }

    if (levelRemainingCount === 0) {
      console.log(`✅ Ebene ${currentLevel} ist bereits vollständig ausgewertet (${lvl.processedIds.length}/${currentSeeds.length} Seeds fertig).`);
      continue;
    }

    console.log(`🚀 ${levelRemainingCount} von ${currentSeeds.length} Seeds auf Ebene ${currentLevel} noch ausstehend.\n`);

    for (let i = 0; i < currentSeeds.length; i++) {
      const seed = currentSeeds[i];
      const indexStr = `[L${currentLevel}: ${i + 1}/${currentSeeds.length}]`;

      // STRIKTE PERSISTENZ-PRÜFUNG: Niemals doppelt abfragen (außer bei explizitem --rescan)
      if (!config.rescan && (lvlProcessedSet.has(seed.id) || queriedGlobalSet.has(seed.id))) {
        console.log(`${indexStr} ⏩ Überspringe @${seed.username} (bereits im State abgefragt).`);
        if (!lvlProcessedSet.has(seed.id)) {
          lvlProcessedSet.add(seed.id);
          lvl.processedIds.push(seed.id);
        }
        continue;
      }

      // Interaktive Pause alle N Abfragen (nur wenn batchPause > 0)
      if (config.batchPause > 0 && batchCallCounter >= config.batchPause) {
        console.log(`\n-------------------------------------------------------------`);
        console.log(`⏸️  PAUSE: ${config.batchPause} Abfragen in diesem Block erreicht!`);
        console.log(`   Fortschritt Ebene ${currentLevel}: ${lvlProcessedSet.size}/${currentSeeds.length} Seeds.`);
        console.log(`   Gesamt-Alpha-Pool Ebene ${currentLevel}: ${lvlScoresMap.size} Accounts.`);
        console.log(`   Bisherige Kosten: ~$${(state.estimatedCostUsd || 0).toFixed(4)} USD`);
        console.log(`-------------------------------------------------------------`);

        let shouldContinue = true;
        if (!config.noPrompt && Deno.stdin.isTerminal()) {
          const answer = prompt(`👉 Möchtest du fortfahren? (j/n) [j]: `);
          if (answer && answer.trim().toLowerCase() === "n") {
            shouldContinue = false;
          }
        }

        if (!shouldContinue) {
          console.log("\nAbbruch durch Benutzer. Sichere Zustand...");
          lvl.scores = Object.fromEntries(lvlScoresMap.entries());
          lvl.processedIds = Array.from(lvlProcessedSet);
          state.queriedAccounts = Array.from(queriedGlobalSet);
          await saveState();
          console.log(`💾 Gesichert in: ${stateFilePath}`);
          printSummary(state, config.topN);
          return;
        }

        batchCallCounter = 0;
        console.log("\nFortsetzung der Analyse...\n");
      }

      console.log(`${indexStr} 🔎 Lade Followings von @${seed.username} [ID: ${seed.id}]...`);

      try {
        let theirFollowings: any[] = [];
        let callCostUsd = 0;

        if (activeProvider === "twitterapi") {
          const result = await fetchFollowingsViaTwitterApiIo(
            seed.id,
            seed.username,
            twitterApiKey,
            effectiveDelayMs,
            effectiveMaxResults,
          );
          theirFollowings = result.followings;
          callCostUsd = result.costUsd;
        } else {
          const result = await fetchFollowingsViaOfficialX(
            seed.id,
            env.xBearerToken,
            effectiveDelayMs,
            effectiveMaxResults,
          );
          theirFollowings = result.followings;
          callCostUsd = result.costUsd;
        }

        batchCallCounter++;
        state.totalApiCalls = (state.totalApiCalls || 0) + 1;
        state.totalProfilesFetched = (state.totalProfilesFetched || 0) + theirFollowings.length;
        state.estimatedCostUsd = (state.estimatedCostUsd || 0) + callCostUsd;

        for (const target of theirFollowings) {
          if (allSeedIdsEver.has(target.id)) continue;

          if (!lvlScoresMap.has(target.id)) {
            lvlScoresMap.set(target.id, {
              id: target.id,
              username: target.username,
              name: target.name,
              count: 0,
              followedBy: [],
            });
          }
          const item = lvlScoresMap.get(target.id)!;
          item.count += 1;
          if (!item.followedBy) item.followedBy = [];
          if (!item.followedBy.includes(seed.username)) {
            item.followedBy.push(seed.username);
          }
        }

        lvlProcessedSet.add(seed.id);
        queriedGlobalSet.add(seed.id);
        lvl.processedIds = Array.from(lvlProcessedSet);
        lvl.scores = Object.fromEntries(lvlScoresMap.entries());
        state.queriedAccounts = Array.from(queriedGlobalSet);

        // 1. Inkrementelle Speicherung in Supabase
        try {
          const chunkSize = 500;
          const relRows = theirFollowings.map((t: any) => ({
            seed_id: seed.id,
            seed_username: seed.username.toLowerCase(),
            candidate_id: t.id,
            candidate_username: t.username,
            created_at: new Date().toISOString(),
          }));
          for (let rIdx = 0; rIdx < relRows.length; rIdx += chunkSize) {
            await env.supabase.from("alpha_relations").upsert(relRows.slice(rIdx, rIdx + chunkSize), { onConflict: "seed_id,candidate_id" });
          }

          const candRows = theirFollowings.map((t: any) => {
            const item = lvlScoresMap.get(t.id);
            return {
              x_id: t.id,
              username: t.username,
              name: t.name || t.username,
              score: item ? item.count : 1,
              first_level: currentLevel,
              updated_at: new Date().toISOString(),
            };
          });
          for (let cIdx = 0; cIdx < candRows.length; cIdx += chunkSize) {
            await env.supabase.from("alpha_candidates").upsert(candRows.slice(cIdx, cIdx + chunkSize), { onConflict: "x_id" });
          }

          await env.supabase.from("alpha_scanned_accounts").upsert({
            x_id: seed.id,
            username: seed.username,
            name: seed.name,
            level: currentLevel,
            scanned_at: new Date().toISOString(),
            followings_fetched: theirFollowings.length,
            api_provider: activeProvider,
            cost_usd: callCostUsd,
          }, { onConflict: "x_id" });
        } catch (dbErr: any) {
          console.warn(`   ⚠️ Supabase-Sync Hinweis: ${dbErr.message}`);
        }

        // 2. Inkrementelles Backup in lokale JSON (Dual-Write)
        await saveState();

        const costStr = activeProvider === "twitterapi"
          ? `~$${callCostUsd.toFixed(5)} (${(callCostUsd * 100).toFixed(3)}¢)`
          : `~$${callCostUsd.toFixed(2)}`;

        console.log(`   ✅ @${seed.username} (${theirFollowings.length} Followings) -> Pool L${currentLevel}: ${lvlScoresMap.size} | Kosten: ${costStr}`);
      } catch (e: any) {
        if (e.message.includes("402") || e.message.includes("429")) {
          console.error(`\n🚨 Limit erreicht (${e.message})! Sichere Zwischenstand...`);
          lvl.scores = Object.fromEntries(lvlScoresMap.entries());
          lvl.processedIds = Array.from(lvlProcessedSet);
          state.queriedAccounts = Array.from(queriedGlobalSet);
          await saveState();
          printSummary(state, config.topN);
          return;
        }
        console.warn(`   ⚠️ Übersprungen wegen Fehler: ${e.message}`);
      }
    }

    lvl.scores = Object.fromEntries(lvlScoresMap.entries());
    lvl.processedIds = Array.from(lvlProcessedSet);
    state.queriedAccounts = Array.from(queriedGlobalSet);
    await saveState();
  }

  console.log(`\n🎉 Alle gewünschten Ebenen abgeschlossen!`);
  printSummary(state, config.topN);
}

function printSummary(state: StateFile, topN: number) {
  console.log(`\n========================================================================================`);
  console.log(`                           🏆 MULTI-LEVEL ERGEBNIS-ÜBERSICHT 🏆                         `);
  console.log(`========================================================================================`);
  console.log(`🕒 Stand: ${new Date(state.updatedAt).toLocaleString("de-DE")}`);
  console.log(`💰 Geschätzte Gesamtkosten: ~$${(state.estimatedCostUsd || 0).toFixed(4)} USD (${state.totalApiCalls || 0} API-Aufrufe, ${state.totalProfilesFetched || 0} Profile)\n`);

  for (const [lvlKey, lvlData] of Object.entries(state.levels)) {
    const sorted = Object.values(lvlData.scores).sort((a, b) => b.count - a.count);
    const overlaps = sorted.filter((s) => s.count > 1).length;
    console.log(`📊 --- EBENE ${lvlKey}: ${lvlData.name} ---`);
    console.log(`   Seeds: ${lvlData.processedIds.length}/${lvlData.seedCount || lvlData.seedIds?.length} verarbeitet | ${sorted.length} Kandidaten (${overlaps} mit >= 2 Stimmen)`);

    const topList = sorted.slice(0, Math.min(topN, 10));
    if (topList.length > 0) {
      console.table(
        topList.map((a, idx) => ({
          Rang: idx + 1,
          Handle: `@${a.username}`,
          Name: a.name,
          Stimmen: `${a.count} Influencer`,
          Profil: `https://x.com/${a.username}`,
        })),
      );
    }
    console.log("");
  }
}

if (import.meta.main) {
  main();
}
