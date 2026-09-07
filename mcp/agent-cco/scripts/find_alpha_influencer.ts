/**
 * Standalone X Alpha-Influencer Finder
 *
 * Findet Accounts ("Alpha-Influencer"), denen besonders viele der in Supabase registrierten
 * Influencer (Tabelle `x_users`) folgen, um hochqualitative Signal-Accounts zu entdecken.
 *
 * Unterstützt:
 * 1. TwitterAPI.io (Empfohlen: Ultra-Low-Cost, 0,01 $ pro 1.000 Followings via x-api-key)
 * 2. Offizielle X API v2 (Fallback via X_BEARER_TOKEN)
 */

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

  // Für Host-Ausführung: host.docker.internal -> 127.0.0.1 auflösen
  if (supabaseUrl.includes("host.docker.internal")) {
    supabaseUrl = supabaseUrl.replace("host.docker.internal", "127.0.0.1");
  }
  if (!supabaseUrl) supabaseUrl = "http://127.0.0.1:8001";

  return { twitterApiIoKey, xBearerToken, supabaseUrl, supabaseKey, source };
}

// --- Supabase REST Client ---
interface RegisteredUser {
  username: string;
  screen_name: string;
  x_id: string;
}

async function fetchRegisteredInfluencers(supabaseUrl: string, supabaseKey: string): Promise<RegisteredUser[]> {
  const url = `${supabaseUrl}/rest/v1/x_users?is_active=eq.true&select=username,screen_name,x_id`;
  const res = await fetch(url, {
    headers: {
      apikey: supabaseKey,
      Authorization: `Bearer ${supabaseKey}`,
    },
  });

  if (!res.ok) {
    throw new Error(`Fehler beim Abrufen der Influencer aus Supabase (${res.status}): ${await res.text()}`);
  }

  const list: RegisteredUser[] = await res.json();
  return list.filter((u) => u.username && u.x_id);
}

