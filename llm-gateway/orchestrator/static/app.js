document.addEventListener("DOMContentLoaded", () => {
    refreshAll();
    
    // Auto-refresh loops
    setInterval(fetchStatus, 5000);
    setInterval(fetchPools, 2000); // Fast refresh for queues
    setInterval(fetchEndpoints, 10000);
});

function refreshAll() {
    fetchStatus();
    fetchPools();
    fetchEndpoints();
}

// --------------------------------------------------------
// API Fetches
// --------------------------------------------------------

async function fetchStatus() {
    try {
        const res = await fetch("/api/status");
        if (!res.ok) throw new Error("Status API failed");
        const data = await res.json();
        
        // Update global metrics
        document.getElementById("metric-jobs").textContent = data.engine.total_jobs;
        document.getElementById("metric-streams").textContent = data.engine.active_streams;
        
        // Update system badge
        const badge = document.getElementById("system-badge-container");
        const statusText = document.getElementById("system-status");
        
        badge.className = `system-badge ${data.status === 'online' ? 'online' : 'offline'}`;
        statusText.textContent = `Router ${data.status.toUpperCase()}`;
        
    } catch (e) {
        const badge = document.getElementById("system-badge-container");
        badge.className = "system-badge offline";
        document.getElementById("system-status").textContent = "Router OFFLINE";
    }
}

async function fetchPools() {
    try {
        const res = await fetch("/v1/pools");
        const data = await res.json();
        renderPools(data.pools);
    } catch (e) {
        document.getElementById("pools-list").innerHTML = `<div class="status-msg">Fehler beim Laden der Pools</div>`;
    }
}

async function fetchEndpoints() {
    try {
        const res = await fetch("/api/endpoints/");
        const data = await res.json();
        renderEndpoints(data.endpoints);
    } catch (e) {
        document.getElementById("endpoints-list").innerHTML = `<div class="status-msg">Fehler beim Laden der Endpoints</div>`;
    }
}

// --------------------------------------------------------
// Render Functions
// --------------------------------------------------------

function renderPools(pools) {
    const list = document.getElementById("pools-list");
    const poolNames = Object.keys(pools);
    
    if (poolNames.length === 0) {
        list.innerHTML = `<div class="skeleton">Keine aktiven Routing-Pools (werden beim ersten WorkItem erstellt)</div>`;
        return;
    }
    
    let html = '';
    for (const name of poolNames) {
        const pool = pools[name];
        const isWorkerActive = pool.worker_active;
        
        // Calculate a visual bar fill (max 100 jobs for visual scale)
        const fillPercent = Math.min(100, Math.max(0, (pool.queue_length / 20) * 100));
        
        html += `
            <div class="pool-item">
                <div class="pool-header">
                    <div class="pool-name">${name}</div>
                    <div class="pool-worker">
                        <span class="worker-dot ${isWorkerActive ? 'active' : 'idle'}"></span>
                        ${isWorkerActive ? 'Worker Ready' : 'Idle'}
                    </div>
                </div>
                <div class="pool-metrics">
                    <div class="queue-count">${pool.queue_length}</div>
                    <div class="queue-label">Jobs in Queue</div>
                </div>
                <div class="pool-bar-bg">
                    <div class="pool-bar-fill" style="width: ${fillPercent}%"></div>
                </div>
            </div>
        `;
    }
    list.innerHTML = html;
}

