/**
 * Zusammenfassung & Ranking-Auswertung für Multi-Level Alpha-Influencer
 *
 * Liest den gespeicherten Zustand (alpha_state_*.json) aus und gibt
 * die Rangliste und Überschneidungen formatiert aus, ohne neue API-Calls zu machen.
 */

interface StateEntry {
  id: string;
  username: string;
  name: string;
  count: number;
}

interface LevelData {
  level: number;
  name: string;
  seedCount: number;
  seedIds: string[];
  processedIds: string[];
  scores: Record<string, StateEntry>;
}

interface StateFile {
  mode: string;
  provider?: string;
  updatedAt: string;
  totalApiCalls: number;
  totalProfilesFetched?: number;
  estimatedCostUsd?: number;
  queriedAccounts?: string[];
  levels?: Record<string, LevelData>;
  processed?: string[];
  scores?: Record<string, StateEntry>;
}

function printHelp() {
  console.log(`
Verwendung:
  ./show_alpha.sh [Optionen]

Optionen:
  --level <1|2|all>  Welche Ebene angezeigt werden soll (Standard: all)
  --top <N>, -n <N>  Anzahl der angezeigten Top-Accounts (Standard: 25)
  --min <N>          Nur Accounts anzeigen, denen mindestens N Influencer folgen (Standard: 1)
  --file <Pfad>      Pfad zur State-Datei (Standard: alpha_state_supabase_registered.json)
  --csv              Ergebnisse als CSV exportieren / ausgeben
  --help, -h         Zeigt diese Hilfe an

Beispiele:
  ./show_alpha.sh
  ./show_alpha.sh --level 1 --min 3
  ./show_alpha.sh --level 2 --top 50
`);
}

function parseArgs(args: string[]) {
  let filePath = "";
  let topN = 25;
  let minCount = 1;
  let level = "all";
  let csv = false;

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--help" || arg === "-h") {
      printHelp();
      return null;
    } else if (arg === "--file" && i + 1 < args.length) {
      filePath = args[++i];
    } else if (arg === "--level" && i + 1 < args.length) {
      level = args[++i];
    } else if ((arg === "--top" || arg === "-n") && i + 1 < args.length) {
      topN = parseInt(args[++i], 10);
    } else if (arg === "--min" && i + 1 < args.length) {
      minCount = parseInt(args[++i], 10);
    } else if (arg === "--csv") {
      csv = true;
    } else if (!arg.startsWith("-") && !filePath) {
      filePath = arg;
    }
  }

  return { filePath, topN, minCount, level, csv };
}

async function main() {
  const options = parseArgs(Deno.args);
  if (!options) return;

  const scriptDir = new URL(".", import.meta.url).pathname;
  let targetFile = options.filePath;

  if (!targetFile) {
    targetFile = `${scriptDir}../alpha_state_supabase_registered.json`;
  }

  let rawData = "";
  try {
    rawData = await Deno.readTextFile(targetFile);
  } catch (_e) {
    console.error(`❌ Fehler: Konnte State-Datei nicht öffnen: ${targetFile}`);
    Deno.exit(1);
  }

  let state: StateFile;
  try {
    state = JSON.parse(rawData);
  } catch (err: any) {
    console.error(`❌ Fehler beim Parsen der JSON-Datei: ${err.message}`);
    Deno.exit(1);
  }

  const lastUpdate = state.updatedAt ? new Date(state.updatedAt).toLocaleString("de-DE") : "Unbekannt";
  const costStr = state.estimatedCostUsd !== undefined ? `~$${state.estimatedCostUsd.toFixed(4)} USD` : "k.A.";

  console.log("=========================================================================================");
  console.log("                           📊 MULTI-LEVEL ALPHA-AUSWERTUNG                               ");
  console.log("=========================================================================================");
  console.log(`📁 Datei:                ${targetFile}`);
  console.log(`🕒 Stand der Daten:      ${lastUpdate}`);
  console.log(`💰 Geschätzte Kosten:    ${costStr} (${state.totalApiCalls || 0} API-Calls, ${state.totalProfilesFetched || 0} Profile)`);
  if (state.queriedAccounts) {
    console.log(`🔒 Einzigartige Accounts abgefragt: ${state.queriedAccounts.length} (Nie doppelt!)`);
  }
  console.log("=========================================================================================\n");

  // Levels auflösen
  const levelsMap: Record<string, { name: string; scores: Record<string, StateEntry>; processedCount: number; seedCount?: number }> = {};

  if (state.levels && Object.keys(state.levels).length > 0) {
    for (const [k, v] of Object.entries(state.levels)) {
      levelsMap[k] = {
        name: v.name || `Ebene ${k}`,
        scores: v.scores || {},
        processedCount: v.processedIds?.length || 0,
        seedCount: v.seedCount || v.seedIds?.length || 0,
      };
    }
  } else if (state.scores) {
    levelsMap["1"] = {
      name: "Basis-Influencer (Ebene 1)",
      scores: state.scores,
      processedCount: state.processed?.length || 0,
    };
  }

  for (const [lvlKey, lvlData] of Object.entries(levelsMap)) {
    if (options.level !== "all" && options.level !== lvlKey) {
      continue;
    }

    const allEntries: StateEntry[] = Object.values(lvlData.scores);
    const filtered = allEntries
      .filter((e) => e.count >= options.minCount)
      .sort((a, b) => b.count - a.count);

    const overlapsCount = allEntries.filter((e) => e.count > 1).length;

    console.log(`🔹 --- ${lvlData.name.toUpperCase()} ---`);
    console.log(`   Fortschritt:          ${lvlData.processedCount}/${lvlData.seedCount || "?"} Seeds verarbeitet`);
    console.log(`   Gesamt-Kandidaten:    ${allEntries.length} gefundene Accounts`);
    console.log(`   Echte Schnittmengen:  ${overlapsCount} Accounts (von >= 2 gefolgt)`);
    if (options.minCount > 1) {
      console.log(`   Filter aktiv:         Score >= ${options.minCount}`);
    }

    if (filtered.length === 0) {
      console.log(`   Keine Accounts gefunden, die den Filter erfüllen.\n`);
      continue;
    }

    const displayList = filtered.slice(0, options.topN);

    console.table(
      displayList.map((a, index) => ({
        Rang: index + 1,
        Handle: `@${a.username}`,
        Name: a.name,
        Stimmen: `${a.count} Influencer`,
        Profil: `https://x.com/${a.username}`,
      })),
    );

    console.log(`   (Angezeigt: Top ${displayList.length} von ${filtered.length} passenden Accounts)\n`);
  }
}

if (import.meta.main) {
  main();
}