// --- TwitterAPI.io Fetcher (Günstige Alternative: 0,01 $ pro 1.000 Followings) ---
async function fetchFollowingsViaTwitterApiIo(
  userId: string,
  username: string,
  apiKey: string,
  delayMs: number,
  maxResults: number,
): Promise<{ followings: any[]; costUsd: number }> {
  const allFollowings: any[] = [];
  let cursor = "";
  let hasNext = true;
  const pageSize = Math.min(200, maxResults);

  while (hasNext && allFollowings.length < maxResults) {
    if (delayMs > 0 && allFollowings.length > 0) {
      await new Promise((r) => setTimeout(r, delayMs));
    }

    // Übergabe der X User-ID wie vom Nutzer gewünscht (mit Fallback auf userName)
    let url = `https://api.twitterapi.io/twitter/user/followings?pageSize=${pageSize}`;
    if (userId) {
      url += `&userId=${userId}`;
    } else {
      url += `&userName=${username.replace("@", "")}`;
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

    hasNext = Boolean(data.has_next_page && data.next_cursor && allFollowings.length < maxResults);
    cursor = data.next_cursor || "";
  }

  // Kosten bei TwitterAPI.io: 0,01 $ pro 1.000 Followings (0,00001 $ pro Profil)
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

function getRateLimitStatusString(): string {
  if (rateLimitRemaining === null) return "Status: unbekannt";
  const resetSec = rateLimitResetEpoch ? Math.max(0, Math.round(rateLimitResetEpoch - Date.now() / 1000)) : 0;
  return `${rateLimitRemaining}/${rateLimitLimit ?? "?"} übrig (Reset in ${resetSec}s)`;
}

async function fetchFollowingsViaOfficialX(
  userId: string,
  bearerToken: string,
  delayMs: number,
  maxResults: number,
): Promise<{ followings: any[]; costUsd: number }> {
  const safeLimit = Math.min(1000, Math.max(1, maxResults));
  const url = `https://api.twitter.com/2/users/${userId}/following?max_results=${safeLimit}`;
  const res = await throttledXFetch(url, bearerToken, delayMs);
  if (!res.ok) {
    throw new Error(`X API Followings Fehler (${res.status}): ${await res.text()}`);
  }
  const data = await res.json();
  const list = data.data || [];
  // Kosten bei offizieller X API: 0,01 $ pro zurückgegebenes Profil!
  const costUsd = list.length * 0.01;
  return { followings: list, costUsd };
}

async function getUserIdByUsername(username: string, bearerToken: string, delayMs: number): Promise<{ id: string; name: string }> {
  const clean = username.replace("@", "").trim();
  const url = `https://api.twitter.com/2/users/by/username/${clean}`;
  const res = await throttledXFetch(url, bearerToken, delayMs);
  if (!res.ok) {
    throw new Error(`X API User Lookup fehlgeschlagen (${res.status}): ${await res.text()}`);
  }
  const data = await res.json();
  if (!data.data?.id) {
    throw new Error(`Account @${clean} wurde auf X nicht gefunden.`);
  }
  return { id: data.data.id, name: data.data.name };
}

// --- CLI Hilfe & Argument-Parsing ---
function printHelp() {
  console.log(`
Verwendung:
  ./run_alpha_finder.sh [Optionen]                  (analysiert alle in Supabase registrierten Influencer)
  ./run_alpha_finder.sh <username> [Optionen]       (optional: analysiert einen spezifischen Account)

Optionen:
  --provider <twitterapi|x>  Wählt den API-Provider (Standard: twitterapi, falls Key vorhanden, sonst x)
  --key <API_KEY>            TwitterAPI.io API-Key direkt übergeben
  --batch-pause <N>          Pausiert nach jeweils N API-Abfragen für Bestätigung (Standard: 100)
  --max-results <N>          Max. Followings pro Influencer abrufen (Standard: 200 bei TwitterAPI, 100 bei X)
  --delay-ms <N>             Pause zwischen API-Aufrufen in ms (Standard: 1000 bei TwitterAPI, 2500 bei X)
  --top <N>                  Anzahl der am Ende angezeigten Alpha-Accounts (Standard: 20)
  --help, -h                 Zeigt diese Hilfe an

Beispiele:
  ./run_alpha_finder.sh                             (Standard mit registrierten Influencern)
  ./run_alpha_finder.sh --key deinkey12345           (TwitterAPI.io Key direkt übergeben)
  ./run_alpha_finder.sh --provider twitterapi --top 30
`);
}

interface CliConfig {
  customUsername?: string;
  provider?: "twitterapi" | "x";
  directApiKey?: string;
  batchPause: number;
  maxResults?: number;
  delayMs?: number;
  topN: number;
}

function parseCliArgs(args: string[]): CliConfig | null {
  let customUsername: string | undefined = undefined;
  let provider: "twitterapi" | "x" | undefined = undefined;
  let directApiKey: string | undefined = undefined;
  let batchPause = parseInt(Deno.env.get("BATCH_PAUSE_SIZE") || "100", 10);
  let maxResults: number | undefined = Deno.env.get("X_MAX_RESULTS") ? parseInt(Deno.env.get("X_MAX_RESULTS")!, 10) : undefined;
  let delayMs: number | undefined = Deno.env.get("X_REQUEST_DELAY_MS") ? parseInt(Deno.env.get("X_REQUEST_DELAY_MS")!, 10) : undefined;
  let topN = parseInt(Deno.env.get("TOP_N") || "20", 10);

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--help" || arg === "-h") {
      printHelp();
      return null;
    } else if (arg === "--provider" && i + 1 < args.length) {
      const p = args[++i].toLowerCase();
      if (p === "twitterapi" || p === "x") provider = p;
    } else if (arg === "--key" && i + 1 < args.length) {
      directApiKey = args[++i];
    } else if (arg === "--batch-pause" && i + 1 < args.length) {
      batchPause = parseInt(args[++i], 10);
    } else if (arg === "--max-results" && i + 1 < args.length) {
      maxResults = parseInt(args[++i], 10);
    } else if (arg === "--delay-ms" && i + 1 < args.length) {
      delayMs = parseInt(args[++i], 10);
    } else if (arg === "--top" && i + 1 < args.length) {
      topN = parseInt(args[++i], 10);
    } else if (!arg.startsWith("-") && !customUsername) {
      customUsername = arg.replace("@", "").trim();
    }
  }

  return { customUsername, provider, directApiKey, batchPause, maxResults, delayMs, topN };
}