function renderEndpoints(endpoints) {
    const list = document.getElementById("endpoints-list");
    
    if (!endpoints || endpoints.length === 0) {
        list.innerHTML = `<div class="skeleton">Keine Endpoints registriert.</div>`;
        return;
    }
    
    let html = '';
    for (const ep of endpoints) {
        // Build capability tags
        const capTags = ep.capabilities.map(c => `<span class="cap-tag">${c}</span>`).join('');
        
        // Build concurrency slots visualizer
        let slotPips = '';
        for (let i = 0; i < ep.max_concurrency; i++) {
            const isActive = i < ep.active_slots;
            slotPips += `<div class="slot-pip ${isActive ? 'active' : ''}"></div>`;
        }
        
        const lastCheck = ep.last_health_check ? new Date(ep.last_health_check).toLocaleTimeString() : 'N/A';
        const latencyStr = ep.last_latency_ms ? `${Math.round(ep.last_latency_ms)}ms` : '-';
        
        html += `
            <div class="ep-card">
                <div class="ep-header">
                    <div class="ep-title-area">
                        <h3>${ep.name || 'Unnamed Endpoint'}</h3>
                        <span class="ep-id">${ep.endpoint_id}</span>
                    </div>
                    <span class="ep-status ${ep.status}">${ep.status}</span>
                </div>
                
                <div class="ep-body">
                    <div class="ep-detail">
                        <span class="ep-detail-label">Model:</span>
                        <span class="ep-detail-val">${ep.model_name || 'N/A'}</span>
                    </div>
                    <div class="ep-detail">
                        <span class="ep-detail-label">API Schema:</span>
                        <span class="ep-detail-val">${ep.api_schema || 'N/A'}</span>
                    </div>
                    <div class="ep-detail">
                        <span class="ep-detail-label">Base URL:</span>
                        <span class="ep-detail-val" style="font-family: monospace; font-size: 0.8rem; color: #a5b4fc;">${ep.base_url || 'N/A'}</span>
                    </div>
                    <div class="ep-detail">
                        <span class="ep-detail-label">Type / Priority:</span>
                        <span class="ep-detail-val">${ep.type || 'N/A'} / P${ep.priority || '-'}</span>
                    </div>
                    <div class="ep-detail">
                        <span class="ep-detail-label">Latency / Failures:</span>
                        <span class="ep-detail-val">${latencyStr} / ${ep.consecutive_failures}</span>
                    </div>
                    <div class="ep-detail" style="flex-direction: column; gap: 4px;">
                        <span class="ep-detail-label">Capabilities:</span>
                        <div class="ep-caps">${capTags}</div>
                    </div>
                </div>
                
                <div class="ep-footer">
                    <div class="slots-indicator" title="${ep.active_slots} / ${ep.max_concurrency} Slots active">
                        ${slotPips}
                    </div>
                    <div style="display: flex; gap: 4px;">
                        ${ep.type === 'managed' ? `<button class="btn-ghost" style="padding: 4px 8px; font-size: 0.8rem;" onclick="restartEndpoint('${ep.endpoint_id}')" title="Docker Container neustarten">Restart</button>` : ''}
                        <button class="btn-delete-ep" onclick="deleteEndpoint('${ep.endpoint_id}')" title="Endpoint entfernen">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                        </button>
                    </div>
                </div>
            </div>
        `;
    }
    list.innerHTML = html;
}

// --------------------------------------------------------
// Add Endpoint Modal
// --------------------------------------------------------

function openEndpointModal() {
    document.getElementById("endpoint-modal").classList.remove("hidden");
    document.getElementById("endpoint-status-msg").textContent = "";
    loadHistoryPresets();
    toggleDockerDropdown();
}

function closeEndpointModal() {
    document.getElementById("endpoint-modal").classList.add("hidden");
}

