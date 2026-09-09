/**
 * Generator für den interaktiven 2D-Netzwerk-Graphen im Browser
 *
 * Liest den aktuellen Zustand aus alpha_state_*.json und erzeugt eine
 * hardwarebeschleunigte, eigenständige HTML-Visualisierung (alpha_graph.html)
 * mit Force-Directed Physics, Schieberegler-Filtern, Suche, kompakter Info-Box,
 * Datenbank-Statistiktabelle und NEUEM Gruppen-Modus (Mutual-Follow k-Core Cliquen
 * sowie Ko-Signal Schnittmengen).
 */

interface StateEntry {
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
  scores: Record<string, StateEntry>;
}

interface StateFile {
  mode: string;
  updatedAt: string;
  totalApiCalls?: number;
  totalProfilesFetched?: number;
  estimatedCostUsd?: number;
  queriedAccounts?: string[];
  levels?: Record<string, LevelData>;
  processed?: string[];
  scores?: Record<string, StateEntry>;
}

interface CliOptions {
  statePath: string;
  outPath: string;
  minScore: number;
  mode: "network" | "groups";
  groupLevel: number;
  noOpen: boolean;
  embedMin: number;
}

function parseArgs(args: string[]): CliOptions {
  let statePath = "";
  let outPath = "";
  let minScore = 2;
  let mode: "network" | "groups" = "network";
  let groupLevel = 2;
  let noOpen = false;
  let embedMin = 2;

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--file" && i + 1 < args.length) {
      statePath = args[++i];
    } else if (arg === "--out" && i + 1 < args.length) {
      outPath = args[++i];
    } else if (arg === "--min" && i + 1 < args.length) {
      minScore = parseInt(args[++i], 10);
    } else if (arg === "--mode" && i + 1 < args.length) {
      const m = args[++i].toLowerCase();
      if (m === "groups" || m === "group") mode = "groups";
      else mode = "network";
    } else if (arg === "--group-level" && i + 1 < args.length) {
      groupLevel = Math.max(1, parseInt(args[++i], 10) || 2);
    } else if (arg === "--embed-min" && i + 1 < args.length) {
      embedMin = Math.max(1, parseInt(args[++i], 10) || 1);
    } else if (arg === "--no-open") {
      noOpen = true;
    }
  }

  return { statePath, outPath, minScore, mode, groupLevel, noOpen, embedMin };
}

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

// 10 harmonische, kontrastreiche Farben für isolierte Cliquen/Netzwerke
const CLUSTER_PALETTES = [
  { background: "#f59e0b", border: "#fbbf24", highlight: "#fef08a", name: "Gold / Amber" },
  { background: "#06b6d4", border: "#22d3ee", highlight: "#cffafe", name: "Electric Cyan" },
  { background: "#ec4899", border: "#f472b6", highlight: "#fbcfe8", name: "Neon Pink" },
  { background: "#10b981", border: "#34d399", highlight: "#a7f3d0", name: "Emerald" },
  { background: "#8b5cf6", border: "#a78bfa", highlight: "#ddd6fe", name: "Purple Violet" },
  { background: "#f97316", border: "#fb923c", highlight: "#ffedd5", name: "Sunset Orange" },
  { background: "#3b82f6", border: "#60a5fa", highlight: "#dbeafe", name: "Deep Blue" },
  { background: "#14b8a6", border: "#2dd4bf", highlight: "#ccfbf1", name: "Teal" },
  { background: "#e11d48", border: "#f43f5e", highlight: "#ffe4e6", name: "Crimson" },
  { background: "#84cc16", border: "#a3e635", highlight: "#ecfccb", name: "Lime" },
];

interface PrecomputedCluster {
  id: string;
  name: string;
  level: number;
  color: { background: string; border: string; highlight: string; name: string };
  memberIds: string[];
  members: { id: string; username: string; name: string; score: number; internalDegree: number }[];
  hub: { id: string; username: string; name: string };
  internalEdgesCount: number;
  density: number;
}

/**
 * 1. Berechnet k-Core Cliquen für echte wechselseitige Follows (A folgt B UND B folgt A).
 */
function computeKCoreClusters(
  allNodes: { id: string; username: string; name: string; score: number; followedBy?: string[] }[],
  directedEdges: { from: string; to: string; fromUsername: string; toUsername: string }[],
  maxLevel = 4,
): Record<string, PrecomputedCluster[]> {
  const handleToNode = new Map<string, typeof allNodes[0]>();
  for (const n of allNodes) {
    handleToNode.set(n.username.toLowerCase(), n);
  }

  const followsMap = new Map<string, Set<string>>();
  for (const e of directedEdges) {
    const from = e.fromUsername.toLowerCase();
    const to = e.toUsername.toLowerCase();
    if (!followsMap.has(from)) followsMap.set(from, new Set());
    followsMap.get(from)!.add(to);
  }

  const mutualAdj = new Map<string, Set<string>>();
  for (const [from, toSet] of followsMap.entries()) {
    for (const to of toSet) {
      if (followsMap.has(to) && followsMap.get(to)!.has(from)) {
        if (!mutualAdj.has(from)) mutualAdj.set(from, new Set());
        mutualAdj.get(from)!.add(to);
        if (!mutualAdj.has(to)) mutualAdj.set(to, new Set());
        mutualAdj.get(to)!.add(from);
      }
    }
  }

  const result: Record<string, PrecomputedCluster[]> = {};

  for (let k = 1; k <= maxLevel; k++) {
    result[String(k)] = [];

    const currentDegrees = new Map<string, number>();
    const currentNeighbors = new Map<string, Set<string>>();
    const activeNodes = new Set<string>();

    for (const [node, neighbors] of mutualAdj.entries()) {
      activeNodes.add(node);
      currentNeighbors.set(node, new Set(neighbors));
      currentDegrees.set(node, neighbors.size);
    }

    let changed = true;
    while (changed) {
      changed = false;
      const toRemove: string[] = [];
      for (const node of activeNodes) {
        if ((currentDegrees.get(node) || 0) < k) {
          toRemove.push(node);
        }
      }

      if (toRemove.length > 0) {
        changed = true;
        for (const rem of toRemove) {
          activeNodes.delete(rem);
          const remNeighbors = currentNeighbors.get(rem) || new Set();
          for (const neighbor of remNeighbors) {
            if (activeNodes.has(neighbor)) {
              currentNeighbors.get(neighbor)?.delete(rem);
              currentDegrees.set(neighbor, (currentDegrees.get(neighbor) || 1) - 1);
            }
          }
        }
      }
    }

    if (activeNodes.size === 0) continue;

    const visited = new Set<string>();
    const components: string[][] = [];

    for (const node of activeNodes) {
      if (!visited.has(node)) {
        const comp: string[] = [];
        const queue = [node];
        visited.add(node);
        while (queue.length > 0) {
          const curr = queue.shift()!;
          comp.push(curr);
          for (const neighbor of currentNeighbors.get(curr) || []) {
            if (activeNodes.has(neighbor) && !visited.has(neighbor)) {
              visited.add(neighbor);
              queue.push(neighbor);
            }
          }
        }
        if (comp.length >= Math.max(2, k + 1)) {
          components.push(comp);
        }
      }
    }

    components.sort((a, b) => b.length - a.length);

    components.forEach((comp, idx) => {
      const palette = CLUSTER_PALETTES[idx % CLUSTER_PALETTES.length];
      const memberObjs: { id: string; username: string; name: string; score: number; internalDegree: number }[] = [];
      let maxInternalDegree = -1;
      let hubAccount = { id: "", username: comp[0], name: comp[0] };
      let internalEdgesCount = 0;

      for (const handle of comp) {
        const nodeObj = handleToNode.get(handle);
        const internalDegree = Array.from(currentNeighbors.get(handle) || []).filter((n) => comp.includes(n)).length;
        internalEdgesCount += internalDegree;

        const info = {
          id: nodeObj?.id || `user_${handle}`,
          username: handle,
          name: nodeObj?.name || `@${handle}`,
          score: nodeObj?.score || 1,
          internalDegree,
        };
        memberObjs.push(info);

        if (internalDegree > maxInternalDegree) {
          maxInternalDegree = internalDegree;
          hubAccount = { id: info.id, username: info.username, name: info.name };
        }
      }

      internalEdgesCount = Math.floor(internalEdgesCount / 2);
      const n = comp.length;
      const maxPossibleEdges = (n * (n - 1)) / 2;
      const density = maxPossibleEdges > 0 ? Math.round((internalEdgesCount / maxPossibleEdges) * 100) : 100;

      result[String(k)].push({
        id: `cluster_mutual_lvl${k}_${idx + 1}`,
        name: `Wechselseitige Clique #${idx + 1} (${comp.length} Trader)`,
        level: k,
        color: palette,
        memberIds: memberObjs.map((m) => m.id),
        members: memberObjs.sort((a, b) => b.internalDegree - a.internalDegree),
        hub: hubAccount,
        internalEdgesCount,
        density,
      });
    });
  }

  return result;
}

/**
 * 2. Berechnet Ko-Signal Cliquen (Trader, die dieselben N Influencer teilen).
 * Ermöglicht sofortige Cliquen-Analyse auf bestehenden Datensätzen, in denen
 * Kandidaten noch keine Rückbezüge haben.
 */
