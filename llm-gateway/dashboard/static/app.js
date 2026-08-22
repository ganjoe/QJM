// ==========================================================================
// QJM Control Plane - Dashboard Application Logic
// ==========================================================================

document.addEventListener("DOMContentLoaded", () => {
    refreshDashboard();
    // Poll stats every 5 seconds
    setInterval(refreshDashboard, 5000);
});

async function refreshDashboard() {
    await Promise.allSettled([
        fetchSystemStats(),
        fetchContainers(),
        fetchSwitchyardRoutes(),
    ]);
}

// --------------------------------------------------------------------------
// 1. Hardware & System Telemetry
// --------------------------------------------------------------------------
async function fetchSystemStats() {
    try {
        const res = await fetch("/api/system/stats");
        if (!res.ok) return;
        const stats = await res.json();

        // Header Metrics
        document.getElementById("header-cpu").textContent = `${stats.cpu_percent}%`;
        document.getElementById("header-ram").textContent = `${stats.memory_percent}%`;

        // Card Stats
        document.getElementById("stat-cpu-val").textContent = `${stats.cpu_percent}%`;
        document.getElementById("stat-cpu-bar").style.width = `${Math.min(stats.cpu_percent, 100)}%`;
        document.getElementById("stat-cpu-cores").textContent = `${stats.cpu_cores} CPU Kerne`;

        document.getElementById("stat-ram-val").textContent = `${stats.memory_used_gb} GB`;
        document.getElementById("stat-ram-bar").style.width = `${Math.min(stats.memory_percent, 100)}%`;
        document.getElementById("stat-ram-sub").textContent = `${stats.memory_used_gb} / ${stats.memory_total_gb} GB (${stats.memory_percent}%)`;

        // GPU / VRAM
        const gpu = stats.gpu;
        if (gpu && gpu.vram_total_mb) {
            document.getElementById("stat-vram-val").textContent = `${gpu.vram_used_mb || 0} MB`;
            document.getElementById("stat-vram-bar").style.width = `${gpu.vram_percent || 0}%`;
            document.getElementById("stat-vram-sub").textContent = `${gpu.name} (${gpu.vram_used_mb || 0} / ${gpu.vram_total_mb} MB - ${gpu.vram_percent || 0}%)`;
            document.getElementById("header-vram").textContent = `${gpu.vram_percent || 0}%`;
        } else {
            document.getElementById("stat-vram-val").textContent = "Aktiv";
            document.getElementById("stat-vram-sub").textContent = "AMD Radeon GPU (DRI/Vulkan)";
            document.getElementById("header-vram").textContent = "GPU OK";
        }
    } catch (e) {
        console.error("Failed to fetch system stats:", e);
    }
}

// --------------------------------------------------------------------------
// 2. Docker Containers & Actions
// --------------------------------------------------------------------------
async function fetchContainers() {
    const listEl = document.getElementById("containers-list");
    try {
        const res = await fetch("/api/containers");
        if (!res.ok) throw new Error("API returned " + res.status);
        const containers = await res.json();

        if (!containers || containers.length === 0) {
            listEl.innerHTML = '<div class="skeleton">Keine Docker Container gefunden.</div>';
            return;
        }

        listEl.innerHTML = containers.map(c => {
            const isRunning = c.status === "running";
            const statusClass = isRunning ? "running" : "stopped";
            const memText = isRunning && c.memory_usage_mb ? `${c.memory_usage_mb} MB (${c.memory_percent || 0}%)` : "0 MB";

            let friendlyName = c.name.replace("llm-gw-", "");
            if (friendlyName === "switchyard") friendlyName = "Switchyard Router";
            if (friendlyName === "dsh") friendlyName = "DeepSeek Harness (Agent)";
            if (friendlyName === "lmstudio") friendlyName = "LM Studio (GPU)";
            if (friendlyName === "orchestrator") friendlyName = "Orchestrator (Control Plane)";

            return `
                <div class="container-card">
                    <div class="container-head">
                        <div>
                            <div class="container-name">${friendlyName}</div>
                            <div class="container-image">${c.image}</div>
                        </div>
                        <span class="status-pill ${statusClass}">${isRunning ? "Aktiv" : "Gestoppt"}</span>
                    </div>
                    <div class="container-meta">
                        <span>RAM: ${memText}</span>
                        <span>${c.ports && c.ports.length > 0 ? c.ports[0] : ""}</span>
                    </div>
                    <div class="container-actions">
                        ${isRunning ? `
                            <button class="btn-danger" onclick="containerAction('${c.name}', 'stop')" title="Container stoppen und VRAM freigeben">
                                ⏹ Stoppen (VRAM frei)
                            </button>
                            <button class="btn-secondary" onclick="containerAction('${c.name}', 'restart')">
                                🔄 Neustart
                            </button>
                        ` : `
                            <button class="btn-success" onclick="containerAction('${c.name}', 'start')">
                                ▶ Starten
                            </button>
                        `}
                    </div>
                </div>
            `;
        }).join("");
    } catch (e) {
        listEl.innerHTML = `<div class="skeleton">Fehler beim Laden der Container: ${e.message}</div>`;
    }
}

async function containerAction(containerName, action) {
    showToast(`Führe ${action} für ${containerName} aus...`);
    try {
        const res = await fetch(`/api/containers/${containerName}/${action}`, {
            method: "POST",
        });
        const data = await res.json();
        if (res.ok) {
            showToast(`✅ ${data.message || "Aktion erfolgreich ausgeführt"}`);
            refreshDashboard();
        } else {
            showToast(`❌ Fehler: ${data.detail || "Aktion fehlgeschlagen"}`);
        }
    } catch (e) {
        showToast(`❌ Netzwerkfehler: ${e.message}`);
    }
}

// --------------------------------------------------------------------------
// 3. Switchyard Routes
// --------------------------------------------------------------------------
async function fetchSwitchyardRoutes() {
    const listEl = document.getElementById("routes-list");
    try {
        const res = await fetch("/api/routing/");
        if (!res.ok) throw new Error("API returned " + res.status);
        const data = await res.json();
        const routes = data.routes || [];

        if (routes.length === 0) {
            listEl.innerHTML = '<div class="skeleton">Keine Routen in Switchyard konfiguriert.</div>';
            return;
        }

        listEl.innerHTML = routes.map(r => {
            return `
                <div class="route-card">
                    <div class="route-name">⚡ ${r.tier || r.key}</div>
                    <div class="route-type">Typ: ${r.type || "passthrough"}</div>
                    <div class="route-target">Ziel: ${r.target || (r.classifier_target ? `Classifier (${r.classifier_target})` : "-")}</div>
                </div>
            `;
        }).join("");
    } catch (e) {
        listEl.innerHTML = `<div class="skeleton">Fehler beim Laden der Routen: ${e.message}</div>`;
    }
}

// --------------------------------------------------------------------------
// Toast Notification
// --------------------------------------------------------------------------
let toastTimer = null;
function showToast(msg) {
    const toastEl = document.getElementById("toast");
    toastEl.textContent = msg;
    toastEl.classList.remove("hidden");

    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        toastEl.classList.add("hidden");
    }, 4000);
}
