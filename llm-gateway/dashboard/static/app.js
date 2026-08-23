// ============================================================================
// QJM Control Plane - Frontend Logic
// Switchyard Hub, TOML Live Editor, Playground & System Manager
// ============================================================================

let currentConfig = { routes: [], targets: [], clients: [], raw_toml: "" };
let editingRouteKey = null;
let editingTargetKey = null;
let editingClientKey = null;

document.addEventListener("DOMContentLoaded", () => {
    initTabs();
    loadAllData();
    startTelemetryPolling();
    window.addEventListener("resize", () => {
        redrawAllAfterburnerCharts();
    });
});

// --- Tab Switching ---
function initTabs() {
    const tabs = document.querySelectorAll(".nav-tab");
    tabs.forEach(tab => {
        tab.addEventListener("click", () => {
            tabs.forEach(t => t.classList.remove("active"));
            document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));
            
            tab.classList.add("active");
            const targetPane = document.getElementById(tab.dataset.tab);
            if (targetPane) targetPane.classList.add("active");

            if (tab.dataset.tab === "tab-toml") {
                loadRawToml();
            } else if (tab.dataset.tab === "tab-stats") {
                loadSwitchyardStats();
            } else if (tab.dataset.tab === "tab-system") {
                setTimeout(redrawAllAfterburnerCharts, 50);
            }
        });
    });
}

// --- Data Loading ---
async function loadAllData() {
    await Promise.all([
        loadFullRoutingConfig(),
        loadSystemTelemetry(),
        loadContainers(),
    ]);
}

async function refreshDashboard() {
    showToast("Aktualisiere Dashboard...");
    await loadAllData();
}

async function loadFullRoutingConfig() {
    try {
        const res = await fetch("/api/routing/full");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        currentConfig = data;

        renderRoutesTable(data.routes || []);
        renderTargetsTable(data.targets || []);
        renderClientsTable(data.clients || []);
        updateRouteSelectOptions(data.routes || [], data.targets || [], data.clients || []);
    } catch (e) {
        console.error("Fehler beim Laden der Switchyard Config:", e);
        showToast("Fehler beim Laden der Switchyard Config", true);
    }
}

// --- Render Routes Table ---
function renderRoutesTable(routes) {
    const tbody = document.getElementById("routes-table-body");
    document.getElementById("routes-count").innerText = `${routes.length} Routen`;
    if (!routes || routes.length === 0) {
        tbody.innerHTML = `<tr><td colspan="4" class="muted">Keine Routen konfiguriert.</td></tr>`;
        return;
    }

    tbody.innerHTML = routes.map(r => {
        let typeBadge = `<span class="type-badge ${r.type}">${r.type}</span>`;
        let targetDesc = "";

        if (r.type === "passthrough") {
            targetDesc = `<span class="code-pill">${r.target || "-"}</span>`;
        } else if (r.type === "llm_classifier") {
            targetDesc = `<div class="target-desc">Judge: <b>${r.classifier_target}</b><br>Weak: <b>${r.weak_target}</b> | Strong: <b>${r.strong_target}</b> (Thresh: ${r.base_threshold})</div>`;
        } else if (r.type === "fallback") {
            targetDesc = `<div class="target-desc">Chain: ${(r.targets || []).map(t => `<b>${t}</b>`).join(" ➔ ")}</div>`;
        } else if (r.type === "weighted") {
            targetDesc = `<div class="target-desc">${JSON.stringify(r.weights || {})}</div>`;
        }

        return `
            <tr>
                <td><span class="code-pill" style="font-weight:700; color:#fff;">${r.id || r.key}</span></td>
                <td>${typeBadge}</td>
                <td>${targetDesc}</td>
                <td>
                    <div class="action-btns">
                        <button class="btn-icon" onclick="openEditRouteModal('${r.key}')" title="Bearbeiten">✏️</button>
                        <button class="btn-icon del" onclick="deleteRoute('${r.key}')" title="Löschen">🗑️</button>
                    </div>
                </td>
            </tr>
        `;
    }).join("");
}

// --- Render Targets Table ---
function renderTargetsTable(targets) {
    const tbody = document.getElementById("targets-table-body");
    document.getElementById("targets-count").innerText = `${targets.length} Targets`;
    if (!targets || targets.length === 0) {
        tbody.innerHTML = `<tr><td colspan="4" class="muted">Keine Targets vorhanden.</td></tr>`;
        return;
    }

    tbody.innerHTML = targets.map(t => `
        <tr>
            <td><span class="code-pill" style="color:var(--primary);">${t.key}</span></td>
            <td><span class="code-pill">${t.id}</span></td>
            <td><span class="code-pill" style="color:#d1c4e9;">${t.llm_client}</span></td>
            <td>
                <div class="action-btns">
                    <button class="btn-icon del" onclick="deleteTarget('${t.key}')" title="Löschen">🗑️</button>
                </div>
            </td>
        </tr>
    `).join("");
}

// --- Render Clients Table ---
function renderClientsTable(clients) {
    const tbody = document.getElementById("clients-table-body");
    document.getElementById("clients-count").innerText = `${clients.length} Clients`;
    if (!clients || clients.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" class="muted">Keine Clients vorhanden.</td></tr>`;
        return;
    }

    tbody.innerHTML = clients.map(c => `
        <tr>
            <td><span class="code-pill" style="color:#00e676;">${c.key}</span></td>
            <td><span class="type-badge">${c.format}</span></td>
            <td style="font-family:var(--font-mono); font-size:11px;">${c.base_url}</td>
            <td><span class="code-pill">${c.api_key_env || "-"}</span></td>
            <td>
                <div class="action-btns">
                    <button class="btn-icon del" onclick="deleteClient('${c.key}')" title="Löschen">🗑️</button>
                </div>
            </td>
        </tr>
    `).join("");
}