function computeCoSignalClusters(
  candidates: { id: string; username: string; name: string; score: number; followedBy?: string[] }[],
  maxLevel = 4,
): Record<string, PrecomputedCluster[]> {
  const result: Record<string, PrecomputedCluster[]> = {};

  for (let k = 2; k <= maxLevel; k++) {
    result[String(k)] = [];
    const qualifying = candidates.filter((c) => (c.followedBy?.length || c.score) >= k);

    // Gruppierung nach exakter oder starker Schnittmenge der Follower
    const signatureMap = new Map<string, typeof qualifying>();

    for (const c of qualifying) {
      if (!c.followedBy || c.followedBy.length < k) continue;
      const sortedFollowers = [...c.followedBy].map((h) => h.replace("@", "").toLowerCase()).sort();
      // Signatur aus den Top-K gemeinsamen Tradern
      const sig = sortedFollowers.slice(0, k).join("+");
      if (!signatureMap.has(sig)) signatureMap.set(sig, []);
      signatureMap.get(sig)!.push(c);
    }

    // Sortiere nach Clustergröße
    const validSignatures = Array.from(signatureMap.entries())
      .filter(([_, list]) => list.length >= 2)
      .sort((a, b) => b[1].length - a[1].length)
      .slice(0, 15); // Top 15 Cluster pro Level

    validSignatures.forEach(([sig, list], idx) => {
      const palette = CLUSTER_PALETTES[idx % CLUSTER_PALETTES.length];
      const sharedTraders = sig.split("+").map((s) => "@" + s).join(", ");
      const members = list.map((c) => ({
        id: c.id,
        username: c.username,
        name: c.name,
        score: c.score,
        internalDegree: c.score,
      })).sort((a, b) => b.score - a.score);

      const topHub = members[0];

      result[String(k)].push({
        id: `cluster_cosignal_lvl${k}_${idx + 1}`,
        name: `Ko-Signal #${idx + 1} (${list.length} Trader via ${sharedTraders})`,
        level: k,
        color: palette,
        memberIds: members.map((m) => m.id),
        members,
        hub: { id: topHub.id, username: topHub.username, name: topHub.name },
        internalEdgesCount: (members.length * (members.length - 1)) / 2,
        density: 100,
      });
    });
  }

  return result;
}

