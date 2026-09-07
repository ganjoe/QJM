/**
 * Zusammenfassung & Ranking-Auswertung für Alpha-Influencer
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

interface StateFile {
  mode: string;
  updatedAt: string;
  totalApiCalls: number;
  processed: string[];
  scores: Record<string, StateEntry>;
}

function printHelp() {
  console.log(`
Verwendung:
  ./show_alpha.sh [Optionen]

Optionen:
  --file <Pfad>      Pfad zur State-Datei (Standard: alpha_state_supabase_registered.json)
  --top <N>, -n <N>  Anzahl der angezeigten Top-Accounts (Standard: 25)
  --min <N>          Nur Accounts anzeigen, denen mindestens N Influencer folgen (Standard: 1)
  --csv              Ergebnisse als CSV exportieren / ausgeben
  --help, -h         Zeigt diese Hilfe an

Beispiele:
  ./show_alpha.sh
  ./show_alpha.sh --min 2
  ./show_alpha.sh --top 50
`);
}

function parseArgs(args: string[]) {
  let filePath = "";
  let topN = 25;
  let minCount = 1;
  let csv = false;

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--help" || arg === "-h") {
      printHelp();
      return null;
    } else if (arg === "--file" && i + 1 < args.length) {
      filePath = args[++i];
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

  return { filePath, topN, minCount, csv };
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
    console.error("   Stelle sicher, dass zuvor ein Suchlauf mit ./run_alpha_finder.sh gestartet wurde.");
    Deno.exit(1);
  }

  let state: StateFile;
  try {
    state = JSON.parse(rawData);
  } catch (err: any) {
    console.error(`❌ Fehler beim Parsen der JSON-Datei: ${err.message}`);
    Deno.exit(1);
  }

  const allEntries: StateEntry[] = Object.values(state.scores || {});
  const processedCount = state.processed?.length || 0;
  const lastUpdate = state.updatedAt ? new Date(state.updatedAt).toLocaleString("de-DE") : "Unbekannt";

  // Filter & Sort
  const filtered = allEntries
    .filter((e) => e.count >= options.minCount)
    .sort((a, b) => b.count - a.count);

  const overlapsCount = allEntries.filter((e) => e.count > 1).length;

  if (options.csv) {
    console.log("Rang,Handle,Name,Gefolgt_von_Anzahl,Profil");
    filtered.slice(0, options.topN).forEach((e, idx) => {
      console.log(`${idx + 1},@${e.username},"${(e.name || "").replace(/"/g, '""')}",${e.count},https://x.com/${e.username}`);
    });
    return;
  }

  console.log("=========================================================================================");
  console.log("                           📊 ALPHA-INFLUENCER AUSWERTUNG                                ");
  console.log("=========================================================================================");
  console.log(`📁 Datei:                ${targetFile}`);
  console.log(`🕒 Stand der Daten:      ${lastUpdate}`);
  console.log(`👤 Analysierte Accounts: ${processedCount} Basis-Influencer`);
  console.log(`🎯 Gesamt-Kandidaten:    ${allEntries.length} gefundene Accounts`);
  console.log(`🔥 Echte Überschneidungen (von >= 2 gefolgt): ${overlapsCount} Accounts`);
  if (options.minCount > 1) {
    console.log(`🔍 Filter aktiv:        Nur Accounts mit mindestens ${options.minCount} Followern aus der Basis`);
  }
  console.log("=========================================================================================\n");

  if (filtered.length === 0) {
    console.log(`Keine Accounts gefunden, die das Kriterium (mind. ${options.minCount} Nennungen) erfüllen.`);
    return;
  }

  const displayList = filtered.slice(0, options.topN);

  console.table(
    displayList.map((a, index) => ({
      Rang: index + 1,
      Handle: `@${a.username}`,
      Name: a.name,
      "Gefolgt von": `${a.count} Influencer(n)`,
      Profil: `https://x.com/${a.username}`,
    })),
  );

  console.log(`\nAngezeigt: Top ${displayList.length} von ${filtered.length} passenden Accounts.`);
  if (overlapsCount > 0 && options.minCount === 1) {
    console.log(`💡 Tipp: Mit './show_alpha.sh --min 2' siehst du nur die ${overlapsCount} Accounts mit echten Schnittmengen.`);
  }
}

if (import.meta.main) {
  main();
}