// --- Dynamic Form Select Populate ---
function updateRouteSelectOptions(routes, targets, clients) {
    // Populate test route select
    const testSelect = document.getElementById("test-route-select");
    if (testSelect) {
        const prev = testSelect.value;
        testSelect.innerHTML = routes.map(r => `<option value="${r.id || r.key}">${r.id || r.key} (${r.type})</option>`).join("");
        if (prev) testSelect.value = prev;
    }

    // Populate route target selects in modal
    const targetOpts = targets.map(t => `<option value="${t.key}">${t.key} (${t.id})</option>`).join("");
    const targetSelect = document.getElementById("route-target-select");
    const clfTargetSelect = document.getElementById("route-clf-target-select");
    const weakSelect = document.getElementById("route-weak-target-select");
    const strongSelect = document.getElementById("route-strong-target-select");

    if (targetSelect) targetSelect.innerHTML = targetOpts;
    if (clfTargetSelect) clfTargetSelect.innerHTML = targetOpts;
    if (weakSelect) weakSelect.innerHTML = targetOpts;
    if (strongSelect) strongSelect.innerHTML = targetOpts;

    // Populate client select in target modal
    const clientSelect = document.getElementById("target-client-select");
    if (clientSelect) {
        clientSelect.innerHTML = clients.map(c => `<option value="${c.key}">${c.key} (${c.base_url})</option>`).join("");
    }
}

// --- Route Modal Management ---
function openCreateRouteModal() {
    editingRouteKey = null;
    document.getElementById("modal-route-title").innerText = "Neue Route erstellen";
    document.getElementById("route-key-input").value = "";
    document.getElementById("route-key-input").disabled = false;
    document.getElementById("route-type-select").value = "passthrough";
    toggleRouteTypeFields();
    openModal("modal-route");
}

function openEditRouteModal(key) {
    const route = (currentConfig.routes || []).find(r => r.key === key || r.id === key);
    if (!route) return;

    editingRouteKey = key;
    document.getElementById("modal-route-title").innerText = `Route bearbeiten: ${key}`;
    document.getElementById("route-key-input").value = route.id || route.key;
    document.getElementById("route-type-select").value = route.type || "passthrough";
    toggleRouteTypeFields();

    if (route.type === "passthrough") {
        document.getElementById("route-target-select").value = route.target || "";
    } else if (route.type === "llm_classifier") {
        document.getElementById("route-clf-target-select").value = route.classifier_target || "";
        document.getElementById("route-weak-target-select").value = route.weak_target || "";
        document.getElementById("route-strong-target-select").value = route.strong_target || "";
        document.getElementById("route-base-thresh").value = route.base_threshold || 0.5;
        document.getElementById("route-step-thresh").value = route.threshold_step || 0.1;
    } else if (route.type === "fallback") {
        document.getElementById("route-fallback-targets").value = (route.targets || []).join(", ");
    }

    openModal("modal-route");
}

function toggleRouteTypeFields() {
    const type = document.getElementById("route-type-select").value;
    document.getElementById("field-passthrough").classList.toggle("hidden", type !== "passthrough");
    document.getElementById("field-classifier").classList.toggle("hidden", type !== "llm_classifier");
    document.getElementById("field-fallback").classList.toggle("hidden", type !== "fallback");
}

async function submitRouteForm() {
    const key = document.getElementById("route-key-input").value.trim();
    const type = document.getElementById("route-type-select").value;
    if (!key) {
        showToast("Bitte Routen-ID eingeben", true);
        return;
    }

    const payload = { key: key, id: key, type: type };
    if (type === "passthrough") {
        payload.target = document.getElementById("route-target-select").value;
    } else if (type === "llm_classifier") {
        payload.classifier_target = document.getElementById("route-clf-target-select").value;
        payload.weak_target = document.getElementById("route-weak-target-select").value;
        payload.strong_target = document.getElementById("route-strong-target-select").value;
        payload.base_threshold = parseFloat(document.getElementById("route-base-thresh").value) || 0.5;
        payload.threshold_step = parseFloat(document.getElementById("route-step-thresh").value) || 0.1;
    } else if (type === "fallback") {
        const raw = document.getElementById("route-fallback-targets").value;
        payload.targets = raw.split(",").map(s => s.trim()).filter(Boolean);
    }

    try {
        const res = await fetch("/api/routing/route", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error(await res.text());
        closeModal("modal-route");
        showToast(`Route '${key}' erfolgreich gespeichert.`);
        await loadFullRoutingConfig();
    } catch (e) {
        showToast(`Fehler: ${e.message}`, true);
    }
}

async function deleteRoute(key) {
    if (!confirm(`Route '${key}' wirklich löschen?`)) return;
    try {
        const res = await fetch(`/api/routing/route/${key}`, { method: "DELETE" });
        if (!res.ok) throw new Error(await res.text());
        showToast(`Route '${key}' gelöscht.`);
        await loadFullRoutingConfig();
    } catch (e) {
        showToast(`Fehler: ${e.message}`, true);
    }
}

// --- Target Modal Management ---
function openCreateTargetModal() {
    document.getElementById("target-key-input").value = "";
    document.getElementById("target-id-input").value = "";
    openModal("modal-target");
}

async function submitTargetForm() {
    const key = document.getElementById("target-key-input").value.trim();
    const id = document.getElementById("target-id-input").value.trim();
    const client = document.getElementById("target-client-select").value;

    if (!key || !id) {
        showToast("Key und Upstream Model ID erforderlich", true);
        return;
    }

    try {
        const res = await fetch("/api/routing/target", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key, id, llm_client: client })
        });
        if (!res.ok) throw new Error(await res.text());
        closeModal("modal-target");
        showToast(`Target '${key}' gespeichert.`);
        await loadFullRoutingConfig();
    } catch (e) {
        showToast(`Fehler: ${e.message}`, true);
    }
}

