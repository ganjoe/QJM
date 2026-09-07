/**
 * Standalone X Alpha-Influencer Finder
 *
 * Findet Accounts ("Alpha-Influencer"), denen besonders viele der von dir gefolgten
 * Accounts folgen, um ein kompaktes, hochqualitatives Signal auf X zu erhalten.
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

function resolveBearerToken(): { token: string; source: string } {
  if (Deno.env.get("X_BEARER_TOKEN")) {
    return { token: Deno.env.get("X_BEARER_TOKEN")!, source: "Umgebungsvariable (Deno.env)" };
  }

  const scriptDir = new URL(".", import.meta.url).pathname;
  const candidates = [
    `${scriptDir}../../../llm-gateway/.env`,
    `${scriptDir}../../llm-gateway/.env`,
    "/home/daniel/QJM/llm-gateway/.env",
    `${scriptDir}../.env`,
    `${scriptDir}.env`,
    "./.env",
  ];

  for (const p of candidates) {
    const env = loadEnvFile(p);
    if (env.X_BEARER_TOKEN) {
      Deno.env.set("X_BEARER_TOKEN", env.X_BEARER_TOKEN);
      return { token: env.X_BEARER_TOKEN, source: p };
    }
  }

  return { token: "", source: "Nicht gefunden" };
}

// --- Rate Limiter & Fetching ---
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
      "Authorization": `Bearer ${bearerToken}`,
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
  return `${rateLimitRemaining}/${rateLimitLimit ?? "?"} verbleibend (Reset in ${resetSec}s)`;
}

// --- X API Calls ---
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

async function fetchFollowings(
  userId: string,
  bearerToken: string,
  delayMs: number,
  maxResults: number,
): Promise<any[]> {
  // max_results in X API v2 is between 1 and 1000
  const safeLimit = Math.min(1000, Math.max(1, maxResults));
  const url = `https://api.twitter.com/2/users/${userId}/following?max_results=${safeLimit}`;
  const res = await throttledXFetch(url, bearerToken, delayMs);
  if (!res.ok) {
    throw new Error(`X API Followings fehlgeschlagen (${res.status}): ${await res.text()}`);
  }
  const data = await res.json();
  return data.data || [];
}

// --- CLI Hilfe & Argument-Parsing ---
function printHelp() {
  console.log(`
Verwendung:
  deno run -A scripts/find_alpha_influencer.ts <username> [Optionen]
  oder: ./run_alpha_finder.sh <username> [Optionen]

Optionen:
  --batch-pause <N>     Pausiert nach jeweils N API-Abfragen für interaktive Bestätigung (Standard: 100)
  --max-results <N>     Max. Followings pro Influencer abrufen (Standard: 1000)
  --delay-ms <N>        Pause zwischen API-Aufrufen in Millisekunden (Standard: 2500)
  --top <N>             Anzahl der am Ende angezeigten Alpha-Accounts (Standard: 20)
  --help, -h            Zeigt diese Hilfe an

Beispiel:
  ./run_alpha_finder.sh elonmusk
  ./run_alpha_finder.sh mein_x_account --batch-pause 50 --top 30
`);
}

interface CliConfig {
  username: string;
  batchPause: number;
  maxResults: number;
  delayMs: number;
  topN: number;
}

function parseCliArgs(args: string[]): CliConfig | null {
  let username = "";
  let batchPause = parseInt(Deno.env.get("BATCH_PAUSE_SIZE") || "100", 10);
  let maxResults = parseInt(Deno.env.get("X_MAX_RESULTS") || "1000", 10);
  let delayMs = parseInt(Deno.env.get("X_REQUEST_DELAY_MS") || "2500", 10);
  let topN = parseInt(Deno.env.get("TOP_N") || "20", 10);

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--help" || arg === "-h") {
      printHelp();
      return null;
    } else if (arg === "--batch-pause" && i + 1 < args.length) {
      batchPause = parseInt(args[++i], 10);
    } else if (arg === "--max-results" && i + 1 < args.length) {
      maxResults = parseInt(args[++i], 10);
    } else if (arg === "--delay-ms" && i + 1 < args.length) {
      delayMs = parseInt(args[++i], 10);
    } else if (arg === "--top" && i + 1 < args.length) {
      topN = parseInt(args[++i], 10);
    } else if (!arg.startsWith("-") && !username) {
      username = arg.replace("@", "").trim();
    }
  }

  if (!username) {
    console.error("❌ Fehler: Bitte gib einen X-Benutzernamen an.");
    printHelp();
    return null;
  }

  return { username, batchPause, maxResults, delayMs, topN };
}

// --- Hauptprogramm ---
async function main() {
  console.log("=========================================");
  console.log("   🚀 X Alpha-Influencer Finder 🚀       ");
  console.log("=========================================\n");

  const config = parseCliArgs(Deno.args);
  if (!config) return;

  const { token: bearerToken, source: tokenSource } = resolveBearerToken();
  if (!bearerToken) {
    console.error("❌ Kritischer Fehler: X_BEARER_TOKEN wurde in keiner Umgebungsvariable oder .env gefunden!");
    console.error("   Bitte trage X_BEARER_TOKEN in llm-gateway/.env ein oder exportiere ihn im Terminal.");
    Deno.exit(1);
  }
  console.log(`🔑 X_BEARER_TOKEN geladen aus: ${tokenSource}`);
  console.log(`⚙️  Konfiguration: Pause alle ${config.batchPause} Calls | Max. Followings: ${config.maxResults} | Delay: ${config.delayMs}ms | Top ${config.topN}\n`);

  // State-Datei vorbereiten (spezifisch für diesen Ziel-Nutzer)
  const scriptDir = new URL(".", import.meta.url).pathname;
  const stateFilePath = `${scriptDir}../alpha_state_${config.username.toLowerCase()}.json`;

  interface StateFile {
    targetUsername: string;
    targetUserId: string;
    updatedAt: string;
    totalApiCalls: number;
    processed: string[];
    scores: Record<string, { id: string; username: string; name: string; count: number }>;
  }

  let state: StateFile = {
    targetUsername: config.username,
    targetUserId: "",
    updatedAt: new Date().toISOString(),
    totalApiCalls: 0,
    processed: [],
    scores: {},
  };

  try {
    const existingRaw = await Deno.readTextFile(stateFilePath);
    const parsed = JSON.parse(existingRaw);
    if (parsed.processed && parsed.scores) {
      state = parsed;
      console.log(`📂 Vorherigen Zustand geladen: ${state.processed.length} Influencer bereits ausgewertet.`);
      console.log(`   Aktuell gespeicherte Alpha-Kandidaten: ${Object.keys(state.scores).length}`);
    }
  } catch {
    console.log(`🆕 Neuer Lauf für @${config.username}. State wird in ${stateFilePath} gespeichert.`);
  }

  const processedIds = new Set<string>(state.processed);
  const alphaScores = new Map<string, { id: string; username: string; name: string; count: number }>();
  for (const [id, entry] of Object.entries(state.scores)) {
    alphaScores.set(id, entry);
  }

  const saveState = async () => {
    state.updatedAt = new Date().toISOString();
    state.processed = Array.from(processedIds);
    state.scores = Object.fromEntries(alphaScores.entries());
    try {
      await Deno.writeTextFile(stateFilePath, JSON.stringify(state, null, 2));
    } catch (err: any) {
      console.error(`⚠️  Konnte Zustand nicht speichern: ${err.message}`);
    }
  };

  // Signal-Handler für Strg+C (SIGINT)
  try {
    Deno.addSignalListener("SIGINT", async () => {
      console.log("\n\n🛑 [Abbruch durch Benutzer via Strg+C]");
      console.log("💾 Speichere aktuellen Stand...");
      await saveState();
      console.log(`✅ Stand erfolgreich in ${stateFilePath} gesichert!`);
      printResults(alphaScores, config.topN);
      Deno.exit(0);
    });
  } catch {
    // Plattform unterstützt Signal-Listener ggf. nicht
  }

  let apiCallsCount = 0;

  // 1. Benutzer-ID ermitteln
  let myUserId = state.targetUserId;
  if (!myUserId) {
    console.log(`🔍 Ermittle X-ID für @${config.username}...`);
    try {
      const user = await getUserIdByUsername(config.username, bearerToken, config.delayMs);
      myUserId = user.id;
      state.targetUserId = myUserId;
      console.log(`   ID gefunden: ${myUserId} (${user.name})`);
      apiCallsCount++;
      state.totalApiCalls = (state.totalApiCalls || 0) + 1;
    } catch (e: any) {
      console.error(`❌ Fehler: ${e.message}`);
      return;
    }
  }

  // 2. Ebene 1: Accounts abrufen, denen der Zielnutzer folgt
  console.log(`\n📋 Rufe Ebene 1 ab: Accounts, denen @${config.username} folgt...`);
  let myFollowings: any[] = [];
  try {
    myFollowings = await fetchFollowings(myUserId, bearerToken, config.delayMs, config.maxResults);
    apiCallsCount++;
    state.totalApiCalls = (state.totalApiCalls || 0) + 1;
    console.log(`   Gefolgte Accounts gefunden: ${myFollowings.length}`);
    console.log(`   [API Rate Limit: ${getRateLimitStatusString()}]\n`);
  } catch (e: any) {
    console.error(`❌ Fehler beim Abrufen der Followings von @${config.username}: ${e.message}`);
    return;
  }

  const myFollowingIds = new Set(myFollowings.map((u) => u.id));

  // 3. Ebene 2: Analyse der Follow-Graphen
  console.log(`🔍 Starte Analyse von Ebene 2 (${myFollowings.length} Influencer)...`);
  console.log(`   (Pausiert nach jeweils ${config.batchPause} Abfragen für Bestätigung)\n`);

  let batchCallCounter = 0;

  for (let i = 0; i < myFollowings.length; i++) {
    const influencer = myFollowings[i];
    const indexStr = `[${i + 1}/${myFollowings.length}]`;

    if (processedIds.has(influencer.id)) {
      console.log(`${indexStr} ⏩ Überspringe @${influencer.username} (bereits im State).`);
      continue;
    }

    // Interaktive Bestätigung alle N API-Calls
    if (batchCallCounter >= config.batchPause) {
      console.log(`\n-------------------------------------------------------------`);
      console.log(`⏸️  PAUSE: ${config.batchPause} Abfragen in diesem Block erreicht!`);
      console.log(`   Bisher verarbeitet: ${processedIds.size}/${myFollowings.length} Influencer.`);
      console.log(`   Gefundene Alpha-Kandidaten: ${alphaScores.size}`);
      console.log(`   API Rate-Limit: ${getRateLimitStatusString()}`);
      console.log(`-------------------------------------------------------------`);

      let shouldContinue = true;
      if (Deno.stdin.isTerminal()) {
        const answer = prompt(`👉 Möchtest du fortfahren? (j/n) [j]: `);
        if (answer && answer.trim().toLowerCase() === "n") {
          shouldContinue = false;
        }
      } else {
        console.log("   (Nicht-interaktive Umgebung erkannt, setze automatisch fort)");
      }

      if (!shouldContinue) {
        console.log("\nAbbruch durch Benutzer. Sichere Zustand...");
        await saveState();
        console.log(`💾 Fortschritt gesichert in: ${stateFilePath}`);
        printResults(alphaScores, config.topN);
        return;
      }

      batchCallCounter = 0;
      console.log("\nFortsetzung der Analyse...\n");
    }

    console.log(`${indexStr} 🔎 Lade Followings von @${influencer.username}...`);

    try {
      const theirFollowings = await fetchFollowings(
        influencer.id,
        bearerToken,
        config.delayMs,
        config.maxResults,
      );
      apiCallsCount++;
      batchCallCounter++;
      state.totalApiCalls = (state.totalApiCalls || 0) + 1;

      for (const target of theirFollowings) {
        // Nicht werten, wenn der Zielnutzer diesem Account bereits selbst folgt oder es der eigene Account ist
        if (myFollowingIds.has(target.id)) continue;
        if (target.id === myUserId) continue;

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

      // Inkrementelle Speicherung nach JEDEM erfolgreichen Account
      await saveState();

      console.log(`   ✅ @${influencer.username} (${theirFollowings.length} Followings) -> Gesamt-Alpha-Pool: ${alphaScores.size} [${getRateLimitStatusString()}]`);
    } catch (e: any) {
      if (e.message.includes("402") || e.message.includes("429")) {
        console.error(`\n🚨 Kritisches Limit erreicht (${e.message})!`);
        console.error("   Beende die Schleife sauber und speichere alle bisherigen Ergebnisse.");
        break;
      }
      console.warn(`   ⚠️ Übersprungen wegen Fehler: ${e.message}`);
    }
  }

  // Abschluss-Sicherung
  await saveState();
  console.log(`\n🎉 Analyse abgeschlossen! Ergebnisse gespeichert in ${stateFilePath}`);
  printResults(alphaScores, config.topN);
}

function printResults(
  alphaScores: Map<string, { id: string; username: string; name: string; count: number }>,
  topN: number,
) {
  const sorted = Array.from(alphaScores.values()).sort((a, b) => b.count - a.count);

  if (sorted.length === 0) {
    console.log("\nKeine neuen Alpha-Influencer entdeckt (alle gefolgten Accounts überschneiden sich oder keine Daten).");
    return;
  }

  console.log(`\n=========================================`);
  console.log(`       🏆 TOP ${Math.min(topN, sorted.length)} ALPHA-INFLUENCER 🏆        `);
  console.log(`=========================================`);
  console.log(`(Accounts, denen deine Influencer am meisten folgen, denen du aber noch NICHT folgst)\n`);

  const topList = sorted.slice(0, topN);
  console.table(
    topList.map((a, index) => ({
      Rang: index + 1,
      Handle: `@${a.username}`,
      Name: a.name,
      Score: `${a.count} Follower`,
      Profil: `https://x.com/${a.username}`,
    })),
  );
}

if (import.meta.main) {
  main();
}