// --- Hauptprogramm ---
async function main() {
  console.log("=========================================");
  console.log("   🚀 X Alpha-Influencer Finder 🚀       ");
  console.log("=========================================\n");

  const config = parseCliArgs(Deno.args);
  if (!config) return;

  const env = resolveAppEnv();
  const twitterApiKey = config.directApiKey || env.twitterApiIoKey;

  // Provider-Auswahl: TwitterAPI.io bevorzugen wenn Key da ist, sonst offizielle X-API
  let activeProvider: "twitterapi" | "x" = config.provider || (twitterApiKey ? "twitterapi" : "x");

  if (activeProvider === "twitterapi" && !twitterApiKey) {
    console.warn("⚠️  TwitterAPI.io gewählt, aber kein Key gefunden. Wechsle zur offiziellen X API v2.");
    activeProvider = "x";
  }

  if (activeProvider === "x" && !env.xBearerToken) {
    console.error("❌ Kritischer Fehler: Weder TWITTER_API_IO_KEY noch X_BEARER_TOKEN gefunden!");
    console.error("   Trage TWITTER_API_IO_KEY (empfohlen) oder X_BEARER_TOKEN in llm-gateway/.env ein.");
    Deno.exit(1);
  }

  // Provider-spezifische Defaults
  const defaultMaxResults = activeProvider === "twitterapi" ? 500 : 100;
  const defaultDelayMs = activeProvider === "twitterapi" ? 1000 : 2500;
  const effectiveMaxResults = config.maxResults ?? defaultMaxResults;
  const effectiveDelayMs = config.delayMs ?? defaultDelayMs;

  console.log(`🔑 Konfiguration geladen aus: ${env.source}`);
  if (activeProvider === "twitterapi") {
    console.log(`✨ Provider: TwitterAPI.io (Ultra-Low-Cost: 0,01 $ pro 1.000 Followings)`);
  } else {
    console.log(`⚠️  Provider: Offizielle X API v2 (Achtung: 0,01 $ pro Profil = 10 $ pro 1.000 Followings!)`);
  }
  console.log(`⚙️  Einstellungen: Max ${effectiveMaxResults} Followings/Influencer | Delay: ${effectiveDelayMs}ms | Pause alle ${config.batchPause} Calls\n`);

  const scriptDir = new URL(".", import.meta.url).pathname;

  interface InfluencerItem {
    id: string;
    username: string;
    name?: string;
  }

  let baseInfluencers: InfluencerItem[] = [];
  let stateKey = "supabase_registered";

  if (config.customUsername) {
    stateKey = config.customUsername.toLowerCase();
    console.log(`🎯 Modus: Spezifischer Account @${config.customUsername}`);
    try {
      const user = await getUserIdByUsername(config.customUsername, env.xBearerToken, effectiveDelayMs);
      console.log(`   ID: ${user.id} (${user.name})`);
      console.log(`   Lade Followings...`);
      const res = activeProvider === "twitterapi"
        ? await fetchFollowingsViaTwitterApiIo(user.id, config.customUsername, twitterApiKey, effectiveDelayMs, effectiveMaxResults)
        : await fetchFollowingsViaOfficialX(user.id, env.xBearerToken, effectiveDelayMs, effectiveMaxResults);
      baseInfluencers = res.followings.map((f: any) => ({ id: f.id, username: f.username, name: f.name }));
      console.log(`   ${baseInfluencers.length} Followings als Basis geladen.`);
    } catch (e: any) {
      console.error(`❌ Fehler: ${e.message}`);
      return;
    }
  } else {
    console.log("📊 Modus: Alle in Supabase (x_users) registrierten Influencer");
    try {
      const dbUsers = await fetchRegisteredInfluencers(env.supabaseUrl, env.supabaseKey);
      baseInfluencers = dbUsers.map((u) => ({ id: u.x_id, username: u.username, name: u.screen_name }));
      console.log(`   ✅ ${baseInfluencers.length} aktive Influencer aus Datenbank geladen:`);
      for (const inf of baseInfluencers) {
        console.log(`      - @${inf.username} (${inf.name || "N/A"}) [X User-ID: ${inf.id}]`);
      }
    } catch (e: any) {
      console.error(`❌ Konnte Influencer nicht aus Supabase laden: ${e.message}`);
      return;
    }
  }

  if (baseInfluencers.length === 0) {
    console.log("Keine Basis-Influencer zur Analyse gefunden.");
    return;
  }

  const baseIds = new Set(baseInfluencers.map((u) => u.id));
  const stateFilePath = `${scriptDir}../alpha_state_${stateKey}.json`;

  interface StateFile {
    mode: string;
    provider: string;
    updatedAt: string;
    totalApiCalls: number;
    totalProfilesFetched: number;
    estimatedCostUsd: number;
    processed: string[];
    scores: Record<string, { id: string; username: string; name: string; count: number }>;
  }

  let state: StateFile = {
    mode: stateKey,
    provider: activeProvider,
    updatedAt: new Date().toISOString(),
    totalApiCalls: 0,
    totalProfilesFetched: 0,
    estimatedCostUsd: 0,
    processed: [],
    scores: {},
  };

  try {
    const existingRaw = await Deno.readTextFile(stateFilePath);
    const parsed = JSON.parse(existingRaw);
    if (parsed.processed && parsed.scores) {
      state = parsed;
      console.log(`\n📂 Vorherigen Zustand geladen: ${state.processed.length} Influencer bereits ausgewertet.`);
      console.log(`   Aktuell gespeicherte Alpha-Kandidaten: ${Object.keys(state.scores).length}`);
      if (state.estimatedCostUsd) {
        console.log(`   Bisherige geschätzte Kosten: ~$${state.estimatedCostUsd.toFixed(4)} USD`);
      }
    }
  } catch {
    console.log(`\n🆕 Neuer Analyse-Lauf. State wird in ${stateFilePath} gespeichert.`);
  }

  const processedIds = new Set<string>(state.processed);
  const alphaScores = new Map<string, { id: string; username: string; name: string; count: number }>();
  for (const [id, entry] of Object.entries(state.scores)) {
    alphaScores.set(id, entry);
  }

  const saveState = async () => {
    state.updatedAt = new Date().toISOString();
    state.provider = activeProvider;
    state.processed = Array.from(processedIds);
    state.scores = Object.fromEntries(alphaScores.entries());
    try {
      await Deno.writeTextFile(stateFilePath, JSON.stringify(state, null, 2));
    } catch (err: any) {
      console.error(`⚠️  Konnte Zustand nicht speichern: ${err.message}`);
    }
  };

  // Signal-Handler für Strg+C
  try {
    Deno.addSignalListener("SIGINT", async () => {
      console.log("\n\n🛑 [Abbruch durch Benutzer via Strg+C]");
      console.log("💾 Speichere aktuellen Stand...");
      await saveState();
      console.log(`✅ Stand erfolgreich in ${stateFilePath} gesichert!`);
      printResults(alphaScores, config.topN, state.estimatedCostUsd);
      Deno.exit(0);
    });
  } catch {
    // Plattform unterstützt Signal-Listener ggf. nicht
  }

  console.log(`\n🔍 Starte Analyse für ${baseInfluencers.length} Influencer...`);
  console.log(`   (Pausiert alle ${config.batchPause} Abfragen für Bestätigung)\n`);

  let batchCallCounter = 0;

  for (let i = 0; i < baseInfluencers.length; i++) {
    const influencer = baseInfluencers[i];
    const indexStr = `[${i + 1}/${baseInfluencers.length}]`;

    if (processedIds.has(influencer.id)) {
      console.log(`${indexStr} ⏩ Überspringe @${influencer.username} (bereits im State analysiert).`);
      continue;
    }

    // Interaktive Bestätigung alle N API-Calls
    if (batchCallCounter >= config.batchPause) {
      console.log(`\n-------------------------------------------------------------`);
      console.log(`⏸️  PAUSE: ${config.batchPause} Abfragen in diesem Block erreicht!`);
      console.log(`   Bisher verarbeitet: ${processedIds.size}/${baseInfluencers.length} Influencer.`);
      console.log(`   Gefundene Alpha-Kandidaten: ${alphaScores.size}`);
      console.log(`   Bisherige Kosten: ~$${(state.estimatedCostUsd || 0).toFixed(4)} USD`);
      console.log(`-------------------------------------------------------------`);

      let shouldContinue = true;
      if (Deno.stdin.isTerminal()) {
        const answer = prompt(`👉 Möchtest du fortfahren? (j/n) [j]: `);
        if (answer && answer.trim().toLowerCase() === "n") {
          shouldContinue = false;
        }
      }

      if (!shouldContinue) {
        console.log("\nAbbruch durch Benutzer. Sichere Zustand...");
        await saveState();
        console.log(`💾 Fortschritt gesichert in: ${stateFilePath}`);
        printResults(alphaScores, config.topN, state.estimatedCostUsd);
        return;
      }

      batchCallCounter = 0;
      console.log("\nFortsetzung der Analyse...\n");
    }

    console.log(`${indexStr} 🔎 Lade Followings von @${influencer.username} (User-ID: ${influencer.id})...`);

    try {
      let theirFollowings: any[] = [];
      let callCostUsd = 0;

      if (activeProvider === "twitterapi") {
        // TwitterAPI.io mit User-ID aufrufen
        const result = await fetchFollowingsViaTwitterApiIo(
          influencer.id,
          influencer.username,
          twitterApiKey,
          effectiveDelayMs,
          effectiveMaxResults,
        );
        theirFollowings = result.followings;
        callCostUsd = result.costUsd;
      } else {
        // Offizielle X API
        const result = await fetchFollowingsViaOfficialX(
          influencer.id,
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
        // Nicht werten, wenn der Account bereits selbst ein registrierter Basis-Influencer ist
        if (baseIds.has(target.id)) continue;

        if (!alphaScores.has(target.id)) {
          alphaScores.set(target.id, {
            id: target.id,
            username: target.username,
            name: target.name,
            count: 0,
          });
        }
        alphaScores.get(target.id)!.count += 1;
      }

      processedIds.add(influencer.id);
      await saveState();

      const costStr = activeProvider === "twitterapi"
        ? `~$${callCostUsd.toFixed(5)} (${(callCostUsd * 100).toFixed(3)}¢)`
        : `~$${callCostUsd.toFixed(2)}`;

      console.log(`   ✅ @${influencer.username} (${theirFollowings.length} Followings) -> Pool: ${alphaScores.size} | Kosten: ${costStr} (Gesamt: ~$${state.estimatedCostUsd.toFixed(4)})`);
    } catch (e: any) {
      if (e.message.includes("402") || e.message.includes("429")) {
        console.error(`\n🚨 Limit erreicht (${e.message})!`);
        console.error("   Beende die Schleife sauber und speichere alle bisherigen Ergebnisse.");
        break;
      }
      console.warn(`   ⚠️ Übersprungen wegen Fehler: ${e.message}`);
    }
  }

  await saveState();
  console.log(`\n🎉 Analyse abgeschlossen! Ergebnisse gespeichert in ${stateFilePath}`);
  printResults(alphaScores, config.topN, state.estimatedCostUsd);
}

function printResults(
  alphaScores: Map<string, { id: string; username: string; name: string; count: number }>,
  topN: number,
  costUsd?: number,
) {
  const sorted = Array.from(alphaScores.values()).sort((a, b) => b.count - a.count);

  if (sorted.length === 0) {
    console.log("\nKeine neuen Alpha-Influencer entdeckt.");
    return;
  }

  console.log(`\n========================================================================================`);
  console.log(`       🏆 TOP ${Math.min(topN, sorted.length)} ALPHA-INFLUENCER AUS DEN REGISTRIERTEN ACCOUNTS 🏆        `);
  console.log(`========================================================================================`);
  if (costUsd !== undefined) {
    console.log(`💰 Geschätzte Gesamtkosten dieses Datenbestands: ~$${costUsd.toFixed(4)} USD\n`);
  }

  const topList = sorted.slice(0, topN);
  console.table(
    topList.map((a, index) => ({
      Rang: index + 1,
      Handle: `@${a.username}`,
      Name: a.name,
      "Gefolgt von": `${a.count} Influencer(n)`,
      Profil: `https://x.com/${a.username}`,
    })),
  );
}

if (import.meta.main) {
  main();
}