async function deleteTarget(key) {
    if (!confirm(`Target '${key}' löschen?`)) return;
    try {
        const res = await fetch(`/api/routing/target/${key}`, { method: "DELETE" });
        if (!res.ok) throw new Error(await res.text());
        showToast(`Target '${key}' gelöscht.`);
        await loadFullRoutingConfig();
    } catch (e) {
        showToast(`Fehler: ${e.message}`, true);
    }
}

// --- Client Modal Management ---
function openCreateClientModal() {
    document.getElementById("client-key-input").value = "";
    document.getElementById("client-url-input").value = "";
    document.getElementById("client-env-input").value = "";
    openModal("modal-client");
}

async function submitClientForm() {
    const key = document.getElementById("client-key-input").value.trim();
    const format = document.getElementById("client-format-select").value;
    const url = document.getElementById("client-url-input").value.trim();
    const env = document.getElementById("client-env-input").value.trim();

    if (!key || !url) {
        showToast("Key und Base URL erforderlich", true);
        return;
    }

    try {
        const res = await fetch("/api/routing/client", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key, format, base_url: url, api_key_env: env || null })
        });
        if (!res.ok) throw new Error(await res.text());
        closeModal("modal-client");
        showToast(`Client '${key}' gespeichert.`);
        await loadFullRoutingConfig();
    } catch (e) {
        showToast(`Fehler: ${e.message}`, true);
    }
}

async function deleteClient(key) {
    if (!confirm(`Client '${key}' löschen?`)) return;
    try {
        const res = await fetch(`/api/routing/client/${key}`, { method: "DELETE" });
        if (!res.ok) throw new Error(await res.text());
        showToast(`Client '${key}' gelöscht.`);
        await loadFullRoutingConfig();
    } catch (e) {
        showToast(`Fehler: ${e.message}`, true);
    }
}

// --- Raw TOML Editor ---
async function loadRawToml() {
    const editor = document.getElementById("toml-code-editor");
    const status = document.getElementById("toml-status");
    try {
        const res = await fetch("/api/routing/raw");
        const data = await res.json();
        editor.value = data.content || "";
        status.className = "editor-footer ok";
        status.innerText = "Status: Konfiguration geladen";
    } catch (e) {
        status.className = "editor-footer err";
        status.innerText = `Fehler beim Laden: ${e.message}`;
    }
}

async function saveRawToml() {
    const editor = document.getElementById("toml-code-editor");
    const status = document.getElementById("toml-status");
    const content = editor.value;

    status.className = "editor-footer";
    status.innerText = "Validiere und speichere...";

    try {
        const res = await fetch("/api/routing/raw", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Validierungsfehler");

        status.className = "editor-footer ok";
        status.innerText = `✓ ${data.message} (Hot-Reload empfohlen)`;
        showToast("routes.toml erfolgreich gespeichert.");
        await loadFullRoutingConfig();
    } catch (e) {
        status.className = "editor-footer err";
        status.innerText = `❌ ${e.message}`;
        showToast(`Fehler: ${e.message}`, true);
    }
}

// --- Hot Reload Switchyard ---
async function triggerSwitchyardReload() {
    showToast("Lade Switchyard neu...");
    try {
        const res = await fetch("/api/routing/reload", { method: "POST" });
        const data = await res.json();
        if (data.success) {
            showToast("Switchyard erfolgreich neu geladen!");
        } else {
            showToast(`Reload Info: ${data.message || "OK"}`);
        }
    } catch (e) {
        showToast(`Fehler: ${e.message}`, true);
    }
}

// --- Route Tester / Playground ---
async function runRouteTest() {
    const route = document.getElementById("test-route-select").value;
    const prompt = document.getElementById("test-prompt").value.trim();
    const maxTokens = parseInt(document.getElementById("test-max-tokens").value) || 500;

    const btn = document.getElementById("btn-send-test");
    const output = document.getElementById("test-response-output");
    const statusVal = document.getElementById("test-status-val");
    const modelVal = document.getElementById("test-model-val");
    const latencyVal = document.getElementById("test-latency-val");
    const badge = document.getElementById("test-latency-badge");

    if (!prompt) {
        showToast("Bitte Prompt eingeben", true);
        return;
    }

    btn.disabled = true;
    btn.innerText = "⏳ Sende Anfrage...";
    output.innerText = "Warte auf Antwort von Switchyard...";
    statusVal.innerText = "--";
    statusVal.style.color = "";
    modelVal.innerText = "--";
    latencyVal.innerText = "--";

    try {
        const res = await fetch("/api/routing/test", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ route, prompt, max_tokens: maxTokens })
        });
        const data = await res.json();

        latencyVal.innerText = `${data.latency_ms} ms`;
        badge.innerText = `${data.latency_ms} ms`;

        if (data.success) {
            statusVal.innerText = `HTTP ${data.status_code} OK`;
            statusVal.style.color = "var(--success)";
            modelVal.innerText = data.routed_model;

            // Build response text
            let responseText = data.content || "(Keine Text-Antwort)";

            // Add truncation warning
            if (data.truncated) {
                responseText += "\n\n⚠️ ANTWORT ABGESCHNITTEN (finish_reason: length) — Erhöhe Max Tokens!";
            }

            // Add usage info
            if (data.usage) {
                const u = data.usage;
                const parts = [];
                if (u.prompt_tokens) parts.push(`Prompt: ${u.prompt_tokens}`);
                if (u.completion_tokens) parts.push(`Antwort: ${u.completion_tokens}`);
                if (u.total_tokens) parts.push(`Total: ${u.total_tokens}`);
                if (parts.length > 0) {
                    responseText += `\n\n📊 Token Usage: ${parts.join(" | ")}`;
                }
            }

            if (data.finish_reason) {
                responseText += `\n🏁 Finish: ${data.finish_reason}`;
            }

            output.innerText = responseText;
        } else {
            statusVal.innerText = `HTTP ${data.status_code || "ERR"}`;
            statusVal.style.color = "var(--danger)";
            modelVal.innerText = route;
            output.innerText = `Fehler: ${data.error}`;
        }
    } catch (e) {
        statusVal.innerText = "ERR";
        statusVal.style.color = "var(--danger)";
        output.innerText = `Netzwerkfehler: ${e.message}`;
    } finally {
        btn.disabled = false;
        btn.innerText = "🚀 Test-Anfrage senden";
    }
}