async function saveEndpoint() {
    const epId = document.getElementById("ep-id").value;
    const name = document.getElementById("ep-name").value;
    const schema = document.getElementById("ep-schema").value;
    const url = document.getElementById("ep-url").value;
    const capsRaw = document.getElementById("ep-caps").value;
    const conc = parseInt(document.getElementById("ep-concurrency").value, 10);
    const prio = parseInt(document.getElementById("ep-priority").value, 10);
    const type = document.getElementById("ep-type").value;
    const dockerContainer = document.getElementById("ep-docker-container").value;
    const statusMsg = document.getElementById("endpoint-status-msg");
    
    if (!epId || !url) {
        statusMsg.textContent = "Endpoint ID und URL sind Pflichtfelder.";
        return;
    }
    
    if (type === 'managed' && !dockerContainer) {
        statusMsg.textContent = "Bitte einen Docker Container aus der Whitelist auswählen.";
        return;
    }
    
    const caps = capsRaw.split(',').map(s => s.trim()).filter(s => s.length > 0);
    
    const payload = {
        endpoint_id: epId,
        name: name,
        type: type,
        docker_container: type === 'managed' ? dockerContainer : null,
        base_url: url,
        api_schema: schema,
        max_concurrency: isNaN(conc) ? 2 : conc,
        capabilities: caps,
        priority: isNaN(prio) ? 5 : prio,
        health_check: {
            url: url.endsWith("/v1") ? url.replace("/v1", "/health") : `${url}/api/tags`,
            interval_seconds: 10,
            timeout_seconds: 3.0
        }
    };
    
    const btn = document.getElementById("btn-save-endpoint");
    btn.disabled = true;
    statusMsg.textContent = "Registriere Endpoint...";
    
    try {
        const res = await fetch("/api/endpoints/", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        if (res.ok) {
            saveEndpointToHistory(payload);
            closeEndpointModal();
            fetchEndpoints();
        } else {
            const err = await res.json();
            statusMsg.textContent = "Fehler: " + (err.detail || "Unbekannt");
        }
    } catch (e) {
        statusMsg.textContent = "Verbindungsfehler zur API.";
    } finally {
        btn.disabled = false;
    }
}

async function deleteEndpoint(epId) {
    if (!confirm(`Endpoint '${epId}' wirklich entfernen?`)) return;
    
    try {
        const res = await fetch(`/api/endpoints/${epId}`, { method: "DELETE" });
        if (res.ok) {
            fetchEndpoints();
        } else {
            alert("Fehler beim Löschen des Endpoints.");
        }
    } catch (e) {
        alert("API nicht erreichbar.");
    }
}

async function restartEndpoint(epId) {
    if (!confirm(`Docker Container für Endpoint '${epId}' wirklich neu starten? Das kann eine Weile dauern.`)) return;
    try {
        const res = await fetch(`/api/endpoints/${epId}/restart`, { method: "POST" });
        const data = await res.json();
        if (res.ok) {
            alert(data.message);
            fetchEndpoints();
        } else {
            alert("Fehler beim Neustart: " + (data.detail || "Unbekannt"));
        }
    } catch (e) {
        alert("API nicht erreichbar.");
    }
}

// --------------------------------------------------------
// Docker Whitelist & History (Phase 4)
// --------------------------------------------------------

function toggleDockerDropdown() {
    const type = document.getElementById("ep-type").value;
    const row = document.getElementById("docker-container-row");
    const select = document.getElementById("ep-docker-container");
    
    if (type === 'managed') {
        row.classList.remove('hidden');
        // Load whitelist
        let whitelist = [];
        try { whitelist = JSON.parse(localStorage.getItem("docker_whitelist")) || []; } catch(e){}
        
        select.innerHTML = '<option value="">-- Container wählen --</option>';
        if (whitelist.length === 0) {
            select.innerHTML += '<option value="" disabled>Keine Container in Whitelist (siehe Docker Pool)</option>';
        } else {
            whitelist.forEach(containerId => {
                select.innerHTML += `<option value="${containerId}">${containerId}</option>`;
            });
        }
    } else {
        row.classList.add('hidden');
    }
}

async function openDockerPoolModal() {
    document.getElementById("docker-pool-modal").classList.remove("hidden");
    const container = document.getElementById("docker-list-container");
    container.innerHTML = '<div class="skeleton">Lade Host-Container...</div>';
    
    try {
        const res = await fetch('/api/system/containers');
        const data = await res.json();
        
        let whitelist = [];
        try { whitelist = JSON.parse(localStorage.getItem("docker_whitelist")) || []; } catch(e){}
        
        let html = '';
        data.containers.forEach(c => {
            const isChecked = whitelist.includes(c.name) ? 'checked' : '';
            html += `
                <div style="display: flex; align-items: center; gap: 10px; padding: 8px; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <input type="checkbox" id="chk-${c.name}" value="${c.name}" class="docker-chk" ${isChecked}>
                    <label for="chk-${c.name}" style="flex:1; cursor:pointer;">
                        <strong>${c.name}</strong> <span style="color:var(--text-secondary); font-size:0.8rem;">(${c.image})</span>
                        <div style="font-size:0.75rem; color:${c.status.includes('Up') ? 'var(--success)' : 'var(--danger)'};">${c.status}</div>
                    </label>
                    <button class="btn-ghost" style="padding: 4px 8px; font-size: 0.8rem; color: var(--accent-primary);" onclick="addDockerAsEndpoint('${c.name}')">➕ Als Endpoint anlegen</button>
                </div>
            `;
        });
        container.innerHTML = html || 'Keine Docker Container gefunden.';
    } catch (e) {
        container.innerHTML = 'Fehler beim Laden der Docker Container.';
    }
}

function addDockerAsEndpoint(containerName) {
    // Check if it's already in the whitelist
    const checkbox = document.getElementById(`chk-${containerName}`);
    if (checkbox) checkbox.checked = true;
    saveDockerWhitelist(); // This saves and closes the modal
    
    openEndpointModal();
    
    document.getElementById("ep-id").value = containerName;
    document.getElementById("ep-name").value = containerName;
    document.getElementById("ep-type").value = "managed";
    
    // Guess a default URL (often port 8000 or 11434 for LLMs)
    let guessedPort = "8000";
    if (containerName.includes("ollama")) guessedPort = "11434";
    if (containerName.includes("lmstudio")) guessedPort = "1234";
    document.getElementById("ep-url").value = `http://${containerName}:${guessedPort}/v1`;
    
    toggleDockerDropdown();
    document.getElementById("ep-docker-container").value = containerName;
}

function closeDockerPoolModal() {
    document.getElementById("docker-pool-modal").classList.add("hidden");
}

function saveDockerWhitelist() {
    const checkboxes = document.querySelectorAll('.docker-chk');
    const whitelist = [];
    checkboxes.forEach(chk => {
        if (chk.checked) whitelist.push(chk.value);
    });
    localStorage.setItem("docker_whitelist", JSON.stringify(whitelist));
    closeDockerPoolModal();
    // Update the dropdown if Add Endpoint modal is open
    if (!document.getElementById("endpoint-modal").classList.contains("hidden")) {
        toggleDockerDropdown();
    }
}

function saveEndpointToHistory(payload) {
    let history = [];
    try { history = JSON.parse(localStorage.getItem("endpoint_history")) || []; } catch(e){}
    
    // Remove if exists with same ID to update
    history = history.filter(h => h.endpoint_id !== payload.endpoint_id);
    history.unshift(payload);
    
    // Keep last 10
    if (history.length > 10) history.pop();
    
    localStorage.setItem("endpoint_history", JSON.stringify(history));
}

function loadHistoryPresets() {
    const select = document.getElementById("ep-history");
    let history = [];
    try { history = JSON.parse(localStorage.getItem("endpoint_history")) || []; } catch(e){}
    
    if (history.length === 0) {
        select.innerHTML = '<option value="">-- Historie leer --</option>';
        return;
    }
    
    select.innerHTML = '<option value="">-- Konfiguration wählen --</option>';
    history.forEach((ep, index) => {
        select.innerHTML += `<option value="${index}">${ep.name || ep.endpoint_id} (${ep.type})</option>`;
    });
}

function applyHistoryPreset() {
    const select = document.getElementById("ep-history");
    if (!select.value) return;
    
    let history = [];
    try { history = JSON.parse(localStorage.getItem("endpoint_history")) || []; } catch(e){}
    
    const ep = history[parseInt(select.value, 10)];
    if (!ep) return;
    
    document.getElementById("ep-id").value = ep.endpoint_id || '';
    document.getElementById("ep-name").value = ep.name || '';
    document.getElementById("ep-schema").value = ep.api_schema || 'openai';
    document.getElementById("ep-url").value = ep.base_url || '';
    document.getElementById("ep-caps").value = (ep.capabilities || []).join(', ');
    document.getElementById("ep-concurrency").value = ep.max_concurrency || 2;
    document.getElementById("ep-priority").value = ep.priority || 5;
    document.getElementById("ep-type").value = ep.type || 'unmanaged';
    
    toggleDockerDropdown();
    
    if (ep.type === 'managed' && ep.docker_container) {
        document.getElementById("ep-docker-container").value = ep.docker_container;
    }
}

// --------------------------------------------------------
// Quick Docker Add Modal Handlers
// --------------------------------------------------------

let availableDockerContainers = [];

async function openDockerAddModal() {
    document.getElementById("docker-add-modal").classList.remove("hidden");
    document.getElementById("quick-docker-status-msg").textContent = "";
    const select = document.getElementById("quick-docker-select");
    select.innerHTML = '<option value="">Lade Container...</option>';

    try {
        const res = await fetch('/api/system/containers');
        const data = await res.json();
        availableDockerContainers = data.containers || [];
        
        let whitelist = [];
        try { whitelist = JSON.parse(localStorage.getItem("docker_whitelist")) || []; } catch(e){}

        // Prioritize whitelisted containers, but show all if whitelist empty
        let listToShow = availableDockerContainers;
        if (whitelist.length > 0) {
            listToShow = availableDockerContainers.filter(c => whitelist.includes(c.name));
            if (listToShow.length === 0) listToShow = availableDockerContainers;
        }

        select.innerHTML = '<option value="">-- Container auswählen --</option>';
        listToShow.forEach(c => {
            select.innerHTML += `<option value="${c.name}">${c.name} (${c.status})</option>`;
        });

        if (listToShow.length > 0) {
            select.selectedIndex = 1;
            onQuickDockerSelected();
        }
    } catch (e) {
        select.innerHTML = '<option value="">Fehler beim Laden</option>';
    }
}

function closeDockerAddModal() {
    document.getElementById("docker-add-modal").classList.add("hidden");
}

function onQuickDockerSelected() {
    const name = document.getElementById("quick-docker-select").value;
    if (!name) return;

    document.getElementById("quick-ep-name").value = name;
    
    let guessedPort = "8000";
    let schema = "openai";
    let caps = "fast, reasoning";
    
    if (name.includes("ollama")) {
        guessedPort = "11434";
        schema = "ollama";
        caps = "fast, embeddings";
    } else if (name.includes("lmstudio") || name.includes("lm-studio")) {
        guessedPort = "1234";
        schema = "openai";
        caps = "fast, reasoning";
    } else if (name.includes("vllm")) {
        guessedPort = "8100";
        schema = "openai";
        caps = "fast, reasoning, coding";
    }

    document.getElementById("quick-ep-url").value = `http://${name}:${guessedPort}/v1`;
    document.getElementById("quick-ep-schema").value = schema;
    document.getElementById("quick-ep-caps").value = caps;
}

async function saveQuickDockerEndpoint() {
    const containerName = document.getElementById("quick-docker-select").value;
    const name = document.getElementById("quick-ep-name").value;
    const url = document.getElementById("quick-ep-url").value;
    const schema = document.getElementById("quick-ep-schema").value;
    const capsRaw = document.getElementById("quick-ep-caps").value;
    const statusMsg = document.getElementById("quick-docker-status-msg");

    if (!containerName || !url) {
        statusMsg.textContent = "Bitte Container und URL prüfen.";
        return;
    }

    const caps = capsRaw.split(',').map(s => s.trim()).filter(s => s.length > 0);

    const payload = {
        endpoint_id: containerName,
        name: name || containerName,
        type: "managed",
        docker_container: containerName,
        base_url: url,
        api_schema: schema,
        max_concurrency: 2,
        capabilities: caps,
        priority: 3,
        health_check: {
            url: url.endsWith("/v1") ? url.replace("/v1", "/health") : `${url}/api/tags`,
            interval_seconds: 10,
            timeout_seconds: 3.0
        }
    };

    const btn = document.getElementById("btn-quick-add");
    btn.disabled = true;
    statusMsg.textContent = "Registriere Endpoint...";

    try {
        const res = await fetch("/api/endpoints/", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (res.ok) {
            saveEndpointToHistory(payload);
            closeDockerAddModal();
            fetchEndpoints();
        } else {
            const err = await res.json();
            statusMsg.textContent = "Fehler: " + (err.detail || "Unbekannt");
        }
    } catch (e) {
        statusMsg.textContent = "Verbindungsfehler zur API.";
    } finally {
        btn.disabled = false;
    }
}

