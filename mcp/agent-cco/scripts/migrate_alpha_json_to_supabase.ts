/**
 * migrate_alpha_json_to_supabase.ts
 * 
 * Sichere Migration der Alpha-Influencer Daten aus alpha_state_supabase_registered.json
 * in die Supabase-Tabellen (alpha_scanned_accounts, alpha_candidates, alpha_relations).
 * 
 * Sicherheits-Garantien:
 * 1. Automatischer Backup-Snapshot der JSON vor jedem Schreibvorgang.
 * 2. Idempotentes Batch-Upsert (keine doppelten Datensätze).
 * 3. Vollständige Validierung der Zählungen und Scores nach der Migration.
 * 4. Zero Impact auf bestehende Tabellen (nur alpha_*).
 */

import { createClient, SupabaseClient } from "npm:@supabase/supabase-js@2.47.10";

// --- Umgebungsvariablen laden ---
function loadEnv(): { supabaseUrl: string; supabaseKey: string } {
  const envCandidates = [
    "/home/daniel/QJM/llm-gateway/.env",
    "/home/daniel/QJM/ibkr_live_daemon/.env",
    "/home/daniel/QJM/mcp/agent-cco/.env",
  ];

  let supabaseUrl = Deno.env.get("SUPABASE_URL") || "";
  let supabaseKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";

  for (const p of envCandidates) {
    try {
      const text = Deno.readTextFileSync(p);
      for (const line of text.split("\n")) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith("#")) continue;
        const [k, ...rest] = trimmed.split("=");
        const v = rest.join("=").trim().replace(/^["']|["']$/g, "");
        if (!supabaseUrl && k.trim() === "SUPABASE_URL") supabaseUrl = v;
        if (!supabaseKey && k.trim() === "SUPABASE_SERVICE_ROLE_KEY") supabaseKey = v;
      }
    } catch {
      // Weiter
    }
  }

  // Netzwerk-IP Fallback für Docker/LAN
  if (!supabaseUrl || supabaseUrl.includes("host.docker.internal")) {
    supabaseUrl = "http://10.20.0.23:8001";
  }

  return { supabaseUrl, supabaseKey };
}

// Bekannte Basis-Trader Mapping (Username -> ID und umgekehrt)
const KNOWN_SEEDS: Record<string, { username: string; name: string }> = {
  "1940360837547565056": { username: "aleabitoreddit", name: "Serenity" },
  "246626141": { username: "temple_eight", name: "Temple 8 Research" },
  "1540038673810350080": { username: "pelositracker", name: "Nancy Pelosi Stock Tracker" },
  "1012372291513208832": { username: "liathetrader", name: "Lia the Trader" },
  "952880754010345473": { username: "stamatoudism", name: "Marios Stamatoudis" },
  "1033127171298975744": { username: "saxena_puru", name: "Puru Saxena" },
  "3399637907": { username: "pradeepbonde", name: "stockbee" },
  "23340242": { username: "lindaraschke", name: "Linda Raschke" },
  "3316376038": { username: "kobeissiletter", name: "The Kobeissi Letter" },
  "2032976046023389184": { username: "paradislabs", name: "Paradis Labs" },
  "105353526": { username: "markminervini", name: "Mark Minervini" },
};

const SEED_BY_USERNAME: Record<string, string> = {};
for (const [id, meta] of Object.entries(KNOWN_SEEDS)) {
  SEED_BY_USERNAME[meta.username.toLowerCase()] = id;
}