// --- Live Stats ---
async function loadSwitchyardStats() {
    try {
        const res = await fetch("/api/routing/stats");
        const data = await res.json();

        const badge = document.getElementById("stats-online-badge");
        badge.innerText = data.online ? "Online (:4000)" : "Offline";
        badge.style.color = data.online ? "var(--success)" : "var(--danger)";

        // Summary metrics
        const total = data.stats.total_requests || 0;
        const success = data.stats.successful_requests || 0;
        const errors = data.stats.failed_requests || 0;

        document.getElementById("stats-total-req").innerText = total;
        document.getElementById("stats-success-req").innerText = success;
        document.getElementById("stats-error-req").innerText = errors;

        // Active models catalog
        const modelsBody = document.getElementById("active-models-body");
        const models = data.active_models || [];
        if (models.length === 0) {
            modelsBody.innerHTML = `<tr><td colspan="3" class="muted">Keine Modelle gemeldet.</td></tr>`;
        } else {
            modelsBody.innerHTML = models.map(m => `
                <tr>
                    <td><span class="code-pill" style="color:var(--primary);">${m.id}</span></td>
                    <td>${m.owned_by || "switchyard"}</td>
                    <td><span class="status-pill online"><span class="status-dot"></span>Bereit</span></td>
                </tr>
            `).join("");
        }
    } catch (e) {
        console.error("Fehler beim Laden der Stats:", e);
    }
}

async function resetSwitchyardStats() {
    if (!confirm("Statistiken in Switchyard wirklich zurücksetzen?")) return;
    try {
        await fetch("/api/routing/stats/reset", { method: "POST" });
        showToast("Statistiken zurückgesetzt.");
        loadSwitchyardStats();
    } catch (e) {
        showToast(`Fehler: ${e.message}`, true);
    }
}

// --- MSI Afterburner Hardware Monitor Engine ---
let telemetryTimer = null;
let telemetryIntervalMs = 2000;
let historyMaxPoints = 60;

// Metric History Buffers
const monitorHistory = {
    gpuUtil: [],
    gpuTempEdge: [],
    gpuTempHotspot: [],
    gpuTempMem: [],
    gpuPower: [],
    gpuVramPct: [],
    gpuVramUsedMb: [],
    cpuUtil: [],
    ramPct: []
};

// Min / Max / Avg Stats Accumulator
const monitorStats = {
    gpuUtil: { min: null, max: null, sum: 0, count: 0 },
    gpuTemp: { min: null, max: null, sum: 0, count: 0 },
    gpuPower: { min: null, max: null, sum: 0, count: 0 },
    gpuVram: { min: null, max: null, sum: 0, count: 0 },
    cpuUtil: { min: null, max: null, sum: 0, count: 0 }
};

function pushMonitorStat(metricKey, val) {
    if (val === null || val === undefined || isNaN(val)) return;
    const st = monitorStats[metricKey];
    if (!st) return;
    if (st.min === null || val < st.min) st.min = val;
    if (st.max === null || val > st.max) st.max = val;
    st.sum += val;
    st.count += 1;
}

function resetMonitorStats() {
    for (const k in monitorStats) {
        monitorStats[k].min = null;
        monitorStats[k].max = null;
        monitorStats[k].sum = 0;
        monitorStats[k].count = 0;
    }
    showToast("Min/Max/Avg Statistiken zurückgesetzt.");
}

function changeTelemetryInterval(val) {
    const ms = parseInt(val, 10);
    telemetryIntervalMs = ms;
    startTelemetryPolling();
    if (ms === 0) {
        showToast("Hardware Monitor pausiert.");
    } else {
        showToast(`Intervall auf ${ms / 1000}s gesetzt.`);
    }
}

