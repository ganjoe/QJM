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
    setInterval(pollTelemetry, 3000);
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

// --- Telemetry & System ---
async function loadSystemTelemetry() {
    try {
        const res = await fetch("/api/system/metrics");
        if (!res.ok) return;
        const d = await res.json();

        // Header pills
        document.getElementById("header-cpu").innerText = `${d.cpu_percent}%`;
        document.getElementById("header-ram").innerText = `${d.ram_percent}%`;
        if (d.vram_percent !== undefined) {
            document.getElementById("header-vram").innerText = `${d.vram_percent}%`;
        }

        // Hardware tab
        const cpuVal = document.getElementById("stat-cpu-val");
        const cpuBar = document.getElementById("stat-cpu-bar");
        const cpuCores = document.getElementById("stat-cpu-cores");
        if (cpuVal) cpuVal.innerText = `${d.cpu_percent}%`;
        if (cpuBar) cpuBar.style.width = `${d.cpu_percent}%`;
        if (cpuCores) cpuCores.innerText = `${d.cpu_count} Kerne`;

        const ramVal = document.getElementById("stat-ram-val");
        const ramBar = document.getElementById("stat-ram-bar");
        const ramSub = document.getElementById("stat-ram-sub");
        if (ramVal) ramVal.innerText = `${d.ram_used_gb} GB`;
        if (ramBar) ramBar.style.width = `${d.ram_percent}%`;
        if (ramSub) ramSub.innerText = `${d.ram_used_gb} / ${d.ram_total_gb} GB (${d.ram_percent}%)`;

        const vramVal = document.getElementById("stat-vram-val");
        const vramBar = document.getElementById("stat-vram-bar");
        const vramSub = document.getElementById("stat-vram-sub");
        if (vramVal) vramVal.innerText = d.vram_used_mb ? `${d.vram_used_mb} MB` : "--";
        if (vramBar) vramBar.style.width = `${d.vram_percent || 0}%`;
        if (vramSub) vramSub.innerText = d.vram_total_mb ? `${d.vram_used_mb} / ${d.vram_total_mb} MB` : "AMD GPU";
    } catch (e) {
        console.error("Telemetry error:", e);
    }
}

async function pollTelemetry() {
    loadSystemTelemetry();
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