async function main() {
  const options = parseArgs(Deno.args);

  const scriptDir = new URL(".", import.meta.url).pathname;
  const defaultStatePath = `${scriptDir}../alpha_state_supabase_registered.json`;
  const stateFilePath = options.statePath || defaultStatePath;
  const htmlOutputPath = options.outPath || `${scriptDir}../../../alpha_graph.html`;
  const secondaryOutputPath = `${scriptDir}../../alpha_graph.html`;

  console.log("=========================================================================");
  console.log("             🎨 Alpha-Influencer Netzwerk-Visualisierer 🎨               ");
  console.log("=========================================================================\n");

  let raw = "";
  try {
    raw = await Deno.readTextFile(stateFilePath);
  } catch (_e) {
    console.error(`❌ Konnte State-Datei nicht öffnen: ${stateFilePath}`);
    Deno.exit(1);
  }

  const state: StateFile = JSON.parse(raw);
  console.log(`📁 State geladen: ${stateFilePath}`);

  const allScoresMap: Record<string, StateEntry> = {};
  if (state.levels && Object.keys(state.levels).length > 0) {
    for (const lvl of Object.values(state.levels)) {
      if (lvl.scores) {
        for (const [id, entry] of Object.entries(lvl.scores)) {
          if (!allScoresMap[id] || allScoresMap[id].count < entry.count) {
            allScoresMap[id] = entry;
          }
        }
      }
    }
  } else if (state.scores) {
    Object.assign(allScoresMap, state.scores);
  }

  const totalCandidatePoolCount = Object.keys(allScoresMap).length;
  // Nach embedMin filtern (Standard: Score >= 2; reduziert HTML von ~28 MB auf ~2.5 MB)
  const candidateEntries = Object.values(allScoresMap).filter(
    (c) => c.count >= options.embedMin
  );
  console.log(`🎯 Gesamt-Kandidaten im Pool: ${totalCandidatePoolCount.toLocaleString("de-DE")}`);
  console.log(`⚡ Eingebettete Kandidaten (Score >= ${options.embedMin}): ${candidateEntries.length.toLocaleString("de-DE")}`);

  const seedIds = state.processed || [];
  const baseSeedNodes: any[] = [];
  const seedNodeByHandle = new Map<string, any>();
  const seedNodeById = new Map<string, any>();

  for (const id of seedIds) {
    const fromScores = allScoresMap[id];
    const meta = KNOWN_SEEDS[id] || (fromScores ? { username: fromScores.username, name: fromScores.name } : { username: `user_${id}`, name: `Influencer ${id.slice(-4)}` });
    const sNode = {
      id: `seed_${id}`,
      originalId: id,
      label: `@${meta.username}`,
      title: `${meta.name} (@${meta.username})\n[Basis-Influencer]`,
      group: "seed",
      username: meta.username,
      name: meta.name,
      isSeed: true,
      score: 11,
    };
    baseSeedNodes.push(sNode);
    seedNodeById.set(id, sNode);
    seedNodeByHandle.set(meta.username.toLowerCase(), sNode);
  }

  // Sicherstellen, dass alle in followedBy genannten Influencer als Basis-Nodes existieren
  for (const c of candidateEntries) {
    if (c.followedBy && c.followedBy.length > 0) {
      for (const fHandle of c.followedBy) {
        const cleanHandle = fHandle.replace("@", "").toLowerCase();
        if (!seedNodeByHandle.has(cleanHandle)) {
          const fromScores = Object.values(allScoresMap).find(x => x.username.toLowerCase() === cleanHandle);
          const sNode = {
            id: `seed_${cleanHandle}`,
            originalId: fromScores ? fromScores.id : cleanHandle,
            label: `@${fromScores ? fromScores.username : cleanHandle}`,
            title: `${fromScores ? fromScores.name : cleanHandle} (@${cleanHandle})\n[Basis-Influencer / Seed]`,
            group: "seed",
            username: fromScores ? fromScores.username : cleanHandle,
            name: fromScores ? fromScores.name : cleanHandle,
            isSeed: true,
            score: 11,
          };
          baseSeedNodes.push(sNode);
          seedNodeByHandle.set(cleanHandle, sNode);
        }
      }
    }
  }

  const candidateNodes: any[] = [];
  const candidateEdges: any[] = [];
  const directedEdgeTuples: { from: string; to: string; fromUsername: string; toUsername: string }[] = [];

  for (const c of candidateEntries) {
    candidateNodes.push({
      id: c.id,
      label: `@${c.username}`,
      title: `${c.name} (@${c.username})\nGefolgt von: ${c.count} Influencer(n)`,
      group: c.count >= 6 ? "tier1" : c.count >= 4 ? "tier2" : c.count >= 2 ? "tier3" : "tier4",
      username: c.username,
      name: c.name,
      isSeed: false,
      score: c.count,
      followedBy: c.followedBy || [],
    });

    if (c.followedBy && c.followedBy.length > 0) {
      for (const fHandle of c.followedBy) {
        const cleanHandle = fHandle.replace("@", "").toLowerCase();
        const foundSeed = seedNodeByHandle.get(cleanHandle);
        if (foundSeed) {
          candidateEdges.push({
            from: foundSeed.id,
            to: c.id,
            arrows: "to",
          });
          directedEdgeTuples.push({
            from: foundSeed.id,
            to: c.id,
            fromUsername: cleanHandle,
            toUsername: c.username.toLowerCase(),
          });
        }
      }
    }
  }

  // Vorberechnung:
  // 1. Echte Mutual Follows (A <-> B)
  console.log("⚙️  Berechne wechselseitige Follows & k-Core Cliquen (Grouplevel 1 bis 4)...");
  const allNodesCombined = [...baseSeedNodes, ...candidateNodes];
  const mutualGroupLevels = computeKCoreClusters(allNodesCombined, directedEdgeTuples, 4);

  // 2. Ko-Signal Cliquen (Schnittmengen gleicher Influencer)
  console.log("⚙️  Berechne Ko-Signal Cliquen (gemeinsame Influencer)...");
  const coSignalGroupLevels = computeCoSignalClusters(candidateNodes, 4);

  let totalMutual = 0;
  for (const [lvl, clusters] of Object.entries(mutualGroupLevels)) {
    console.log(`   - Mutual Lvl ${lvl}: ${clusters.length} Clique(n)`);
    totalMutual += clusters.length;
  }
  let totalCoSignal = 0;
  for (const [lvl, clusters] of Object.entries(coSignalGroupLevels)) {
    console.log(`   - Ko-Signal Lvl ${lvl}: ${clusters.length} Schnittmengen-Netzwerk(e)`);
    totalCoSignal += clusters.length;
  }

  const maxCandidateScore = Math.max(...candidateNodes.map((c) => c.score), 8);
  const defaultMinScore = options.minScore > 2 ? options.minScore : maxCandidateScore;

  const payload = {
    generatedAt: new Date().toISOString(),
    stateUpdatedAt: state.updatedAt,
    baseSeeds: baseSeedNodes,
    candidates: candidateNodes,
    edges: candidateEdges,
    groupLevels: mutualGroupLevels,
    coSignalLevels: coSignalGroupLevels,
    initialMode: options.mode,
    initialGroupLevel: options.groupLevel,
    minScoreDefault: defaultMinScore,
    maxScore: maxCandidateScore,
    embedMin: options.embedMin,
    totalCandidatePoolCount,
    totalApiCalls: state.totalApiCalls || 0,
    totalProfilesFetched: state.totalProfilesFetched || 0,
    estimatedCostUsd: state.estimatedCostUsd || 0,
    queriedAccountsCount: (state.queriedAccounts && state.queriedAccounts.length) || (state.processed && state.processed.length) || 0,
  };

  console.log(`✅ Vorberechnung abgeschlossen (${totalMutual} wechselseitige Cliquen, ${totalCoSignal} Ko-Signal Netzwerke).`);

  // Standalone HTML Content
  const htmlContent = `<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Alpha-Influencer Network & Cliquen Explorer</title>
  <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-base: #090d16;
      --bg-surface: rgba(15, 23, 42, 0.92);
      --border-color: rgba(255, 255, 255, 0.09);
      --border-highlight: rgba(255, 255, 255, 0.2);
      --accent-gold: #f59e0b;
      --accent-pink: #ec4899;
      --accent-purple: #8b5cf6;
      --accent-cyan: #06b6d4;
      --accent-emerald: #10b981;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: 'Inter', sans-serif;
    }

    body {
      background-color: var(--bg-base);
      color: var(--text-main);
      overflow: hidden;
      width: 100vw;
      height: 100vh;
      display: flex;
    }

    #network-container {
      flex: 1;
      height: 100%;
      background: radial-gradient(circle at center, #111827 0%, var(--bg-base) 100%);
    }

    .hud-panel {
      position: absolute;
      top: 16px;
      left: 16px;
      width: 330px;
      background: var(--bg-surface);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border: 1px solid var(--border-color);
      border-radius: 14px;
      padding: 16px;
      box-shadow: 0 16px 36px rgba(0, 0, 0, 0.6);
      z-index: 100;
      display: flex;
      flex-direction: column;
      gap: 12px;
      max-height: calc(100vh - 32px);
      overflow-y: auto;
    }

    .hud-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid var(--border-color);
      padding-bottom: 10px;
    }

    .hud-title {
      font-size: 15px;
      font-weight: 700;
      background: linear-gradient(135deg, #f59e0b, #ec4899);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .stat-badge {
      font-size: 11px;
      background: rgba(255, 255, 255, 0.08);
      padding: 2px 7px;
      border-radius: 999px;
      color: var(--text-muted);
      border: 1px solid rgba(255, 255, 255, 0.05);
    }

    .view-mode-tabs {
      display: flex;
      gap: 4px;
      background: rgba(0, 0, 0, 0.45);
      padding: 3px;
      border-radius: 10px;
      border: 1px solid var(--border-color);
    }

    .view-mode-btn {
      flex: 1;
      padding: 7px 10px;
      font-size: 11px;
      font-weight: 600;
      border-radius: 7px;
      border: none;
      background: transparent;
      color: var(--text-muted);
      cursor: pointer;
      transition: all 0.2s;
      text-align: center;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
    }

    .view-mode-btn:hover {
      color: var(--text-main);
    }

    .view-mode-btn.active {
      background: linear-gradient(135deg, rgba(245, 158, 11, 0.35), rgba(236, 72, 153, 0.35));
      border: 1px solid rgba(245, 158, 11, 0.6);
      color: #ffffff;
      font-weight: 700;
      box-shadow: 0 2px 10px rgba(245, 158, 11, 0.2);
    }

    .form-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .form-label {
      font-size: 11px;
      font-weight: 600;
      color: var(--text-muted);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .search-input {
      width: 100%;
      padding: 8px 12px;
      background: rgba(30, 41, 59, 0.7);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      color: var(--text-main);
      font-size: 12px;
      outline: none;
      transition: all 0.2s;
    }

    .search-input:focus {
      border-color: var(--accent-cyan);
      box-shadow: 0 0 0 2px rgba(6, 182, 212, 0.2);
    }

    .hud-select {
      width: 100%;
      padding: 7px 10px;
      background: rgba(30, 41, 59, 0.9);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      color: var(--text-main);
      font-size: 11px;
      outline: none;
      cursor: pointer;
    }

    .hud-select:focus {
      border-color: var(--accent-cyan);
    }

    .grouplevel-btns {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 4px;
      background: rgba(0, 0, 0, 0.35);
      padding: 3px;
      border-radius: 8px;
      border: 1px solid var(--border-color);
    }

    .grouplevel-btn {
      padding: 6px 2px;
      font-size: 10px;
      font-weight: 600;
      border-radius: 6px;
      border: 1px solid transparent;
      background: transparent;
      color: var(--text-muted);
      cursor: pointer;
      transition: all 0.2s;
      text-align: center;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 2px;
    }

    .grouplevel-btn:hover {
      color: var(--text-main);
      background: rgba(255, 255, 255, 0.05);
    }

    .grouplevel-btn.active {
      background: linear-gradient(135deg, rgba(6, 182, 212, 0.35), rgba(139, 92, 246, 0.35));
      border-color: var(--accent-cyan);
      color: #ffffff;
      font-weight: 700;
      box-shadow: 0 2px 8px rgba(6, 182, 212, 0.25);
    }

    .grouplevel-btn .badge {
      font-size: 9px;
      padding: 1px 4px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.1);
      color: var(--text-muted);
    }

    .grouplevel-btn.active .badge {
      background: var(--accent-cyan);
      color: #090d16;
      font-weight: 700;
    }

    .clique-type-toggle {
      display: flex;
      gap: 4px;
      background: rgba(0, 0, 0, 0.3);
      padding: 2px;
      border-radius: 8px;
      border: 1px solid var(--border-color);
    }

    .clique-type-btn {
      flex: 1;
      padding: 4px 6px;
      font-size: 9.5px;
      font-weight: 600;
      border-radius: 6px;
      border: none;
      background: transparent;
      color: var(--text-muted);
      cursor: pointer;
      text-align: center;
      transition: all 0.15s;
    }

    .clique-type-btn.active {
      background: rgba(255, 255, 255, 0.1);
      color: var(--text-main);
      font-weight: 700;
    }

    .perspective-btns {
      display: flex;
      gap: 4px;
      background: rgba(0, 0, 0, 0.35);
      padding: 3px;
      border-radius: 9px;
      border: 1px solid var(--border-color);
    }

    .persp-btn {
      flex: 1;
      padding: 5px 6px;
      font-size: 10px;
      font-weight: 600;
      border-radius: 6px;
      border: none;
      background: transparent;
      color: var(--text-muted);
      cursor: pointer;
      transition: all 0.2s;
      white-space: nowrap;
      text-align: center;
    }

    .persp-btn:hover {
      color: var(--text-main);
    }

    .persp-btn.active {
      background: linear-gradient(135deg, rgba(245, 158, 11, 0.3), rgba(236, 72, 153, 0.3));
      border: 1px solid rgba(245, 158, 11, 0.5);
      color: #fff;
      font-weight: 700;
    }

    .range-slider {
      width: 100%;
      accent-color: var(--accent-pink);
      cursor: pointer;
    }

    .btn-group {
      display: flex;
      gap: 6px;
    }

    .hud-btn {
      flex: 1;
      padding: 6px 10px;
      font-size: 11px;
      font-weight: 600;
      border-radius: 7px;
      border: 1px solid var(--border-color);
      background: rgba(30, 41, 59, 0.8);
      color: var(--text-main);
      cursor: pointer;
      transition: all 0.2s;
      text-align: center;
    }

    .hud-btn:hover {
      background: rgba(51, 65, 85, 0.9);
      border-color: rgba(255, 255, 255, 0.2);
    }

    .db-stats-panel {
      position: absolute;
      bottom: 16px;
      left: 16px;
      width: 330px;
      background: var(--bg-surface);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border: 1px solid var(--border-color);
      border-radius: 14px;
      padding: 14px 16px;
      box-shadow: 0 16px 36px rgba(0, 0, 0, 0.6);
      z-index: 100;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .db-stats-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid var(--border-color);
      padding-bottom: 8px;
    }

    .db-stats-title {
      font-size: 12px;
      font-weight: 700;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .pulse-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--accent-emerald);
      box-shadow: 0 0 8px var(--accent-emerald);
    }

    .stats-tabs {
      display: flex;
      gap: 4px;
      border-bottom: 1px solid var(--border-color);
      padding-bottom: 6px;
      margin-bottom: 6px;
    }

    .stats-tab-btn {
      flex: 1;
      padding: 4px 6px;
      font-size: 10px;
      font-weight: 600;
      border-radius: 6px;
      border: 1px solid transparent;
      background: transparent;
      color: var(--text-muted);
      cursor: pointer;
      text-align: center;
      transition: all 0.15s;
    }

    .stats-tab-btn.active {
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.12);
      color: var(--accent-cyan);
    }

    .stats-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 11px;
    }

    .stats-table tr {
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    }

    .stats-table tr:last-child {
      border-bottom: none;
    }

    .stats-table td {
      padding: 4px 0;
      color: var(--text-muted);
    }

    .stats-table td.val {
      text-align: right;
      font-weight: 600;
      color: var(--text-main);
      font-variant-numeric: tabular-nums;
    }

    .val-highlight {
      color: var(--accent-cyan) !important;
    }

    .val-cost {
      color: var(--accent-emerald) !important;
    }

    .inspector-panel {
      position: absolute;
      top: 16px;
      right: 16px;
      width: 290px;
      background: rgba(15, 23, 42, 0.95);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      border: 1px solid var(--border-color);
      border-radius: 14px;
      padding: 16px;
      box-shadow: 0 16px 36px rgba(0, 0, 0, 0.7);
      z-index: 100;
      display: none;
      flex-direction: column;
      gap: 12px;
      animation: slideIn 0.18s ease-out;
      max-height: calc(100vh - 32px);
      overflow-y: auto;
    }

    @keyframes slideIn {
      from { transform: translateX(16px); opacity: 0; }
      to { transform: translateX(0); opacity: 1; }
    }

    .inspector-header {
      display: flex;
      align-items: center;
      gap: 10px;
      position: relative;
    }

    .close-btn {
      position: absolute;
      top: -2px;
      right: -2px;
      background: rgba(255, 255, 255, 0.06);
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      width: 22px;
      height: 22px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 11px;
      transition: all 0.2s;
    }

    .close-btn:hover {
      background: rgba(255, 255, 255, 0.15);
      color: white;
    }

    .inspector-avatar {
      width: 40px;
      height: 40px;
      border-radius: 50%;
      background: linear-gradient(135deg, var(--accent-purple), var(--accent-cyan));
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 16px;
      font-weight: 700;
      color: white;
      flex-shrink: 0;
      box-shadow: 0 0 12px rgba(6, 182, 212, 0.3);
    }

    .inspector-title-area {
      overflow: hidden;
      padding-right: 20px;
    }

    .inspector-name {
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: var(--text-main);
    }

    .inspector-handle {
      color: var(--accent-cyan);
      font-size: 11px;
      font-weight: 500;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .info-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      background: rgba(0, 0, 0, 0.3);
      border-radius: 8px;
      padding: 8px;
      border: 1px solid rgba(255, 255, 255, 0.05);
    }

    .info-cell {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .info-cell-label {
      font-size: 9px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
      font-weight: 600;
    }

    .info-cell-value {
      font-size: 12px;
      font-weight: 700;
      color: var(--text-main);
    }

    .cluster-card {
      background: rgba(0, 0, 0, 0.35);
      border: 1px solid rgba(245, 158, 11, 0.3);
      border-radius: 10px;
      padding: 10px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .cluster-card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid rgba(255, 255, 255, 0.07);
      padding-bottom: 6px;
    }

    .cluster-tag {
      font-size: 10px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }

    .followers-section {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .followers-title {
      font-size: 10px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .followers-pills {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      max-height: 110px;
      overflow-y: auto;
    }

    .follower-pill {
      font-size: 10px;
      background: rgba(255, 255, 255, 0.07);
      border: 1px solid rgba(255, 255, 255, 0.05);
      padding: 2px 6px;
      border-radius: 6px;
      color: var(--text-main);
      cursor: pointer;
      transition: all 0.15s;
    }

    .follower-pill:hover {
      background: rgba(6, 182, 212, 0.2);
      border-color: var(--accent-cyan);
    }

    .action-link {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      width: 100%;
      padding: 8px;
      background: linear-gradient(135deg, #1d4ed8, #2563eb);
      color: white;
      text-decoration: none;
      border-radius: 8px;
      font-size: 11px;
      font-weight: 600;
      transition: opacity 0.2s;
    }

    .action-link:hover {
      opacity: 0.9;
    }

    .empty-state {
      padding: 16px 12px;
      text-align: center;
      color: var(--text-muted);
      font-size: 11px;
      line-height: 1.5;
      background: rgba(0, 0, 0, 0.2);
      border-radius: 8px;
      border: 1px dashed rgba(255, 255, 255, 0.1);
    }
  </style>
</head>
<body>

  <!-- Controls Panel (Oben Links) -->
  <div class="hud-panel">
    <div class="hud-header">
      <div class="hud-title">
        <span>⚡ Alpha Graph Explorer</span>
      </div>
      <div style="display: flex; gap: 6px; align-items: center;">
        <span class="stat-badge" id="source-badge" style="color: var(--accent-emerald); border-color: rgba(16, 185, 129, 0.3); background: rgba(16, 185, 129, 0.15);">🟢 State JSON</span>
        <span class="stat-badge" id="node-count-badge">0 Knoten</span>
      </div>
    </div>

    <!-- Modus-Umschalter: Netzwerk vs Cliquen -->
    <div class="view-mode-tabs">
      <button class="view-mode-btn ${payload.initialMode === "network" ? "active" : ""}" id="btn-mode-network">🌐 Netzwerk</button>
      <button class="view-mode-btn ${payload.initialMode === "groups" ? "active" : ""}" id="btn-mode-groups">👥 Cliquen & Gruppen</button>
    </div>

    <!-- Live Suche -->
    <div class="form-group">
      <input type="text" id="search-box" class="search-input" placeholder="🔍 Account suchen (z.B. @sama)...">
    </div>

    <!-- CONTROLS: NETZWERK-MODUS -->
    <div id="controls-network" style="display: ${payload.initialMode === "network" ? "flex" : "none"}; flex-direction: column; gap: 12px;">
      <div class="form-group">
        <div class="form-label">
          <span>Perspektive</span>
          <span id="perspective-badge" style="color: var(--accent-gold); font-weight: 700;">🌟 Alpha-Zentrum</span>
        </div>
        <div class="perspective-btns">
          <button class="persp-btn active" data-persp="alpha" title="Top-Alpha Influencer mit den meisten Followern im Mittelpunkt">🌟 Alpha</button>
          <button class="persp-btn" data-persp="seeds" title="Basis-Trader im Mittelpunkt">🌱 Seeds</button>
          <button class="persp-btn" data-persp="radial" title="Konzentrische Orbits nach Signal-Stärke">🪐 Orbits</button>
        </div>
      </div>

      <div class="form-group">
        <div class="form-label">
          <span>Mindest-Score</span>
          <span id="score-val" style="color: var(--accent-pink);">${payload.minScoreDefault}+ Stimmen</span>
        </div>
        <input type="range" id="min-score-slider" class="range-slider" min="${payload.embedMin}" max="${payload.maxScore}" value="${payload.minScoreDefault}">
      </div>

      <!-- Grafik-Qualität Slider (Nur in Netzwerkansicht) -->
      <div class="form-group" style="padding-top: 4px; border-top: 1px solid rgba(255, 255, 255, 0.06);">
        <div class="form-label" style="font-size: 10px;">
          <span>Grafik-Qualität</span>
          <span id="quality-val" style="color: var(--accent-cyan); font-weight: 600;">⚖️ Ausgewogen</span>
        </div>
        <input type="range" id="quality-slider" class="range-slider" min="1" max="3" value="2" style="height: 4px; accent-color: var(--accent-cyan);">
        <div id="quality-hint" style="font-size: 9px; color: var(--text-muted); line-height: 1.25; margin-top: 2px;">
          Schatten auf Basis-Tradern, 60 Iterationen – flüssig &amp; schick.
        </div>
      </div>

      <!-- Buttons (Nur in Netzwerkansicht) -->
      <div class="btn-group">
        <button class="hud-btn" id="freeze-btn">⏸️ Pause</button>
        <button class="hud-btn" id="reset-btn">🔄 Reset</button>
      </div>
    </div>

    <!-- CONTROLS: CLIQUE- & GRUPPEN-MODUS -->
    <div id="controls-groups" style="display: ${payload.initialMode === "groups" ? "flex" : "none"}; flex-direction: column; gap: 12px;">
      <!-- Cliquen-Typ Auswahl (Wechselseitig vs Ko-Signal) -->
      <div class="clique-type-toggle">
        <button class="clique-type-btn active" id="clique-type-mutual" title="Echte wechselseitige Follows (A <-> B)">🔄 Wechselseitig (Mutual)</button>
        <button class="clique-type-btn" id="clique-type-cosignal" title="Trader mit Schnittmengen derselben Influencer">🔗 Ko-Signal (Schnittmenge)</button>
      </div>

      <!-- High-Performance Topologie & Limit für Ko-Signal (Optionen vom User gewünscht) -->
      <div class="form-group" id="topology-toggle-group" style="display: none; padding-top: 2px;">
        <label style="display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--text-main); cursor: pointer;" title="Verbindet Trader mit dem Alpha-Hub des Clusters statt O(n²) Vollvernetzung">
          <input type="checkbox" id="hub-topology-toggle" checked style="accent-color: var(--accent-cyan); cursor: pointer;">
          <span style="font-weight: 600;">⚡ Stern-/Hub-Topologie (60 FPS)</span>
        </label>
        <div style="font-size: 9px; color: var(--text-muted); line-height: 1.25; margin-left: 20px; margin-top: 2px;">
          Reduziert Kanten um 99,7% für flüssiges Rendering.
        </div>
      </div>

      <div class="form-group" id="cluster-limit-group" style="display: none;">
        <div class="form-label">
          <span>Max. Trader pro Gruppe</span>
          <span id="cluster-limit-val" style="color: var(--accent-gold); font-weight: 600;">Top 35</span>
        </div>
        <input type="range" id="cluster-limit-slider" class="range-slider" min="10" max="150" step="5" value="35" style="accent-color: var(--accent-gold);">
        <div style="font-size: 9px; color: var(--text-muted); line-height: 1.25; margin-top: 2px;">
          Rendert die stärksten Signal-Trader der Gruppe (alle im Inspector).
        </div>
      </div>

      <div class="form-group">
        <div class="form-label">
          <span>Grouplevel (Mindest-Verbindungen)</span>
          <span id="grouplevel-badge" style="color: var(--accent-cyan); font-weight: 700;">Stufe ${payload.initialGroupLevel}</span>
        </div>
        <div class="grouplevel-btns">
          <button class="grouplevel-btn ${payload.initialGroupLevel === 1 ? "active" : ""}" data-level="1">
            <span>Lvl 1</span>
            <span class="badge" id="badge-lvl-1">0</span>
          </button>
          <button class="grouplevel-btn ${payload.initialGroupLevel === 2 ? "active" : ""}" data-level="2">
            <span>Lvl 2</span>
            <span class="badge" id="badge-lvl-2">0</span>
          </button>
          <button class="grouplevel-btn ${payload.initialGroupLevel === 3 ? "active" : ""}" data-level="3">
            <span>Lvl 3</span>
            <span class="badge" id="badge-lvl-3">0</span>
          </button>
          <button class="grouplevel-btn ${payload.initialGroupLevel >= 4 ? "active" : ""}" data-level="4">
            <span>Lvl 4+</span>
            <span class="badge" id="badge-lvl-4">0</span>
          </button>
        </div>
        <div id="grouplevel-hint" style="font-size: 9px; color: var(--text-muted); line-height: 1.35; margin-top: 2px;">
          Jeder Teilnehmer folgt mindestens 2 Mitgliedern der Gruppe gegenseitig (geschlossene Kreise).
        </div>
      </div>

      <div class="form-group">
        <div class="form-label">
          <span>Isolierte Cliquen dieser Stufe</span>
          <span id="cluster-count-badge" style="color: var(--accent-gold); font-weight: 700;">0 Netzwerke</span>
        </div>
        <select id="cluster-select" class="hud-select">
          <option value="all">🌟 Alle Cliquen dieser Stufe anzeigen</option>
        </select>
      </div>

      <div class="form-group" style="padding-top: 2px;">
        <label style="display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--text-muted); cursor: pointer;">
          <input type="checkbox" id="hide-inactives-toggle" checked style="accent-color: var(--accent-cyan); cursor: pointer;">
          <span>Inaktive Profile vollständig ausblenden</span>
        </label>
      </div>

      <button class="hud-btn" id="isolate-cluster-btn" style="display: none; background: rgba(6, 182, 212, 0.2); border-color: var(--accent-cyan); margin-top: 4px;">🎯 Gruppe fokussieren</button>
    </div>
  </div>

  <!-- Datenbank-Statistik & Tabs (Unten Links) -->
  <div class="db-stats-panel">
    <div class="db-stats-header">
      <div class="db-stats-title">
        <span class="pulse-dot"></span>
        <span>Signal- & Cliquen-Status</span>
      </div>
      <span class="stat-badge" id="db-mode-badge">${state.mode || "JSON State"}</span>
    </div>

    <div class="stats-tabs">
      <button class="stats-tab-btn active" id="tab-btn-overview">📋 Metriken</button>
      <button class="stats-tab-btn" id="tab-btn-dist">📊 Follower</button>
      <button class="stats-tab-btn" id="tab-btn-clusters">👥 Cliquen</button>
    </div>

    <!-- Tab 1: Allgemeine Metriken -->
    <div id="tab-content-overview">
      <table class="stats-table">
        <tbody>
          <tr>
            <td>Abgefragte Accounts (Seeds)</td>
            <td class="val">${payload.queriedAccountsCount}</td>
          </tr>
          <tr>
            <td>Erfasste Profile</td>
            <td class="val">${payload.totalProfilesFetched.toLocaleString("de-DE")}</td>
          </tr>
          <tr>
            <td>Alpha-Kandidaten (Gesamt-Pool)</td>
            <td class="val">${payload.totalCandidatePoolCount.toLocaleString("de-DE")}</td>
          </tr>
          <tr>
            <td>Im Graphen geladen (≥ ${payload.embedMin} Stimmen)</td>
            <td class="val val-highlight">${payload.candidates.length.toLocaleString("de-DE")}</td>
          </tr>
          <tr>
            <td>Aktive Cliquen (Stufe ${payload.initialGroupLevel})</td>
            <td class="val val-highlight" id="stat-active-clusters-count">0 Gruppen</td>
          </tr>
          <tr>
            <td>Stand der Daten</td>
            <td class="val" style="font-size: 10px;">${new Date(payload.stateUpdatedAt).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Tab 2: Follower-Verteilung -->
    <div id="tab-content-dist" style="display: none; max-height: 160px; overflow-y: auto;">
      <table class="stats-table" id="dist-table">
        <tbody>
          ${(() => {
            const scoreCounts = new Map<number, number>();
            for (const c of payload.candidates) {
              scoreCounts.set(c.score, (scoreCounts.get(c.score) || 0) + 1);
            }
            const sortedScores = Array.from(scoreCounts.keys()).sort((a, b) => b - a);
            return sortedScores
              .map((sc) => {
                const cnt = scoreCounts.get(sc) || 0;
                return `<tr><td>${sc} Stimmen</td><td class="val">${cnt.toLocaleString("de-DE")}</td></tr>`;
              })
              .join("");
          })()}
        </tbody>
      </table>
    </div>

    <!-- Tab 3: Cliquen-Verteilung nach Level -->
    <div id="tab-content-clusters" style="display: none; max-height: 160px; overflow-y: auto;">
      <table class="stats-table">
        <tbody id="clusters-breakdown-body">
          <tr><td>Level 1 (≥ 1 Verbindung)</td><td class="val" id="stat-lvl-1">0 Gruppen</td></tr>
          <tr><td>Level 2 (≥ 2 Verbindungen)</td><td class="val val-highlight" id="stat-lvl-2">0 Gruppen</td></tr>
          <tr><td>Level 3 (≥ 3 Verbindungen)</td><td class="val val-highlight" id="stat-lvl-3">0 Gruppen</td></tr>
          <tr><td>Level 4+ (≥ 4 Verbindungen)</td><td class="val" id="stat-lvl-4">0 Gruppen</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Inspector-Panel (Rechts) -->
  <div class="inspector-panel" id="inspector">
    <div class="inspector-header">
      <div class="inspector-avatar" id="inspector-avatar">@</div>
      <div class="inspector-title-area">
        <div class="inspector-name" id="inspector-name">Name</div>
        <div class="inspector-handle" id="inspector-handle">@handle</div>
      </div>
      <button class="close-btn" id="close-inspector">✕</button>
    </div>

    <div class="info-grid">
      <div class="info-cell">
        <span class="info-cell-label">Signal-Score</span>
        <span class="info-cell-value" id="inspector-score" style="color: var(--accent-pink);">-</span>
      </div>
      <div class="info-cell">
        <span class="info-cell-label">Kategorie</span>
        <span class="info-cell-value" id="inspector-type" style="color: var(--accent-gold); font-size: 11px;">-</span>
      </div>
      <div class="info-cell">
        <span class="info-cell-label">X User-ID</span>
        <span class="info-cell-value" id="inspector-id" style="font-size: 10px; font-family: monospace; color: var(--text-muted);">-</span>
      </div>
      <div class="info-cell">
        <span class="info-cell-label">Rang</span>
        <span class="info-cell-value" id="inspector-rank" style="color: var(--accent-cyan);">-</span>
      </div>
    </div>

    <!-- Cliquen-Details (Im Gruppen-Modus) -->
    <div class="cluster-card" id="inspector-cluster-card" style="display: none;">
      <div class="cluster-card-header">
        <span class="cluster-tag" id="cluster-card-tag" style="background: rgba(245, 158, 11, 0.2); color: var(--accent-gold); border: 1px solid rgba(245, 158, 11, 0.4);">
          👥 Clique
        </span>
        <span id="cluster-card-density" style="font-size: 10px; color: var(--text-muted);">85% Dichte</span>
      </div>
      <div style="display: flex; justify-content: space-between; font-size: 11px;">
        <span style="color: var(--text-muted);">Zentraler Hub:</span>
        <span id="cluster-card-hub" style="color: var(--accent-cyan); font-weight: 700;">@hub</span>
      </div>
      <div style="display: flex; justify-content: space-between; font-size: 11px;">
        <span style="color: var(--text-muted);">Mitglieder:</span>
        <span id="cluster-card-count" style="font-weight: 700;">5 Trader</span>
      </div>
      <button class="hud-btn" id="btn-focus-this-cluster" style="margin-top: 4px; padding: 5px; font-size: 10px; background: rgba(6, 182, 212, 0.15); border-color: var(--accent-cyan);">
        🔍 Nur diese Gruppe isolieren
      </button>
    </div>

    <div class="followers-section" id="inspector-followers-box">
      <span class="followers-title" id="followers-title">Gefolgt von Tradern:</span>
      <div class="followers-pills" id="inspector-followers"></div>
    </div>

    <a href="#" id="inspector-link" target="_blank" class="action-link">
      <span>Profil auf X öffnen</span>
      <span style="font-size: 10px;">↗</span>
    </a>
  </div>

  <div id="network-container"></div>

  <script>
    const DATA = ${JSON.stringify(payload)};

    let network = null;
    let isFrozen = false;
    let currentMode = DATA.initialMode || "network";
    let currentGroupLevel = DATA.initialGroupLevel || 2;
    let currentCliqueKind = "mutual"; // "mutual" (A<->B) oder "cosignal" (Shared seeds)
    let currentMinScore = DATA.minScoreDefault || DATA.maxScore || 8;
    let currentQuality = 2; // 1: Performance, 2: Ausgewogen, 3: Ultra
    let currentPerspective = "alpha";
    let activeClusterId = "all";
    let hideInactives = true;
    let activeSelectedRaw = null;
    let useHubTopology = true;
    let maxClusterMembers = 35;

    const QUALITY_PRESETS = {
      1: {
        label: "⚡ Performance",
        hint: "Keine Schatten, 30 Iterationen – maximale FPS.",
        iterations: 30,
        seedShadow: false,
        nodeShadow: false
      },
      2: {
        label: "⚖️ Ausgewogen",
        hint: "Schatten auf Basis-Tradern, 60 Iterationen – flüssig & schick.",
        iterations: 60,
        seedShadow: { enabled: true, color: 'rgba(245, 158, 11, 0.4)', size: 10 },
        nodeShadow: false
      },
      3: {
        label: "✨ Ultra",
        hint: "Volle Schatten auf allen Knoten, 120 Iterationen – maximale Optik.",
        iterations: 120,
        seedShadow: { enabled: true, color: 'rgba(245, 158, 11, 0.45)', size: 14 },
        nodeShadow: { enabled: true, color: 'rgba(0,0,0,0.5)', size: 7 }
      }
    };

    const LEVEL_DESCRIPTIONS = {
      mutual: {
        1: "Jeder Teilnehmer hat mindestens 1 wechselseitiges Follow (Basis-Netzwerke).",
        2: "Jeder Teilnehmer folgt mindestens 2 Mitgliedern der Gruppe gegenseitig (geschlossene Kreise & Ringe).",
        3: "Jeder Teilnehmer folgt mindestens 3 anderen Tradern aus demselben Netzwerk gegenseitig (dichte Alpha-Zirkel).",
        4: "Jeder Teilnehmer folgt mindestens 4 anderen Mitgliedern der Gruppe gegenseitig (hochvernetzte elitäre Cliquen)."
      },
      cosignal: {
        1: "Accounts, die von mindestens 1 gemeinsamen Basis-Trader gefolgt werden.",
        2: "Accounts, die dieselben 2+ Basis-Trader teilen (starke Trader-Schnittmengen).",
        3: "Accounts, die dieselben 3+ Basis-Trader teilen (gezielte Fokus-Cluster).",
        4: "Exklusive Alpha-Gruppen mit denselben 4+ identischen Basis-Tradern."
      }
    };

    const container = document.getElementById('network-container');
    const searchBox = document.getElementById('search-box');
    const freezeBtn = document.getElementById('freeze-btn');
    const resetBtn = document.getElementById('reset-btn');
    const inspector = document.getElementById('inspector');
    const nodeCountBadge = document.getElementById('node-count-badge');
    const clusterSelect = document.getElementById('cluster-select');
    const clusterCountBadge = document.getElementById('cluster-count-badge');
    const grouplevelBadge = document.getElementById('grouplevel-badge');
    const grouplevelHint = document.getElementById('grouplevel-hint');
    const hideInactivesToggle = document.getElementById('hide-inactives-toggle');
    const inspectorClusterCard = document.getElementById('inspector-cluster-card');
    const isolateClusterBtn = document.getElementById('isolate-cluster-btn');
    const hubTopologyToggle = document.getElementById('hub-topology-toggle');
    const clusterLimitSlider = document.getElementById('cluster-limit-slider');
    const clusterLimitVal = document.getElementById('cluster-limit-val');
    const topologyToggleGroup = document.getElementById('topology-toggle-group');
    const clusterLimitGroup = document.getElementById('cluster-limit-group');

    function updateCliqueControlsUI() {
      const isCosignal = currentMode === 'groups' && currentCliqueKind === 'cosignal';
      if (topologyToggleGroup) topologyToggleGroup.style.display = isCosignal ? 'block' : 'none';
      if (clusterLimitGroup) clusterLimitGroup.style.display = isCosignal ? 'block' : 'none';
    }

    function getColorForScore(score, isSeed) {
      if (isSeed) return { background: '#f59e0b', border: '#fbbf24', highlight: '#fef08a' };
      if (score >= 6) return { background: '#ec4899', border: '#f472b6', highlight: '#fbcfe8' };
      if (score >= 4) return { background: '#8b5cf6', border: '#a78bfa', highlight: '#ddd6fe' };
      if (score >= 2) return { background: '#06b6d4', border: '#22d3ee', highlight: '#cffafe' };
      return { background: '#475569', border: '#64748b', highlight: '#94a3b8' };
    }

    function getActiveClustersPool() {
      const source = currentCliqueKind === 'mutual' ? DATA.groupLevels : DATA.coSignalLevels;
      const lvlKey = String(currentGroupLevel);
      return (source && source[lvlKey]) ? source[lvlKey] : [];
    }

    function updateLevelBadges() {
      const source = currentCliqueKind === 'mutual' ? DATA.groupLevels : DATA.coSignalLevels;
      for (let lvl = 1; lvl <= 4; lvl++) {
        const badge = document.getElementById('badge-lvl-' + lvl);
        const count = (source && source[String(lvl)]) ? source[String(lvl)].length : 0;
        if (badge) badge.innerText = count;
        const statCell = document.getElementById('stat-lvl-' + lvl);
        if (statCell) statCell.innerText = count + ' Gruppen';
      }
    }

    function buildGraphData() {
      if (currentMode === 'network') {
        const activeCandidates = DATA.candidates.filter(c => c.score >= currentMinScore);
        const activeCandidateIds = new Set(activeCandidates.map(c => c.id));
        const qualityPreset = QUALITY_PRESETS[currentQuality] || QUALITY_PRESETS[2];

        const baseSeedNodes = DATA.baseSeeds.map((s, idx) => {
          let x = undefined;
          let y = undefined;
          if (currentPerspective === 'alpha') {
            const angle = (idx * 2 * Math.PI) / DATA.baseSeeds.length;
            x = Math.cos(angle) * 540;
            y = Math.sin(angle) * 540;
          } else if (currentPerspective === 'seeds') {
            const angle = (idx * 2 * Math.PI) / DATA.baseSeeds.length;
            x = Math.cos(angle) * 160;
            y = Math.sin(angle) * 160;
          } else if (currentPerspective === 'radial') {
            const angle = (idx * 2 * Math.PI) / DATA.baseSeeds.length;
            x = Math.cos(angle) * 620;
            y = Math.sin(angle) * 620;
          }
          return {
            id: s.id,
            label: s.label,
            title: s.title,
            shape: 'box',
            margin: 9,
            color: getColorForScore(11, true),
            font: { color: '#ffffff', size: 13, face: 'Inter', bold: true },
            borderWidth: 2,
            shadow: qualityPreset.seedShadow || false,
            x, y,
            raw: s
          };
        });

        // Top-Alpha Nodes nach Score sortieren für saubere Orbit-Verteilung
        const topAlphaCandidates = [...activeCandidates].sort((a, b) => b.score - a.score);
        let tier8Count = 0;
        let tier7Count = 0;
        let tier6Count = 0;

        const candidateNodes = topAlphaCandidates.map((c, idx) => {
          let x = undefined;
          let y = undefined;
          if (currentPerspective === 'alpha') {
            if (c.score >= 8) {
              // Konzentrischer innerer Alpha-Kern (55px Radius, kein (0,0) Kollaps!)
              const a = (tier8Count++ * 2 * Math.PI) / 10;
              x = Math.cos(a) * 55;
              y = Math.sin(a) * 55;
            } else if (c.score === 7) {
              const a = (tier7Count++ * 2 * Math.PI) / 6;
              x = Math.cos(a) * 140;
              y = Math.sin(a) * 140;
            } else if (c.score >= 5) {
              const a = (tier6Count++ * 2 * Math.PI) / 8;
              x = Math.cos(a) * 260;
              y = Math.sin(a) * 260;
            }
          } else if (currentPerspective === 'radial') {
            const orbitR = Math.max(90, (9 - c.score) * 85);
            const a = (idx * 2 * Math.PI) / 10;
            x = Math.cos(a) * orbitR;
            y = Math.sin(a) * orbitR;
          }
          return {
            id: c.id,
            label: c.label,
            title: c.title,
            shape: 'dot',
            size: Math.max(10, Math.min(38, 8 + c.score * 4.2)),
            color: getColorForScore(c.score, false),
            font: { color: '#f8fafc', size: 11, face: 'Inter' },
            borderWidth: 1.5,
            shadow: qualityPreset.nodeShadow || false,
            x, y,
            raw: c
          };
        });

        const allNodes = [...baseSeedNodes, ...candidateNodes];
        const allNodeIds = new Set(allNodes.map(n => n.id));

        const edges = (DATA.edges || [])
          .filter(e => allNodeIds.has(e.from) && allNodeIds.has(e.to))
          .map(e => ({
            from: e.from,
            to: e.to,
            arrows: 'to',
            color: { color: 'rgba(255, 255, 255, 0.15)', highlight: '#38bdf8' },
            width: 1.2
          }));

        return { nodes: new vis.DataSet(allNodes), edges: new vis.DataSet(edges) };

      } else {
        // --- 2. CLIQUEN- & GRUPPEN-MODUS (Exklusive Stufen-Darstellung) ---
        const clusters = getActiveClustersPool();

        const visibleClusters = activeClusterId === 'all'
          ? clusters
          : clusters.filter(c => c.id === activeClusterId);

        const clusterNodeMap = new Map();
        const clusterEdges = [];

        visibleClusters.forEach((cluster, cIdx) => {
          const col = cluster.color;
          const numClusters = Math.max(1, visibleClusters.length);
          const clusterCenterAngle = (cIdx * 2 * Math.PI) / numClusters;
          const clusterDist = numClusters > 1 ? (numClusters > 4 ? 460 : 340) : 0;
          const clusterCenterX = Math.cos(clusterCenterAngle) * clusterDist;
          const clusterCenterY = Math.sin(clusterCenterAngle) * clusterDist;

          // Limitierung bei Ko-Signal (Option 2)
          let membersToRender = cluster.members;
          if (currentCliqueKind === 'cosignal' && maxClusterMembers > 0 && cluster.members.length > maxClusterMembers) {
            membersToRender = cluster.members.slice(0, maxClusterMembers);
          }

          const isCosignal = currentCliqueKind === 'cosignal';
          const isStar = isCosignal && useHubTopology;

          membersToRender.forEach((m, mIdx) => {
            const isHub = m.username === cluster.hub.username;
            let x, y;

            if (isStar) {
              if (isHub) {
                x = clusterCenterX;
                y = clusterCenterY;
              } else {
                const angle = ((mIdx - 1) * 2 * Math.PI) / Math.max(1, membersToRender.length - 1);
                const dist = 80 + Math.min(130, membersToRender.length * 4.5);
                x = clusterCenterX + Math.cos(angle) * dist;
                y = clusterCenterY + Math.sin(angle) * dist;
              }
            } else {
              const memberAngle = (mIdx * 2 * Math.PI) / membersToRender.length;
              const memberDist = 70 + Math.min(80, membersToRender.length * 8);
              x = clusterCenterX + Math.cos(memberAngle) * memberDist;
              y = clusterCenterY + Math.sin(memberAngle) * memberDist;
            }

            clusterNodeMap.set(m.id, {
              id: m.id,
              label: '@' + m.username,
              title: m.name + ' (@' + m.username + ')\\n' + cluster.name + '\\nKonnektivität in Gruppe: ' + m.internalDegree + (cluster.members.length > membersToRender.length ? ' (Top ' + membersToRender.length + ' von ' + cluster.members.length + ')' : ''),
              shape: 'dot',
              size: isHub ? 32 : Math.max(12, Math.min(28, 12 + m.internalDegree * 3.2)),
              color: {
                background: col.background,
                border: col.border,
                highlight: { background: col.highlight, border: '#ffffff' }
              },
              font: { color: '#ffffff', size: 12, face: 'Inter', bold: isHub },
              borderWidth: isHub ? 3 : 1.8,
              shadow: isCosignal ? false : { enabled: true, color: col.background, size: 10 },
              x, y,
              raw: {
                ...m,
                clusterId: cluster.id,
                clusterName: cluster.name,
                clusterColor: col,
                clusterHub: cluster.hub,
                clusterDensity: cluster.density,
                clusterTotal: cluster.members.length
              }
            });
          });

          // Kanten innerhalb der Clique
          if (isStar) {
            // ⚡ Star / Hub Topologie: O(n) Kanten statt O(n²) (Option 1)
            const hubId = cluster.hub.id;
            membersToRender.forEach(m => {
              if (m.id !== hubId) {
                clusterEdges.push({
                  from: hubId,
                  to: m.id,
                  color: { color: col.border, highlight: '#ffffff' },
                  width: 1.5,
                  shadow: false
                });
              }
            });

            // Zusätzlicher Kern-Ring für die Top-4 Mitglieder (visual cohesion)
            const topCore = membersToRender.slice(0, Math.min(5, membersToRender.length));
            for (let i = 0; i < topCore.length; i++) {
              for (let j = i + 1; j < topCore.length; j++) {
                clusterEdges.push({
                  from: topCore[i].id,
                  to: topCore[j].id,
                  color: { color: col.highlight, highlight: '#ffffff' },
                  width: 2.2,
                  shadow: false
                });
              }
            }
          } else {
            // Vollvernetzung (Mutual oder bei deaktivierter Checkbox)
            for (let i = 0; i < membersToRender.length; i++) {
              for (let j = i + 1; j < membersToRender.length; j++) {
                const u = membersToRender[i];
                const v = membersToRender[j];
                clusterEdges.push({
                  from: u.id,
                  to: v.id,
                  arrows: currentCliqueKind === 'mutual' ? 'to;from' : undefined,
                  color: { color: col.border, highlight: '#ffffff' },
                  width: 1.8,
                  shadow: currentCliqueKind === 'mutual' ? { enabled: true, color: col.background, size: 5 } : false
                });
              }
            }
          }
        });

        const nodesList = Array.from(clusterNodeMap.values());

        return {
          nodes: new vis.DataSet(nodesList),
          edges: new vis.DataSet(clusterEdges)
        };
      }
    }

    function initNetwork() {
      const graphData = buildGraphData();
      nodeCountBadge.innerText = graphData.nodes.length + ' Knoten, ' + graphData.edges.length + ' Kanten';

      const isCosignal = currentMode === 'groups' && currentCliqueKind === 'cosignal';

      const options = {
        physics: {
          enabled: !isFrozen,
          forceAtlas2Based: {
            gravitationalConstant: currentMode === 'groups' ? (isCosignal ? -200 : -130) : -75,
            centralGravity: currentMode === 'groups' ? (isCosignal ? 0.04 : 0.028) : 0.015,
            springLength: currentMode === 'groups' ? (isCosignal ? 180 : 140) : 125,
            springConstant: 0.09,
            damping: 0.45
          },
          solver: 'forceAtlas2Based',
          stabilization: { iterations: currentMode === 'groups' ? (isCosignal ? 40 : 60) : 100 }
        },
        interaction: {
          hover: true,
          tooltipDelay: 100,
          zoomView: true,
          dragView: true
        }
      };

      if (!network) {
        network = new vis.Network(container, graphData, options);

        network.once('stabilizationIterationsDone', () => {
          if (currentMode === 'groups' && currentCliqueKind === 'cosignal') {
            network.setOptions({ physics: { enabled: false } });
            isFrozen = true;
            freezeBtn.innerText = '▶️ Start';
          }
        });

        network.on('click', function(params) {
          if (params.nodes.length > 0) {
            const nodeId = params.nodes[0];
            const graphNodes = network.body.data.nodes;
            const node = graphNodes.get(nodeId);
            if (node && node.raw) {
              handleNodeClick(node.raw);
            }
          } else {
            inspector.style.display = 'none';
          }
        });

        network.on('doubleClick', function(params) {
          if (params.nodes.length > 0) {
            const nodeId = params.nodes[0];
            const node = network.body.data.nodes.get(nodeId);
            if (node && node.raw && currentMode === 'groups' && node.raw.clusterId) {
              isolateCluster(node.raw.clusterId);
            }
          }
        });
      } else {
        network.setOptions(options);
        network.setData(graphData);
        if (isCosignal) {
          network.once('stabilizationIterationsDone', () => {
            network.setOptions({ physics: { enabled: false } });
            isFrozen = true;
            freezeBtn.innerText = '▶️ Start';
          });
        }
      }

      updateCliqueControlsUI();
      updateClusterDropdown();
      updateLevelBadges();
    }

    function handleNodeClick(raw) {
      activeSelectedRaw = raw;
      showInspector(raw);

      if (currentMode === 'groups' && raw.clusterId) {
        isolateCluster(raw.clusterId, false);
      }
    }

    function isolateCluster(clusterId, triggerFit = true) {
      const clusters = getActiveClustersPool();
      const targetCluster = clusters.find(c => c.id === clusterId);

      if (!targetCluster) return;

      activeClusterId = clusterId;
      clusterSelect.value = clusterId;

      if (triggerFit && network) {
        network.fit({
          nodes: targetCluster.memberIds,
          animation: { duration: 600, easingFunction: 'easeInOutQuad' }
        });
      }

      isolateClusterBtn.style.display = 'block';
      isolateClusterBtn.innerText = '🔍 ' + targetCluster.name;
    }

    function showInspector(raw) {
      inspector.style.display = 'flex';
      document.getElementById('inspector-avatar').innerText = (raw.name || raw.username || '@')[0].toUpperCase();
      document.getElementById('inspector-name').innerText = raw.name || raw.username;
      document.getElementById('inspector-handle').innerText = '@' + raw.username;
      document.getElementById('inspector-link').href = 'https://x.com/' + raw.username;
      document.getElementById('inspector-id').innerText = raw.originalId || raw.id || '-';

      const sortedCandidates = [...DATA.candidates].sort((a, b) => b.score - a.score);
      const rankIdx = sortedCandidates.findIndex(c => c.id === (raw.id || raw.originalId));
      document.getElementById('inspector-rank').innerText = rankIdx >= 0 ? '#' + (rankIdx + 1) : '-';

      if (currentMode === 'groups' && raw.clusterId) {
        inspectorClusterCard.style.display = 'flex';
        document.getElementById('cluster-card-tag').innerText = '👥 ' + raw.clusterName;
        document.getElementById('cluster-card-tag').style.background = raw.clusterColor?.background ? raw.clusterColor.background + '33' : 'rgba(245, 158, 11, 0.2)';
        document.getElementById('cluster-card-tag').style.borderColor = raw.clusterColor?.border || 'var(--accent-gold)';
        document.getElementById('cluster-card-tag').style.color = raw.clusterColor?.border || 'var(--accent-gold)';
        document.getElementById('cluster-card-density').innerText = raw.clusterDensity + '% Dichte';
        document.getElementById('cluster-card-hub').innerText = '@' + (raw.clusterHub?.username || '-');
        document.getElementById('cluster-card-count').innerText = raw.clusterTotal + ' Trader';

        document.getElementById('inspector-score').innerText = (raw.internalDegree !== undefined ? raw.internalDegree + ' Verbindungen' : raw.score + ' Stimmen');
        document.getElementById('inspector-type').innerText = 'Cliquen-Mitglied';
        document.getElementById('inspector-type').style.color = raw.clusterColor?.border || 'var(--accent-cyan)';
        document.getElementById('followers-title').innerText = 'Mitglieder in dieser Clique:';

        const clusters = getActiveClustersPool();
        const cl = clusters.find(c => c.id === raw.clusterId);
        if (cl && cl.members) {
          document.getElementById('inspector-followers').innerHTML = cl.members
            .filter(m => m.username !== raw.username)
            .map(m => '<span class="follower-pill" onclick="focusMember(\\'' + m.id + '\\')">@' + m.username + ' (' + m.internalDegree + ')</span>')
            .join('');
        }
      } else {
        inspectorClusterCard.style.display = 'none';
        if (raw.isSeed) {
          document.getElementById('inspector-score').innerText = '11/11';
          document.getElementById('inspector-type').innerText = 'Basis-Trader';
          document.getElementById('inspector-type').style.color = 'var(--accent-gold)';
          document.getElementById('followers-title').innerText = 'Überwachter CCO-Account';
          document.getElementById('inspector-followers').innerHTML = '<span class="follower-pill" style="background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.3); color: var(--accent-gold);">Registriert in Supabase x_users</span>';
        } else {
          document.getElementById('inspector-score').innerText = raw.score + ' Stimmen';
          document.getElementById('inspector-type').innerText = raw.score >= 5 ? 'Top Alpha' : 'Alpha Signal';
          document.getElementById('inspector-type').style.color = raw.score >= 5 ? 'var(--accent-pink)' : 'var(--accent-cyan)';
          document.getElementById('followers-title').innerText = 'Follower-Abdeckung:';

          if (raw.followedBy && raw.followedBy.length > 0) {
            document.getElementById('inspector-followers').innerHTML = raw.followedBy.map(h => '<span class="follower-pill">@' + h.replace('@','') + '</span>').join('');
          } else {
            document.getElementById('inspector-followers').innerHTML = '<span class="follower-pill" style="color: var(--accent-pink);">' + raw.score + ' Tradern folgen</span>';
          }
        }
      }
    }

    window.focusMember = function(nodeId) {
      if (!network) return;
      const node = network.body.data.nodes.get(nodeId);
      if (node) {
        network.focus(nodeId, {
          scale: 1.4,
          animation: { duration: 500, easingFunction: 'easeInOutQuad' }
        });
        network.selectNodes([nodeId]);
        if (node.raw) showInspector(node.raw);
      } else {
        const clusters = getActiveClustersPool();
        for (const cl of clusters) {
          const m = cl.members.find(x => x.id === nodeId);
          if (m) {
            showInspector({
              ...m,
              clusterId: cl.id,
              clusterName: cl.name,
              clusterColor: cl.color,
              clusterHub: cl.hub,
              clusterDensity: cl.density,
              clusterTotal: cl.members.length
            });
            break;
          }
        }
      }
    };

    function updateClusterDropdown() {
      const clusters = getActiveClustersPool();
      clusterCountBadge.innerText = clusters.length + ' Netzwerk' + (clusters.length === 1 ? '' : 'e');
      document.getElementById('stat-active-clusters-count').innerText = clusters.length + ' Gruppen';

      clusterSelect.innerHTML = '<option value="all">🌟 Alle Gruppen dieser Stufe (' + clusters.length + ')</option>';
      clusters.forEach((cl) => {
        const opt = document.createElement('option');
        opt.value = cl.id;
        opt.innerText = cl.name + ' (Hub: @' + cl.hub.username + ', ' + cl.members.length + ' Trader)';
        clusterSelect.appendChild(opt);
      });
      clusterSelect.value = activeClusterId;

      if (activeClusterId !== 'all') {
        const targetCluster = clusters.find(c => c.id === activeClusterId);
        if (targetCluster) {
          isolateClusterBtn.style.display = 'block';
          isolateClusterBtn.innerText = '🔍 ' + targetCluster.name;
        } else {
          isolateClusterBtn.style.display = 'none';
        }
      } else {
        isolateClusterBtn.style.display = 'none';
      }
    }

    // --- EVENT LISTENER ---

    document.getElementById('btn-mode-network').addEventListener('click', () => {
      currentMode = 'network';
      document.getElementById('btn-mode-network').classList.add('active');
      document.getElementById('btn-mode-groups').classList.remove('active');
      document.getElementById('controls-network').style.display = 'flex';
      document.getElementById('controls-groups').style.display = 'none';
      updateCliqueControlsUI();
      isolateClusterBtn.style.display = 'none';
      inspector.style.display = 'none';
      initNetwork();
    });

    document.getElementById('btn-mode-groups').addEventListener('click', () => {
      currentMode = 'groups';
      document.getElementById('btn-mode-groups').classList.add('active');
      document.getElementById('btn-mode-network').classList.remove('active');
      document.getElementById('controls-groups').style.display = 'flex';
      document.getElementById('controls-network').style.display = 'none';
      updateCliqueControlsUI();
      const pool = getActiveClustersPool();
      activeClusterId = (currentCliqueKind === 'cosignal' && pool.length > 0) ? pool[0].id : 'all';
      inspector.style.display = 'none';
      initNetwork();
    });

    // Cliquen-Typ: Mutual vs Ko-Signal
    document.getElementById('clique-type-mutual').addEventListener('click', () => {
      currentCliqueKind = 'mutual';
      document.getElementById('clique-type-mutual').classList.add('active');
      document.getElementById('clique-type-cosignal').classList.remove('active');
      updateCliqueControlsUI();
      grouplevelHint.innerText = LEVEL_DESCRIPTIONS.mutual[currentGroupLevel] || '';
      activeClusterId = 'all';
      initNetwork();
    });

    document.getElementById('clique-type-cosignal').addEventListener('click', () => {
      currentCliqueKind = 'cosignal';
      document.getElementById('clique-type-cosignal').classList.add('active');
      document.getElementById('clique-type-mutual').classList.remove('active');
      updateCliqueControlsUI();
      grouplevelHint.innerText = LEVEL_DESCRIPTIONS.cosignal[currentGroupLevel] || '';
      const pool = getActiveClustersPool();
      activeClusterId = pool.length > 0 ? pool[0].id : 'all';
      initNetwork();
    });

    // Grouplevel-Buttons (1, 2, 3, 4) - Strikte Level-Trennung
    document.querySelectorAll('.grouplevel-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        document.querySelectorAll('.grouplevel-btn').forEach(b => b.classList.remove('active'));
        const targetBtn = e.currentTarget;
        targetBtn.classList.add('active');

        currentGroupLevel = parseInt(targetBtn.getAttribute('data-level'), 10);
        grouplevelBadge.innerText = 'Stufe ' + currentGroupLevel;
        grouplevelHint.innerText = (LEVEL_DESCRIPTIONS[currentCliqueKind] && LEVEL_DESCRIPTIONS[currentCliqueKind][currentGroupLevel]) || '';

        const pool = getActiveClustersPool();
        activeClusterId = (currentCliqueKind === 'cosignal' && pool.length > 0) ? pool[0].id : 'all';
        isolateClusterBtn.style.display = 'none';
        inspector.style.display = 'none';

        initNetwork();
      });
    });

    clusterSelect.addEventListener('change', (e) => {
      activeClusterId = e.target.value;
      if (activeClusterId === 'all') {
        isolateClusterBtn.style.display = 'none';
        initNetwork();
        network.fit({ animation: { duration: 500 } });
      } else {
        isolateCluster(activeClusterId, true);
      }
    });

    hideInactivesToggle.addEventListener('change', (e) => {
      hideInactives = e.target.checked;
      initNetwork();
    });

    document.getElementById('btn-focus-this-cluster')?.addEventListener('click', () => {
      if (activeSelectedRaw && activeSelectedRaw.clusterId) {
        isolateCluster(activeSelectedRaw.clusterId, true);
      }
    });

    if (hubTopologyToggle) {
      hubTopologyToggle.addEventListener('change', (e) => {
        useHubTopology = e.target.checked;
        if (currentMode === 'groups') initNetwork();
      });
    }

    if (clusterLimitSlider) {
      clusterLimitSlider.addEventListener('input', (e) => {
        maxClusterMembers = parseInt(e.target.value, 10);
        if (clusterLimitVal) clusterLimitVal.innerText = 'Top ' + maxClusterMembers;
        if (currentMode === 'groups') initNetwork();
      });
    }

    const minScoreSlider = document.getElementById('min-score-slider');
    if (minScoreSlider) {
      minScoreSlider.addEventListener('input', (e) => {
        currentMinScore = parseInt(e.target.value, 10);
        document.getElementById('score-val').innerText = currentMinScore + '+ Stimmen';
        if (currentMode === 'network') initNetwork();
      });
    }

    const qualitySlider = document.getElementById('quality-slider');
    if (qualitySlider) {
      qualitySlider.addEventListener('input', (e) => {
        currentQuality = parseInt(e.target.value, 10);
        const preset = QUALITY_PRESETS[currentQuality];
        if (preset) {
          document.getElementById('quality-val').innerText = preset.label;
          document.getElementById('quality-hint').innerText = preset.hint;
          initNetwork();
        }
      });
    }

    document.querySelectorAll('.persp-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        document.querySelectorAll('.persp-btn').forEach(b => b.classList.remove('active'));
        const target = e.currentTarget;
        target.classList.add('active');
        currentPerspective = target.getAttribute('data-persp');
        document.getElementById('perspective-badge').innerText = target.innerText;
        if (currentMode === 'network') initNetwork();
      });
    });

    document.getElementById('tab-btn-overview').addEventListener('click', () => {
      document.querySelectorAll('.stats-tab-btn').forEach(b => b.classList.remove('active'));
      document.getElementById('tab-btn-overview').classList.add('active');
      document.getElementById('tab-content-overview').style.display = 'block';
      document.getElementById('tab-content-dist').style.display = 'none';
      document.getElementById('tab-content-clusters').style.display = 'none';
    });

    document.getElementById('tab-btn-dist').addEventListener('click', () => {
      document.querySelectorAll('.stats-tab-btn').forEach(b => b.classList.remove('active'));
      document.getElementById('tab-btn-dist').classList.add('active');
      document.getElementById('tab-content-overview').style.display = 'none';
      document.getElementById('tab-content-dist').style.display = 'block';
      document.getElementById('tab-content-clusters').style.display = 'none';
    });

    document.getElementById('tab-btn-clusters').addEventListener('click', () => {
      document.querySelectorAll('.stats-tab-btn').forEach(b => b.classList.remove('active'));
      document.getElementById('tab-btn-clusters').classList.add('active');
      document.getElementById('tab-content-overview').style.display = 'none';
      document.getElementById('tab-content-dist').style.display = 'none';
      document.getElementById('tab-content-clusters').style.display = 'block';
    });

    searchBox.addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase().trim().replace('@', '');
      if (!q || !network) return;

      const allNodes = network.body.data.nodes.get();
      const match = allNodes.find(n => n.raw.username.toLowerCase().includes(q) || (n.raw.name && n.raw.name.toLowerCase().includes(q)));
      if (match) {
        network.focus(match.id, {
          scale: 1.4,
          animation: { duration: 500, easingFunction: 'easeInOutQuad' }
        });
        network.selectNodes([match.id]);
        handleNodeClick(match.raw);
      }
    });

    freezeBtn?.addEventListener('click', () => {
      isFrozen = !isFrozen;
      network.setOptions({ physics: { enabled: !isFrozen } });
      freezeBtn.innerText = isFrozen ? '▶️ Start' : '⏸️ Pause';
    });

    resetBtn?.addEventListener('click', () => {
      currentMinScore = DATA.minScoreDefault || DATA.maxScore || 8;
      if (minScoreSlider) {
        minScoreSlider.value = currentMinScore;
        document.getElementById('score-val').innerText = currentMinScore + '+ Stimmen';
      }
      currentQuality = 2;
      if (qualitySlider) {
        qualitySlider.value = 2;
        const preset = QUALITY_PRESETS[2];
        if (preset) {
          document.getElementById('quality-val').innerText = preset.label;
          document.getElementById('quality-hint').innerText = preset.hint;
        }
      }
      currentPerspective = 'alpha';
      document.querySelectorAll('.persp-btn').forEach(b => {
        b.classList.toggle('active', b.getAttribute('data-persp') === 'alpha');
      });
      document.getElementById('perspective-badge').innerText = '🌟 Alpha-Zentrum';
      searchBox.value = '';
      inspector.style.display = 'none';
      initNetwork();
      network.fit({ animation: { duration: 500 } });
    });

    isolateClusterBtn?.addEventListener('click', () => {
      if (activeClusterId && activeClusterId !== 'all') {
        isolateCluster(activeClusterId, true);
      }
    });

    document.getElementById('close-inspector').addEventListener('click', () => {
      inspector.style.display = 'none';
    });

    initNetwork();
  </script>
</body>
</html>`;

  await Deno.writeTextFile(htmlOutputPath, htmlContent);
  try {
    await Deno.writeTextFile(secondaryOutputPath, htmlContent);
  } catch {
    // optional
  }

  console.log(`✅ Interaktive Visualisierung erfolgreich generiert:`);
  console.log(`   👉 ${htmlOutputPath}`);
  console.log(`   👉 ${secondaryOutputPath}\n`);

  if (!options.noOpen) {
    try {
      console.log(`🌐 Öffne Visualisierung im Standard-Browser...`);
      new Deno.Command("xdg-open", { args: [htmlOutputPath] }).spawn();
    } catch {
      console.log(`💡 Tipp: Öffne die Datei direkt im Browser: file://${htmlOutputPath}`);
    }
  }
}

if (import.meta.main) {
  main();
}