function changeHistoryWindow(val) {
    const sec = parseInt(val, 10);
    historyMaxPoints = sec;
    const rStart = document.getElementById("ruler-start");
    const rMid = document.getElementById("ruler-mid");
    if (rStart) rStart.innerText = `T - ${sec}s`;
    if (rMid) rMid.innerText = `T - ${Math.round(sec / 2)}s`;
    redrawAllAfterburnerCharts();
    showToast(`Historie auf ${sec}s eingestellt.`);
}

function startTelemetryPolling() {
    if (telemetryTimer) {
        clearInterval(telemetryTimer);
        telemetryTimer = null;
    }
    if (telemetryIntervalMs > 0) {
        telemetryTimer = setInterval(pollTelemetry, telemetryIntervalMs);
    }
}

async function pollTelemetry() {
    await loadSystemTelemetry();
}

// --- Telemetry & System Update ---
async function loadSystemTelemetry() {
    try {
        const res = await fetch("/api/system/metrics");
        if (!res.ok) return;
        const d = await res.json();

        // 1. Update Header Telemetry Pills
        const hCpu = document.getElementById("header-cpu");
        const hRam = document.getElementById("header-ram");
        const hGpu = document.getElementById("header-gpu");
        const hTemp = document.getElementById("header-temp");
        const hPower = document.getElementById("header-power");
        const hVram = document.getElementById("header-vram");

        if (hCpu) hCpu.innerText = `${d.cpu_percent}%`;
        if (hRam) hRam.innerText = `${d.ram_percent}%`;
        if (hGpu) hGpu.innerText = d.gpu_util !== null ? `${d.gpu_util}%` : "--%";
        if (hTemp) hTemp.innerText = d.gpu_temp !== null ? `${d.gpu_temp}°C` : "--°C";
        if (hPower) hPower.innerText = d.gpu_power !== null ? `${d.gpu_power} W` : "-- W";
        if (hVram) hVram.innerText = d.vram_percent !== null ? `${d.vram_percent}%` : "--%";

        // 2. Update MSI Afterburner Top HUD Blocks
        const hwName = document.getElementById("hw-gpu-model-name");
        if (hwName && d.gpu_name) hwName.innerText = d.gpu_name;

        // GPU Util HUD
        const gpuVal = d.gpu_util !== null ? d.gpu_util : 0;
        pushMonitorStat("gpuUtil", gpuVal);
        const hudGpuVal = document.getElementById("hud-gpu-val");
        const hudGpuMin = document.getElementById("hud-gpu-min");
        const hudGpuMax = document.getElementById("hud-gpu-max");
        const hudGpuAvg = document.getElementById("hud-gpu-avg");
        if (hudGpuVal) hudGpuVal.innerText = `${gpuVal}%`;
        if (hudGpuMin) hudGpuMin.innerText = `${monitorStats.gpuUtil.min || 0}%`;
        if (hudGpuMax) hudGpuMax.innerText = `${monitorStats.gpuUtil.max || 0}%`;
        if (hudGpuAvg) {
            const avg = monitorStats.gpuUtil.count > 0 ? (monitorStats.gpuUtil.sum / monitorStats.gpuUtil.count).toFixed(0) : 0;
            hudGpuAvg.innerText = `${avg}%`;
        }

        // GPU Temp HUD
        const tempVal = d.gpu_temp !== null ? d.gpu_temp : 0;
        pushMonitorStat("gpuTemp", tempVal);
        const hudTempVal = document.getElementById("hud-temp-val");
        const hudTempHotspot = document.getElementById("hud-temp-hotspot");
        const hudTempMem = document.getElementById("hud-temp-mem");
        const hudTempMax = document.getElementById("hud-temp-max");
        if (hudTempVal) hudTempVal.innerText = `${tempVal}°C`;
        if (hudTempHotspot) hudTempHotspot.innerText = `${d.gpu_temp_hotspot || tempVal}°C`;
        if (hudTempMem) hudTempMem.innerText = d.gpu_temp_mem ? `${d.gpu_temp_mem}°C` : "--";
        if (hudTempMax) hudTempMax.innerText = `${monitorStats.gpuTemp.max || 0}°C`;

        // GPU Power HUD
        const powerVal = d.gpu_power !== null ? d.gpu_power : 0;
        pushMonitorStat("gpuPower", powerVal);
        const hudPowerVal = document.getElementById("hud-power-val");
        const hudPowerCap = document.getElementById("hud-power-cap");
        const hudPowerMax = document.getElementById("hud-power-max");
        const hudPowerAvg = document.getElementById("hud-power-avg");
        if (hudPowerVal) hudPowerVal.innerText = `${powerVal} W`;
        if (hudPowerCap) hudPowerCap.innerText = d.gpu_power_cap ? `${d.gpu_power_cap} W` : "300 W";
        if (hudPowerMax) hudPowerMax.innerText = `${monitorStats.gpuPower.max || 0} W`;
        if (hudPowerAvg) {
            const avg = monitorStats.gpuPower.count > 0 ? (monitorStats.gpuPower.sum / monitorStats.gpuPower.count).toFixed(0) : 0;
            hudPowerAvg.innerText = `${avg} W`;
        }

        // GPU VRAM HUD
        const vramPct = d.vram_percent !== null ? d.vram_percent : 0;
        pushMonitorStat("gpuVram", vramPct);
        const hudVramVal = document.getElementById("hud-vram-val");
        const hudVramUsed = document.getElementById("hud-vram-used");
        const hudVramTotal = document.getElementById("hud-vram-total");
        if (hudVramVal) hudVramVal.innerText = `${vramPct}%`;
        if (hudVramUsed) hudVramUsed.innerText = d.vram_used_mb ? `${(d.vram_used_mb / 1024).toFixed(1)} GB` : "0 MB";
        if (hudVramTotal) hudVramTotal.innerText = d.vram_total_mb ? `${(d.vram_total_mb / 1024).toFixed(1)} GB` : "32 GB";

        // CPU & RAM HUD
        const cpuVal = d.cpu_percent !== null ? d.cpu_percent : 0;
        pushMonitorStat("cpuUtil", cpuVal);
        const hudCpuVal = document.getElementById("hud-cpu-val");
        const hudRamVal = document.getElementById("hud-ram-val");
        const hudCpuCores = document.getElementById("hud-cpu-cores");
        if (hudCpuVal) hudCpuVal.innerText = `${cpuVal}%`;
        if (hudRamVal) hudRamVal.innerText = `${d.ram_used_gb} / ${d.ram_total_gb} GB`;
        if (hudCpuCores) hudCpuCores.innerText = `${d.cpu_count} Kerne`;

        // 3. Append to Rolling History Buffers
        function pushHistory(arr, val) {
            arr.push(val);
            if (arr.length > historyMaxPoints) {
                arr.shift();
            }
        }

        pushHistory(monitorHistory.gpuUtil, gpuVal);
        pushHistory(monitorHistory.gpuTempEdge, tempVal);
        pushHistory(monitorHistory.gpuTempHotspot, d.gpu_temp_hotspot !== null ? d.gpu_temp_hotspot : tempVal);
        pushHistory(monitorHistory.gpuTempMem, d.gpu_temp_mem !== null ? d.gpu_temp_mem : tempVal);
        pushHistory(monitorHistory.gpuPower, powerVal);
        pushHistory(monitorHistory.gpuVramPct, vramPct);
        pushHistory(monitorHistory.gpuVramUsedMb, d.vram_used_mb || 0);
        pushHistory(monitorHistory.cpuUtil, cpuVal);
        pushHistory(monitorHistory.ramPct, d.ram_percent || 0);

        // 4. Update Channel Text Readouts
        // Channel 1: GPU
        const gGpuCur = document.getElementById("g-gpu-cur");
        const gGpuMin = document.getElementById("g-gpu-min");
        const gGpuMax = document.getElementById("g-gpu-max");
        const gGpuAvg = document.getElementById("g-gpu-avg");
        if (gGpuCur) gGpuCur.innerText = `${gpuVal} %`;
        if (gGpuMin) gGpuMin.innerText = `${monitorStats.gpuUtil.min || 0}`;
        if (gGpuMax) gGpuMax.innerText = `${monitorStats.gpuUtil.max || 0}`;
        if (gGpuAvg) {
            const avg = monitorStats.gpuUtil.count > 0 ? (monitorStats.gpuUtil.sum / monitorStats.gpuUtil.count).toFixed(0) : 0;
            gGpuAvg.innerText = `${avg}`;
        }

        // Channel 2: Temp
        const gTempCur = document.getElementById("g-temp-cur");
        const gTempMin = document.getElementById("g-temp-min");
        const gTempMax = document.getElementById("g-temp-max");
        const gTempHotspot = document.getElementById("g-temp-hotspot");
        if (gTempCur) gTempCur.innerText = `${tempVal} °C`;
        if (gTempMin) gTempMin.innerText = `${monitorStats.gpuTemp.min || 0}`;
        if (gTempMax) gTempMax.innerText = `${monitorStats.gpuTemp.max || 0}`;
        if (gTempHotspot) gTempHotspot.innerText = `${d.gpu_temp_hotspot || tempVal}`;

        // Channel 3: Power
        const gPwrCur = document.getElementById("g-pwr-cur");
        const gPwrMin = document.getElementById("g-pwr-min");
        const gPwrMax = document.getElementById("g-pwr-max");
        const gPwrCap = document.getElementById("g-pwr-cap");
        if (gPwrCur) gPwrCur.innerText = `${powerVal} W`;
        if (gPwrMin) gPwrMin.innerText = `${monitorStats.gpuPower.min || 0}`;
        if (gPwrMax) gPwrMax.innerText = `${monitorStats.gpuPower.max || 0}`;
        if (gPwrCap && d.gpu_power_cap) gPwrCap.innerText = `${d.gpu_power_cap}`;

        // Channel 4: VRAM
        const gVramCur = document.getElementById("g-vram-cur");
        const gVramMin = document.getElementById("g-vram-min");
        const gVramMax = document.getElementById("g-vram-max");
        const gVramTotal = document.getElementById("g-vram-total-info");
        if (gVramCur) gVramCur.innerText = d.vram_used_mb ? `${(d.vram_used_mb / 1024).toFixed(1)} GB (${vramPct}%)` : `${vramPct}%`;
        if (gVramMin) gVramMin.innerText = `${monitorStats.gpuVram.min || 0}`;
        if (gVramMax) gVramMax.innerText = `${monitorStats.gpuVram.max || 0}`;
        if (gVramTotal && d.vram_total_mb) gVramTotal.innerText = `Total: ${(d.vram_total_mb / 1024).toFixed(1)} GB`;

        // Channel 5: CPU
        const gCpuCur = document.getElementById("g-cpu-cur");
        const gCpuMin = document.getElementById("g-cpu-min");
        const gCpuMax = document.getElementById("g-cpu-max");
        const gRamStat = document.getElementById("g-ram-stat");
        if (gCpuCur) gCpuCur.innerText = `${cpuVal} %`;
        if (gCpuMin) gCpuMin.innerText = `${monitorStats.cpuUtil.min || 0}`;
        if (gCpuMax) gCpuMax.innerText = `${monitorStats.cpuUtil.max || 0}`;
        if (gRamStat) gRamStat.innerText = `${d.ram_percent}% (${d.ram_used_gb} GB)`;

        // 5. Redraw Canvas Charts
        redrawAllAfterburnerCharts(d);
    } catch (e) {
        console.error("Telemetry error:", e);
    }
}

