/**
 * Generator für den interaktiven 2D-Netzwerk-Graphen im Browser
 *
 * Liest den aktuellen Zustand aus alpha_state_*.json und erzeugt eine
 * hardwarebeschleunigte, eigenständige HTML-Visualisierung (alpha_graph.html)
 * mit Force-Directed Physics, Schieberegler-Filtern, Suche, kompakter Info-Box
 * und einer Datenbank-Statistiktabelle unten links.
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

function parseArgs(args: string[]) {
  let statePath = "";
  let outPath = "";
  let minScore = 2;
  let noOpen = false;

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--file" && i + 1 < args.length) {
      statePath = args[++i];
    } else if (arg === "--out" && i + 1 < args.length) {
      outPath = args[++i];
    } else if (arg === "--min" && i + 1 < args.length) {
      minScore = parseInt(args[++i], 10);
    } else if (arg === "--no-open") {
      noOpen = true;
    }
  }

  return { statePath, outPath, minScore, noOpen };
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

async function main() {
  const options = parseArgs(Deno.args);

  const scriptDir = new URL(".", import.meta.url).pathname;
  const defaultStatePath = `${scriptDir}../alpha_state_supabase_registered.json`;
  const stateFilePath = options.statePath || defaultStatePath;
  const htmlOutputPath = options.outPath || `${scriptDir}../../alpha_graph.html`;

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

  const candidateEntries = Object.values(allScoresMap);
  console.log(`🎯 Gesamt-Kandidaten im Pool: ${candidateEntries.length}`);

  const seedIds = state.processed || [];
  const baseSeedNodes: any[] = [];

  for (const id of seedIds) {
    const meta = KNOWN_SEEDS[id] || { username: `user_${id}`, name: `Influencer ${id.slice(-4)}` };
    baseSeedNodes.push({
      id: `seed_${id}`,
      originalId: id,
      label: `@${meta.username}`,
      title: `${meta.name} (@${meta.username})\n[Basis-Influencer]`,
      group: "seed",
      username: meta.username,
      name: meta.name,
      isSeed: true,
      score: 11,
    });
  }

  const candidateNodes: any[] = [];
  const candidateEdges: any[] = [];

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
        const foundSeed = baseSeedNodes.find((s) => s.username.toLowerCase() === cleanHandle);
        if (foundSeed) {
          candidateEdges.push({
            from: foundSeed.id,
            to: c.id,
            arrows: "to",
          });
        }
      }
    }
  }

  const payload = {
    generatedAt: new Date().toISOString(),
    stateUpdatedAt: state.updatedAt,
    baseSeeds: baseSeedNodes,
    candidates: candidateNodes,
    edges: candidateEdges,
    minScoreDefault: options.minScore,
    totalApiCalls: state.totalApiCalls || 0,
    totalProfilesFetched: state.totalProfilesFetched || 0,
    estimatedCostUsd: state.estimatedCostUsd || 0,
    queriedAccountsCount: (state.queriedAccounts && state.queriedAccounts.length) || (state.processed && state.processed.length) || 0,
  };

  const htmlContent = `<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Alpha-Influencer Network Explorer</title>
  <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-base: #090d16;
      --bg-surface: rgba(15, 23, 42, 0.9);
      --border-color: rgba(255, 255, 255, 0.09);
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
      width: 320px;
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
      width: 320px;
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
      width: 270px;
      background: rgba(15, 23, 42, 0.94);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      border: 1px solid var(--border-color);
      border-radius: 14px;
      padding: 14px 16px;
      box-shadow: 0 16px 36px rgba(0, 0, 0, 0.7);
      z-index: 100;
      display: none;
      flex-direction: column;
      gap: 12px;
      animation: slideIn 0.18s ease-out;
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
      width: 38px;
      height: 38px;
      border-radius: 50%;
      background: linear-gradient(135deg, var(--accent-purple), var(--accent-cyan));
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 16px;
      font-weight: 700;
      color: white;
      flex-shrink: 0;
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
      background: rgba(0, 0, 0, 0.25);
      border-radius: 8px;
      padding: 8px;
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
      max-height: 90px;
      overflow-y: auto;
    }

    .follower-pill {
      font-size: 10px;
      background: rgba(255, 255, 255, 0.07);
      border: 1px solid rgba(255, 255, 255, 0.05);
      padding: 2px 6px;
      border-radius: 6px;
      color: var(--text-main);
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
  </style>
</head>
<body>

  <!-- Controls Panel (Oben Links) -->
  <div class="hud-panel">
    <div class="hud-header">
      <div class="hud-title">
        <span>⚡ Alpha Graph Explorer</span>
      </div>
      <span class="stat-badge" id="node-count-badge">0 Knoten</span>
    </div>

    <div class="form-group">
      <input type="text" id="search-box" class="search-input" placeholder="🔍 Account suchen (z.B. @sama)...">
    </div>

    <div class="form-group">
      <div class="form-label">
        <span>Mindest-Score</span>
        <span id="score-val" style="color: var(--accent-pink);">2+ Stimmen</span>
      </div>
      <input type="range" id="min-score-slider" class="range-slider" min="1" max="8" value="${options.minScore}">
    </div>

    <div class="btn-group">
      <button class="hud-btn" id="freeze-btn">⏸️ Pause</button>
      <button class="hud-btn" id="reset-btn">🔄 Reset</button>
    </div>
  </div>

  <!-- Datenbank-Statistik Tabelle (Unten Links) -->
  <div class="db-stats-panel">
    <div class="db-stats-header">
      <div class="db-stats-title">
        <span class="pulse-dot"></span>
        <span>Datenbank & Signal-Status</span>
      </div>
      <span class="stat-badge" id="db-mode-badge">Supabase</span>
    </div>
    <table class="stats-table">
      <tbody>
        <tr>
          <td>Basis-Influencer (x_users)</td>
          <td class="val val-highlight">11</td>
        </tr>
        <tr>
          <td>Abgefragte Accounts (Seeds)</td>
          <td class="val">${payload.queriedAccountsCount}</td>
        </tr>
        <tr>
          <td>Gesamt erfasste Profile</td>
          <td class="val">${payload.totalProfilesFetched.toLocaleString("de-DE")}</td>
        </tr>
        <tr>
          <td>Alpha-Kandidaten (Unique)</td>
          <td class="val">${payload.candidates.length.toLocaleString("de-DE")}</td>
        </tr>
        <tr>
          <td>Echte Schnittmengen (≥ 2)</td>
          <td class="val val-highlight">${payload.candidates.filter((c: any) => c.score >= 2).length.toLocaleString("de-DE")}</td>
        </tr>
        <tr>
          <td>Höchste Signal-Stärke</td>
          <td class="val">${Math.max(...payload.candidates.map((c: any) => c.score), 0)} Influencer</td>
        </tr>
        <tr>
          <td>API-Kosten (geschätzt)</td>
          <td class="val val-cost">~$${payload.estimatedCostUsd.toFixed(4)} USD</td>
        </tr>
        <tr>
          <td>Stand der Daten</td>
          <td class="val" style="font-size: 10px;">${new Date(payload.stateUpdatedAt).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <!-- Kompakte, informative Inspector-Box (Rechts) -->
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
    let currentMinScore = DATA.minScoreDefault || 2;
    const sortedCandidates = [...DATA.candidates].sort((a, b) => b.score - a.score);

    const container = document.getElementById('network-container');
    const slider = document.getElementById('min-score-slider');
    const scoreVal = document.getElementById('score-val');
    const searchBox = document.getElementById('search-box');
    const freezeBtn = document.getElementById('freeze-btn');
    const resetBtn = document.getElementById('reset-btn');
    const inspector = document.getElementById('inspector');
    const nodeCountBadge = document.getElementById('node-count-badge');

    function getColorForScore(score, isSeed) {
      if (isSeed) return { background: '#f59e0b', border: '#fbbf24', highlight: '#fef08a' };
      if (score >= 6) return { background: '#ec4899', border: '#f472b6', highlight: '#fbcfe8' };
      if (score >= 4) return { background: '#8b5cf6', border: '#a78bfa', highlight: '#ddd6fe' };
      if (score >= 2) return { background: '#06b6d4', border: '#22d3ee', highlight: '#cffafe' };
      return { background: '#475569', border: '#64748b', highlight: '#94a3b8' };
    }

    function buildGraphData(minScore) {
      const activeCandidates = DATA.candidates.filter(c => c.score >= minScore);
      const activeCandidateIds = new Set(activeCandidates.map(c => c.id));

      const nodes = [
        ...DATA.baseSeeds.map(s => ({
          id: s.id,
          label: s.label,
          title: s.title,
          shape: 'box',
          margin: 9,
          color: getColorForScore(11, true),
          font: { color: '#ffffff', size: 13, face: 'Inter', bold: true },
          borderWidth: 2,
          shadow: { enabled: true, color: 'rgba(245, 158, 11, 0.45)', size: 14 },
          raw: s
        })),
        ...activeCandidates.map(c => ({
          id: c.id,
          label: c.label,
          title: c.title,
          shape: 'dot',
          size: Math.max(10, Math.min(38, 8 + c.score * 4.2)),
          color: getColorForScore(c.score, false),
          font: { color: '#f8fafc', size: 11, face: 'Inter' },
          borderWidth: 1.5,
          shadow: { enabled: true, color: 'rgba(0,0,0,0.5)', size: 7 },
          raw: c
        }))
      ];

      let edges = [];
      if (DATA.edges && DATA.edges.length > 0) {
        edges = DATA.edges.filter(e => activeCandidateIds.has(e.to));
      } else {
        for (const c of activeCandidates) {
          if (c.score >= 2) {
            const numEdges = Math.min(c.score, DATA.baseSeeds.length);
            for (let i = 0; i < numEdges; i++) {
              edges.push({
                from: DATA.baseSeeds[i].id,
                to: c.id,
                color: { color: 'rgba(255,255,255,0.1)', highlight: '#38bdf8' },
                width: 1
              });
            }
          }
        }
      }

      return { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
    }

    function initNetwork() {
      const graphData = buildGraphData(currentMinScore);
      nodeCountBadge.innerText = graphData.nodes.length + ' Knoten';

      const options = {
        physics: {
          forceAtlas2Based: {
            gravitationalConstant: -70,
            centralGravity: 0.014,
            springLength: 125,
            springConstant: 0.08,
            damping: 0.4
          },
          solver: 'forceAtlas2Based',
          stabilization: { iterations: 100 }
        },
        interaction: {
          hover: true,
          tooltipDelay: 100,
          zoomView: true,
          dragView: true
        }
      };

      network = new vis.Network(container, graphData, options);

      network.on('click', function(params) {
        if (params.nodes.length > 0) {
          const nodeId = params.nodes[0];
          const node = graphData.nodes.get(nodeId);
          if (node && node.raw) {
            showInspector(node.raw);
          }
        } else {
          inspector.style.display = 'none';
        }
      });
    }

    function showInspector(raw) {
      inspector.style.display = 'flex';
      document.getElementById('inspector-avatar').innerText = (raw.name || raw.username || '@')[0].toUpperCase();
      document.getElementById('inspector-name').innerText = raw.name || raw.username;
      document.getElementById('inspector-handle').innerText = '@' + raw.username;
      document.getElementById('inspector-link').href = 'https://x.com/' + raw.username;
      document.getElementById('inspector-id').innerText = raw.originalId || raw.id || '-';

      const rankIdx = sortedCandidates.findIndex(c => c.id === (raw.id || raw.originalId));
      document.getElementById('inspector-rank').innerText = rankIdx >= 0 ? '#' + (rankIdx + 1) : '-';

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
          document.getElementById('inspector-followers').innerHTML = raw.followedBy.map(h => '<span class="follower-pill">@' + h + '</span>').join('');
        } else {
          document.getElementById('inspector-followers').innerHTML = '<span class="follower-pill" style="color: var(--accent-pink);">' + raw.score + ' von 11 Tradern folgen</span>';
        }
      }
    }

    document.getElementById('close-inspector').addEventListener('click', () => {
      inspector.style.display = 'none';
    });

    slider.addEventListener('input', (e) => {
      currentMinScore = parseInt(e.target.value, 10);
      scoreVal.innerText = currentMinScore + '+ Stimmen';
      const newGraph = buildGraphData(currentMinScore);
      network.setData(newGraph);
      nodeCountBadge.innerText = newGraph.nodes.length + ' Knoten';
    });

    searchBox.addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase().trim().replace('@', '');
      if (!q || !network) return;

      const allNodes = buildGraphData(currentMinScore).nodes.get();
      const match = allNodes.find(n => n.raw.username.toLowerCase().includes(q) || (n.raw.name && n.raw.name.toLowerCase().includes(q)));
      if (match) {
        network.focus(match.id, {
          scale: 1.3,
          animation: { duration: 500, easingFunction: 'easeInOutQuad' }
        });
        network.selectNodes([match.id]);
        showInspector(match.raw);
      }
    });

    freezeBtn.addEventListener('click', () => {
      isFrozen = !isFrozen;
      network.setOptions({ physics: { enabled: !isFrozen } });
      freezeBtn.innerText = isFrozen ? '▶️ Start' : '⏸️ Pause';
    });

    resetBtn.addEventListener('click', () => {
      network.fit({ animation: { duration: 500 } });
      searchBox.value = '';
      inspector.style.display = 'none';
    });

    initNetwork();
  </script>
</body>
</html>`;

  await Deno.writeTextFile(htmlOutputPath, htmlContent);
  console.log(`✅ Interaktive Visualisierung erfolgreich generiert:`);
  console.log(`   👉 ${htmlOutputPath}\n`);

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