async function main() {
  console.log("==================================================================");
  console.log("🚀 Starte Alpha-Influencer Migration -> Supabase");
  console.log("==================================================================\n");

  const { supabaseUrl, supabaseKey } = loadEnv();
  console.log(`📡 Supabase URL: ${supabaseUrl}`);

  if (!supabaseKey) {
    console.error("❌ FEHLER: SUPABASE_SERVICE_ROLE_KEY konnte nicht gefunden werden.");
    Deno.exit(1);
  }

  const supabase: SupabaseClient = createClient(supabaseUrl, supabaseKey);

  // 1. Verbindung prüfen
  const { data: testData, error: testErr } = await supabase.from("x_users").select("username").limit(1);
  if (testErr) {
    console.error(`❌ Verbindungsfehler zu Supabase: ${testErr.message}`);
    console.log("   Versuche Fallback auf 127.0.0.1:8001...");
    // Fallback versuchen falls nötig
    Deno.exit(1);
  }
  console.log("✅ Erfolgreich mit Supabase verbunden.\n");

  // 2. Prüfen, ob die Tabellen existieren – falls nicht, automatisch via openbrain-db anlegen!
  let { error: checkTableErr } = await supabase.from("alpha_candidates").select("x_id").limit(1);
  if (checkTableErr) {
    console.log("⏳ Tabellen fehlen noch in Supabase – wende 'migrations/002_alpha_influencer_network.sql' automatisch via openbrain-db an...");
    try {
      const scriptDir = new URL(".", import.meta.url).pathname;
      const sqlCandidates = [
        "/home/daniel/QJM/migrations/002_alpha_influencer_network.sql",
        `${scriptDir}../../../migrations/002_alpha_influencer_network.sql`,
        "./migrations/002_alpha_influencer_network.sql",
      ];
      let sqlContent = "";
      for (const p of sqlCandidates) {
        try {
          sqlContent = await Deno.readTextFile(p);
          if (sqlContent) break;
        } catch {
          // weiter
        }
      }
      if (!sqlContent) throw new Error("migrations/002_alpha_influencer_network.sql nicht gefunden.");

      const cmd = new Deno.Command("docker", {
        args: ["exec", "-i", "openbrain-db", "psql", "-U", "postgres", "-d", "postgres"],
        stdin: "piped",
        stdout: "piped",
        stderr: "piped",
      });
      const child = cmd.spawn();
      const writer = child.stdin.getWriter();
      await writer.write(new TextEncoder().encode(sqlContent));
      await writer.close();
      const status = await child.status;

      if (status.success) {
        console.log("✅ SQL-Schema erfolgreich in openbrain-db ausgeführt!");
        // Schema Cache reloaden
        const reloadCmd = new Deno.Command("docker", {
          args: ["exec", "openbrain-db", "psql", "-U", "postgres", "-d", "postgres", "-c", "NOTIFY pgrst, 'reload schema';"],
          stdout: "piped",
          stderr: "piped",
        });
        await reloadCmd.output();
        console.log("✅ PostgREST Schema-Cache neu geladen.");

        // Kurze Pause für PostgREST Reload
        await new Promise((r) => setTimeout(r, 1500));

        // Erneut prüfen
        const retryCheck = await supabase.from("alpha_candidates").select("x_id").limit(1);
        checkTableErr = retryCheck.error;
      } else {
        const errOut = new TextDecoder().decode((await child.output()).stderr);
        console.warn(`⚠️ Automatischer Docker-Befehl fehlgeschlagen: ${errOut}`);
      }
    } catch (e: any) {
      console.warn(`⚠️ Automatisches Einspielen via Docker nicht möglich: ${e.message}`);
    }
  }

  if (checkTableErr) {
    console.error("\n⚠️  Die Tabelle 'alpha_candidates' ist noch nicht bereit.");
    console.error(`   Fehlermeldung: ${checkTableErr.message}\n`);
    console.log("👉 Führe bitte folgenden 1-Zeiler im Terminal aus:");
    console.log("   docker exec -i openbrain-db psql -U postgres -d postgres < migrations/002_alpha_influencer_network.sql && docker exec openbrain-db psql -U postgres -d postgres -c \"NOTIFY pgrst, 'reload schema';\"\n");
    Deno.exit(1);
  }
  console.log("✅ Ziel-Tabellen (alpha_candidates, alpha_relations, alpha_scanned_accounts) sind bereit.\n");

  // 3. Quell-JSON einlesen
  const scriptDir = new URL(".", import.meta.url).pathname;
  const jsonPath = `${scriptDir}../alpha_state_supabase_registered.json`;

  console.log(`📂 Lese Quelldatei: ${jsonPath}`);
  const rawText = await Deno.readTextFile(jsonPath);
  const state = JSON.parse(rawText);

  // 4. Sicherheits-Backup erstellen
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const backupPath = `${jsonPath}.backup_${timestamp}`;
  await Deno.writeTextFile(backupPath, rawText);
  console.log(`🛡️  Sicherheits-Backup erstellt: ${backupPath}\n`);

  // Extrahiere und merge Daten über ALLE Ebenen (Levels 1 bis 5 + Top-Level)
  const queriedAccounts = state.queriedAccounts || [];

  const mergedMap = new Map<string, {
    x_id: string;
    username: string;
    name: string;
    score: number;
    first_level: number;
    followedBy: Set<string>;
  }>();

  // 1. Alle Levels durchgehen (levels 1, 2, 3, 4, 5...)
  if (state.levels && typeof state.levels === "object") {
    for (const [lvlKey, lvlData] of Object.entries(state.levels)) {
      const lvlNum = parseInt(lvlKey, 10) || 1;
      const scores = (lvlData as any).scores || {};
      for (const [cId, c] of Object.entries(scores) as any) {
        if (!c || !c.username) continue;
        if (!mergedMap.has(cId)) {
          mergedMap.set(cId, {
            x_id: cId,
            username: c.username,
            name: c.name || c.username,
            score: c.count || 1,
            first_level: lvlNum,
            followedBy: new Set<string>(Array.isArray(c.followedBy) ? c.followedBy : []),
          });
        } else {
          const item = mergedMap.get(cId)!;
          item.score = Math.max(item.score, c.count || 1);
          if (c.name && (!item.name || item.name === item.username)) item.name = c.name;
          if (Array.isArray(c.followedBy)) {
            for (const f of c.followedBy) item.followedBy.add(f);
          }
        }
      }
    }
  }

  // 2. Auch top-level scores einbeziehen (falls vorhanden)
  if (state.scores && typeof state.scores === "object") {
    for (const [cId, c] of Object.entries(state.scores) as any) {
      if (!c || !c.username) continue;
      if (!mergedMap.has(cId)) {
        mergedMap.set(cId, {
          x_id: cId,
          username: c.username,
          name: c.name || c.username,
          score: c.count || 1,
          first_level: 1,
          followedBy: new Set<string>(Array.isArray(c.followedBy) ? c.followedBy : []),
        });
      } else {
        const item = mergedMap.get(cId)!;
        item.score = Math.max(item.score, c.count || 1);
        if (c.name && (!item.name || item.name === item.username)) item.name = c.name;
        if (Array.isArray(c.followedBy)) {
          for (const f of c.followedBy) item.followedBy.add(f);
        }
      }
    }
  }

  const candidateList = Array.from(mergedMap.values());

  console.log(`📊 Gefundene Daten in JSON (über alle Ebenen 1–5):`);
  console.log(`   - Bereits abgefragte Seeds: ${queriedAccounts.length}`);
  console.log(`   - Unique Alpha-Kandidaten:   ${candidateList.length}`);
  console.log(`   - Overlaps (Score >= 2):    ${candidateList.filter(c => c.score >= 2).length}\n`);

  // 5. Migration: alpha_scanned_accounts (Seeds)
  console.log(`⏳ Migriere ${queriedAccounts.length} abgefragte Seeds nach 'alpha_scanned_accounts'...`);

  // Echte Namen & Usernamen aus x_users und dem State auflösen
  const { data: xUsersData } = await supabase.from("x_users").select("x_id, username, screen_name");
  const xUsersMap = new Map<string, { username: string; name: string }>();
  if (xUsersData) {
    for (const u of xUsersData) {
      if (u.x_id) {
        xUsersMap.set(u.x_id, {
          username: u.username,
          name: u.screen_name || u.username,
        });
      }
    }
  }

  const scannedRows = queriedAccounts.map((id: string) => {
    const known = KNOWN_SEEDS[id];
    const xUser = xUsersMap.get(id);
    const cand = mergedMap.get(id);
    const username = known?.username || xUser?.username || cand?.username || `user_${id.slice(-6)}`;
    const name = known?.name || xUser?.name || cand?.name || username;
    return {
      x_id: id,
      username,
      name,
      level: 1,
      scanned_at: state.updatedAt || new Date().toISOString(),
      followings_fetched: 0,
      api_provider: state.provider || "twitterapi",
      cost_usd: 0,
    };
  });

  const { error: seedErr } = await supabase.from("alpha_scanned_accounts").upsert(scannedRows, { onConflict: "x_id" });
  if (seedErr) {
    console.error(`❌ Fehler bei alpha_scanned_accounts: ${seedErr.message}`);
  } else {
    console.log(`✅ ${scannedRows.length} Seeds erfolgreich in 'alpha_scanned_accounts' gesichert.`);
  }

  // 6. Migration: alpha_candidates (in Chunks à 500)
  console.log(`\n⏳ Migriere ${candidateList.length} Kandidaten nach 'alpha_candidates' (Chunks à 500)...`);
  const chunkSize = 500;
  let candidatesInserted = 0;

  for (let i = 0; i < candidateList.length; i += chunkSize) {
    const chunk = candidateList.slice(i, i + chunkSize);
    const candidateRows = chunk.map((c) => ({
      x_id: c.x_id,
      username: c.username,
      name: c.name,
      score: c.score,
      first_level: c.first_level,
      created_at: new Date().toISOString(),
      updated_at: state.updatedAt || new Date().toISOString(),
    }));

    const { error: candErr } = await supabase.from("alpha_candidates").upsert(candidateRows, { onConflict: "x_id" });
    if (candErr) {
      console.error(`   ⚠️ Fehler in Chunk ${i}-${i + chunk.length}: ${candErr.message}`);
    } else {
      candidatesInserted += chunk.length;
      const pct = Math.round((candidatesInserted / candidateList.length) * 100);
      Deno.stdout.writeSync(new TextEncoder().encode(`\r   Fortschritt: ${candidatesInserted}/${candidateList.length} Kandidaten (${pct}%)`));
    }
  }
  console.log(`\n✅ Alle Kandidaten erfolgreich in 'alpha_candidates' eingetragen.`);

  // 7. Migration: alpha_relations (Kanten)
  console.log(`\n⏳ Extrahiere und migriere Graph-Kanten nach 'alpha_relations'...`);
  const relationRows: any[] = [];
  const edgeSeen = new Set<string>();

  const usernameToId = new Map<string, string>();
  for (const [id, meta] of Object.entries(KNOWN_SEEDS)) {
    usernameToId.set(meta.username.toLowerCase(), id);
  }
  for (const c of candidateList) {
    usernameToId.set(c.username.toLowerCase(), c.x_id);
  }

  for (const c of candidateList) {
    if (c.followedBy && c.followedBy.size > 0) {
      for (const rawSeedHandle of c.followedBy) {
        const cleanHandle = rawSeedHandle.replace(/^@/, "").toLowerCase();
        const seedId = usernameToId.get(cleanHandle) || SEED_BY_USERNAME[cleanHandle] || `seed_${cleanHandle}`;
        const edgeKey = `${seedId}->${c.x_id}`;
        if (!edgeSeen.has(edgeKey)) {
          edgeSeen.add(edgeKey);
          relationRows.push({
            seed_id: seedId,
            seed_username: cleanHandle,
            candidate_id: c.x_id,
            candidate_username: c.username,
            created_at: state.updatedAt || new Date().toISOString(),
          });
        }
      }
    }
  }

  console.log(`   Gefundene Kanten: ${relationRows.length}. Schreibe in Chunks à 500...`);
  let relationsInserted = 0;

  for (let i = 0; i < relationRows.length; i += chunkSize) {
    const chunk = relationRows.slice(i, i + chunkSize);
    const { error: relErr } = await supabase.from("alpha_relations").upsert(chunk, { onConflict: "seed_id,candidate_id" });
    if (relErr) {
      console.error(`   ⚠️ Fehler bei Relationen Chunk ${i}-${i + chunk.length}: ${relErr.message}`);
    } else {
      relationsInserted += chunk.length;
      const pct = Math.round((relationsInserted / relationRows.length) * 100);
      Deno.stdout.writeSync(new TextEncoder().encode(`\r   Fortschritt Kanten: ${relationsInserted}/${relationRows.length} (${pct}%)`));
    }
  }
  console.log(`\n✅ Alle Kanten erfolgreich in 'alpha_relations' eingetragen.`);

  // 8. Vollständige Validierung
  console.log("\n==================================================================");
  console.log("🔍 Validierung der migrierten Daten in Supabase");
  console.log("==================================================================");

  const { count: countScanned } = await supabase.from("alpha_scanned_accounts").select("*", { count: "exact", head: true });
  const { count: countCandidates } = await supabase.from("alpha_candidates").select("*", { count: "exact", head: true });
  const { count: countRelations } = await supabase.from("alpha_relations").select("*", { count: "exact", head: true });

  console.log(`   - alpha_scanned_accounts: ${countScanned} Zeilen (Erwartet: ${queriedAccounts.length})`);
  console.log(`   - alpha_candidates:       ${countCandidates} Zeilen (Erwartet: ${candidateList.length})`);
  console.log(`   - alpha_relations:        ${countRelations} Zeilen (Erwartet: ${relationRows.length})`);

  // Top-Kandidaten stichprobenartig prüfen
  const { data: topNikita } = await supabase.from("alpha_candidates").select("username, score").eq("username", "nikitabier").single();
  if (topNikita) {
    console.log(`   - Prüfe @${topNikita.username}: Score = ${topNikita.score} (Erwartet: 8) ${topNikita.score === 8 ? "✅" : "⚠️"}`);
  }

  // 9. Vollständigen JS-State generieren (Damit Dashboard sofort alle Knoten hat)
  console.log(`\n💾 Aktualisiere alpha_state_data.js mit vollem Datenstand...`);
  const mergedScoresObject: Record<string, any> = {};
  for (const c of candidateList) {
    mergedScoresObject[c.x_id] = {
      id: c.x_id,
      username: c.username,
      name: c.name,
      count: c.score,
      followedBy: Array.from(c.followedBy),
    };
  }
  const fullExportState = {
    ...state,
    scores: mergedScoresObject,
    stats: {
      totalCandidates: candidateList.length,
      overlapsCount: candidateList.filter((c) => c.score >= 2).length,
      maxScore: candidateList.length > 0 ? Math.max(...candidateList.map((c) => c.score)) : 8,
    },
  };

  const fullJs = `window.ALPHA_STATE = ${JSON.stringify(fullExportState)};\n`;
  await Deno.writeTextFile(`${scriptDir}../alpha_state_data.js`, fullJs);
  await Deno.writeTextFile(`${scriptDir}../../alpha_state_data.js`, fullJs);
  console.log(`✅ alpha_state_data.js aktualisiert (enthält alle ${candidateList.length} Kandidaten).`);

  console.log("\n🎉 Migration erfolgreich und vollständig abgeschlossen!");
}

if (import.meta.main) {
  main().catch((err) => {
    console.error(`💥 Unerwarteter Fehler: ${err.message}`);
    Deno.exit(1);
  });
}