// --- Canvas Chart Rendering Engine ---
function redrawAllAfterburnerCharts(latestData = {}) {
    // Channel 1: GPU Auslastung (0 - 100%)
    drawAfterburnerCanvas("chart-gpu-util", [
        {
            data: monitorHistory.gpuUtil,
            color: "#00e5ff",
            fillColor: "rgba(0, 229, 255, 0.18)",
            lineWidth: 2
        }
    ], { minVal: 0, maxVal: 100, unit: "%" });

    // Channel 2: GPU Temperatur (0 - 110 °C)
    drawAfterburnerCanvas("chart-gpu-temp", [
        {
            data: monitorHistory.gpuTempEdge,
            color: "#ff7043",
            fillColor: "rgba(255, 112, 67, 0.15)",
            lineWidth: 2
        },
        {
            data: monitorHistory.gpuTempHotspot,
            color: "#ff1744",
            lineWidth: 1.5,
            dashed: true
        }
    ], { minVal: 20, maxVal: 105, unit: "°C" });

    // Channel 3: GPU Leistung (0 - 380 W)
    const powerCap = latestData.gpu_power_cap || 300;
    const maxPowerScale = Math.max(powerCap + 50, 360);
    drawAfterburnerCanvas("chart-gpu-power", [
        {
            data: monitorHistory.gpuPower,
            color: "#ffca28",
            fillColor: "rgba(255, 202, 40, 0.18)",
            lineWidth: 2
        }
    ], {
        minVal: 0,
        maxVal: maxPowerScale,
        unit: "W",
        dashedLine: { val: powerCap, color: "rgba(255, 82, 82, 0.4)", label: "TDP" }
    });

    // Channel 4: GPU VRAM (0 - 100%)
    drawAfterburnerCanvas("chart-gpu-vram", [
        {
            data: monitorHistory.gpuVramPct,
            color: "#d500f9",
            fillColor: "rgba(213, 0, 249, 0.16)",
            lineWidth: 2
        }
    ], { minVal: 0, maxVal: 100, unit: "%" });

    // Channel 5: CPU & RAM (0 - 100%)
    drawAfterburnerCanvas("chart-cpu-util", [
        {
            data: monitorHistory.cpuUtil,
            color: "#00e676",
            fillColor: "rgba(0, 230, 118, 0.15)",
            lineWidth: 2
        },
        {
            data: monitorHistory.ramPct,
            color: "#2979ff",
            lineWidth: 1.5,
            dashed: true
        }
    ], { minVal: 0, maxVal: 100, unit: "%" });
}

function drawAfterburnerCanvas(canvasId, seriesList, opts = {}) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    const parent = canvas.parentElement;
    if (!parent) return;
    const rect = parent.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const displayWidth = rect.width > 50 ? rect.width : 600;
    const displayHeight = 80;

    if (canvas.width !== Math.floor(displayWidth * dpr) || canvas.height !== Math.floor(displayHeight * dpr)) {
        canvas.width = Math.floor(displayWidth * dpr);
        canvas.height = Math.floor(displayHeight * dpr);
        canvas.style.width = displayWidth + "px";
        canvas.style.height = displayHeight + "px";
    }

    const ctx = canvas.getContext("2d");
    ctx.resetTransform();
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, displayWidth, displayHeight);

    const padLeft = 8;
    const padRight = 8;
    const padTop = 6;
    const padBottom = 6;
    const chartW = displayWidth - padLeft - padRight;
    const chartH = displayHeight - padTop - padBottom;

    const minVal = opts.minVal !== undefined ? opts.minVal : 0;
    const maxVal = opts.maxVal !== undefined ? opts.maxVal : 100;
    const valRange = Math.max(maxVal - minVal, 1);

    // Background Grid lines
    ctx.strokeStyle = "rgba(255, 255, 255, 0.05)";
    ctx.lineWidth = 1;
    ctx.setLineDash([]);

    // 3 horizontal grid lines (25%, 50%, 75%)
    for (let i = 1; i <= 3; i++) {
        const y = padTop + (chartH * i) / 4;
        ctx.beginPath();
        ctx.moveTo(padLeft, y);
        ctx.lineTo(padLeft + chartW, y);
        ctx.stroke();
    }

    // Vertical time grid lines
    const vSteps = 6;
    for (let i = 1; i < vSteps; i++) {
        const x = padLeft + (chartW * i) / vSteps;
        ctx.beginPath();
        ctx.moveTo(x, padTop);
        ctx.lineTo(x, padTop + chartH);
        ctx.stroke();
    }

    // Dashed reference line (e.g. TDP Power Cap)
    if (opts.dashedLine && opts.dashedLine.val) {
        const capY = padTop + chartH - ((opts.dashedLine.val - minVal) / valRange) * chartH;
        if (capY >= padTop && capY <= padTop + chartH) {
            ctx.strokeStyle = opts.dashedLine.color || "rgba(255, 82, 82, 0.5)";
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 4]);
            ctx.beginPath();
            ctx.moveTo(padLeft, capY);
            ctx.lineTo(padLeft + chartW, capY);
            ctx.stroke();
            ctx.setLineDash([]);
        }
    }

    // Draw each series
    seriesList.forEach(series => {
        const data = series.data;
        if (!data || data.length === 0) return;

        const points = [];
        const stepX = chartW / Math.max(historyMaxPoints - 1, 1);
        const offsetPoints = Math.max(0, historyMaxPoints - data.length);

        for (let i = 0; i < data.length; i++) {
            const rawVal = data[i];
            const clampedVal = Math.max(minVal, Math.min(maxVal, rawVal));
            const x = padLeft + (offsetPoints + i) * stepX;
            const y = padTop + chartH - ((clampedVal - minVal) / valRange) * chartH;
            points.push({ x, y });
        }

        if (points.length === 0) return;

        // Area Gradient Fill under curve
        if (series.fillColor) {
            const grad = ctx.createLinearGradient(0, padTop, 0, padTop + chartH);
            grad.addColorStop(0, series.fillColor);
            grad.addColorStop(1, "rgba(0, 0, 0, 0)");

            ctx.beginPath();
            ctx.moveTo(points[0].x, padTop + chartH);
            points.forEach(p => ctx.lineTo(p.x, p.y));
            ctx.lineTo(points[points.length - 1].x, padTop + chartH);
            ctx.closePath();
            ctx.fillStyle = grad;
            ctx.fill();
        }

        // Line Stroke
        ctx.strokeStyle = series.color;
        ctx.lineWidth = series.lineWidth || 2;
        ctx.shadowColor = series.color;
        ctx.shadowBlur = 4;
        ctx.setLineDash(series.dashed ? [4, 4] : []);

        ctx.beginPath();
        points.forEach((p, idx) => {
            if (idx === 0) ctx.moveTo(p.x, p.y);
            else ctx.lineTo(p.x, p.y);
        });
        ctx.stroke();

        ctx.shadowBlur = 0;
        ctx.setLineDash([]);

        // Current leading pulse point
        const lastPt = points[points.length - 1];
        if (lastPt) {
            ctx.fillStyle = series.color;
            ctx.beginPath();
            ctx.arc(lastPt.x, lastPt.y, 3, 0, Math.PI * 2);
            ctx.fill();
        }
    });
}

// --- Container Management ---
async function loadContainers() {
    const list = document.getElementById("containers-list");
    try {
        const res = await fetch("/api/containers/");
        if (!res.ok) return;
        const containers = await res.json();

        if (containers.length === 0) {
            list.innerHTML = `<div class="muted">Keine LLM Container gefunden.</div>`;
            return;
        }

        list.innerHTML = containers.map(c => {
            const isRunning = c.status === "running";
            const statusClass = isRunning ? "running" : "stopped";
            const statusLabel = isRunning ? "RUNNING" : (c.status || "STOPPED").toUpperCase();
            
            // Truncate long image names
            let imgName = c.image || "";
            if (imgName.length > 35) imgName = imgName.substring(0, 32) + "…";
            
            // Memory info for running containers
            let memInfo = "";
            if (isRunning && c.memory_usage_mb) {
                memInfo = ` · ${c.memory_usage_mb} MB`;
            }

            return `
            <div class="container-card">
                <div class="c-info">
                    <span class="c-name">${c.name}</span>
                    <span class="c-status ${statusClass}">
                        <span class="c-status-dot"></span>${statusLabel}${memInfo}
                    </span>
                    <span style="font-size:10px; color:var(--text-dim); font-family:var(--font-mono);">${imgName}</span>
                </div>
                <div class="c-actions">
                    ${isRunning 
                        ? `<button class="btn-compact danger" onclick="toggleContainer('${c.name}', 'stop')">Stop</button>`
                        : `<button class="btn-compact primary" onclick="toggleContainer('${c.name}', 'start')">Start</button>`
                    }
                    <button class="btn-compact" onclick="toggleContainer('${c.name}', 'restart')" title="Neustarten">🔄</button>
                </div>
            </div>`;
        }).join("");
    } catch (e) {
        list.innerHTML = `<div class="muted">Fehler beim Laden der Container.</div>`;
    }
}

async function toggleContainer(name, action) {
    showToast(`${action.toUpperCase()} ${name}...`);
    try {
        const res = await fetch(`/api/containers/${name}/${action}`, { method: "POST" });
        const d = await res.json();
        showToast(d.message || `${name} ${action} ausgeführt.`);
        await loadContainers();
    } catch (e) {
        showToast(`Fehler: ${e.message}`, true);
    }
}

// --- Modals & Toast Helper ---
function openModal(id) {
    const m = document.getElementById(id);
    if (m) m.classList.remove("hidden");
}

function closeModal(id) {
    const m = document.getElementById(id);
    if (m) m.classList.add("hidden");
}

function showToast(msg, isError = false) {
    const t = document.getElementById("toast");
    if (!t) return;
    t.innerText = msg;
    t.className = `toast ${isError ? 'err' : 'ok'}`;
    t.classList.remove("hidden");
    setTimeout(() => {
        t.classList.add("hidden");
    }, 3000);
}
