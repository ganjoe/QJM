// Shelly PowerMeter Dashboard Controller
let currentHandle = "server-plug";
let ws = null;
let chartInstance = null;
let currentRange = "live";
let selectedPowerCycleSeconds = 5;

// DOM Elements
const deviceSelect = document.getElementById("deviceSelect");
const statusPill = document.getElementById("statusPill");
const statusText = document.getElementById("statusText");
const valWatts = document.getElementById("valWatts");
const valVoltage = document.getElementById("valVoltage");
const valCurrent = document.getElementById("valCurrent");
const valKwh = document.getElementById("valKwh");
const valCost = document.getElementById("valCost");
const valTemp = document.getElementById("valTemp");
const switchBtn = document.getElementById("switchBtn");
const switchStatusText = document.getElementById("switchStatusText");

// Initialize
document.addEventListener("DOMContentLoaded", async () => {
  await loadDevices();
  await loadPricing();
  setupRangeButtons();
  setupPowerCycleButtons();
  initChart();
  connectWebSocket();
  loadHistory(currentRange);
});

// Load Devices
async function loadDevices() {
  try {
    const res = await fetch("/api/devices");
    const devices = await res.json();
    deviceSelect.innerHTML = "";
    devices.forEach(d => {
      const opt = document.createElement("option");
      opt.value = d.handle;
      opt.textContent = `${d.name} (${d.handle})`;
      if (d.handle === currentHandle) opt.selected = true;
      deviceSelect.appendChild(opt);
    });

    deviceSelect.addEventListener("change", (e) => {
      currentHandle = e.target.value;
      if (ws) ws.close();
      connectWebSocket();
      loadHistory(currentRange);
    });
  } catch (err) {
    console.error("Failed to load devices:", err);
  }
}

// WebSocket Live Stream
function connectWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws/live/${currentHandle}`;
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    statusPill.classList.remove("offline");
    statusText.textContent = "ONLINE (1s Live)";
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      updateLiveMetrics(data);
    } catch (e) {
      console.error("WS Parse Error:", e);
    }
  };

  ws.onclose = () => {
    statusPill.classList.add("offline");
    statusText.textContent = "OFFLINE";
    setTimeout(connectWebSocket, 3000);
  };
}

// Update Live UI
function updateLiveMetrics(data) {
  valWatts.textContent = data.watts.toFixed(1);
  valVoltage.textContent = data.voltage.toFixed(1);
  valCurrent.textContent = data.current.toFixed(2);
  valTemp.textContent = data.temp_c.toFixed(1);

  // Relay Switch
  if (data.output) {
    switchBtn.className = "btn-toggle on";
    switchBtn.innerHTML = "<span>⚡</span> EINGESCHALTET";
    switchStatusText.textContent = "Relais ist geschlossen (Strom fließt)";
  } else {
    switchBtn.className = "btn-toggle off";
    switchBtn.innerHTML = "<span>⚪</span> AUSGESCHALTET";
    switchStatusText.textContent = "Relais ist offen (Strom unterbrochen)";
  }

  // Live Chart Update if in 'live' mode
  if (currentRange === "live" && chartInstance) {
    const timeLabel = new Date(data.ts).toLocaleTimeString();
    chartInstance.data.labels.push(timeLabel);
    chartInstance.data.datasets[0].data.push(data.watts);

    // Letzte 60 Punkte im Live-Modus
    if (chartInstance.data.labels.length > 60) {
      chartInstance.data.labels.shift();
      chartInstance.data.datasets[0].data.shift();
    }
    chartInstance.update("none");
  }
}

// Relay Toggle
switchBtn.addEventListener("click", async () => {
  const isCurrentlyOn = switchBtn.classList.contains("on");
  const newState = !isCurrentlyOn;
  try {
    const res = await fetch(`/api/devices/${currentHandle}/switch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on: newState })
    });
    if (!res.ok) alert("Fehler beim Schalten des Relais!");
  } catch (err) {
    alert("Netzwerkfehler: " + err.message);
  }
});

// Power Cycle Controls
function setupPowerCycleButtons() {
  const presetBtns = document.querySelectorAll(".btn-preset");
  const customInput = document.getElementById("customRestartSeconds");

  presetBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      presetBtns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      selectedPowerCycleSeconds = parseInt(btn.dataset.seconds);
      customInput.value = selectedPowerCycleSeconds;
    });
  });

  customInput.addEventListener("input", (e) => {
    presetBtns.forEach(b => b.classList.remove("active"));
    selectedPowerCycleSeconds = parseInt(e.target.value) || 5;
  });
}

// Modal Handling
const powerCycleModal = document.getElementById("powerCycleModal");
const triggerPowerCycleBtn = document.getElementById("triggerPowerCycleBtn");
const cancelModalBtn = document.getElementById("cancelModalBtn");
const confirmPowerCycleBtn = document.getElementById("confirmPowerCycleBtn");
const modalSecondsSpan = document.getElementById("modalSecondsSpan");

triggerPowerCycleBtn.addEventListener("click", () => {
  modalSecondsSpan.textContent = selectedPowerCycleSeconds;
  powerCycleModal.classList.add("open");
});

cancelModalBtn.addEventListener("click", () => {
  powerCycleModal.classList.remove("open");
});

confirmPowerCycleBtn.addEventListener("click", async () => {
  powerCycleModal.classList.remove("open");
  try {
    const res = await fetch(`/api/devices/${currentHandle}/power-cycle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ restart_delay_seconds: selectedPowerCycleSeconds })
    });
    const result = await res.json();
    alert(`✅ ${result.message}`);
  } catch (err) {
    alert("Fehler beim Auslösen des Power-Cycles: " + err.message);
  }
});

// Chart.js Setup
function initChart() {
  const ctx = document.getElementById("historyChart").getContext("2d");
  
  const gradient = ctx.createLinearGradient(0, 0, 0, 300);
  gradient.addColorStop(0, "rgba(0, 229, 255, 0.35)");
  gradient.addColorStop(1, "rgba(0, 229, 255, 0.0)");

  chartInstance = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [{
        label: "Leistung (Watt)",
        data: [],
        borderColor: "#00e5ff",
        borderWidth: 2,
        backgroundColor: gradient,
        fill: true,
        tension: 0.25,
        pointRadius: 1,
        pointHoverRadius: 5
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: "index",
        intersect: false
      },
      plugins: {
        legend: { display: false }
      },
      scales: {
        x: {
          grid: { color: "rgba(255, 255, 255, 0.05)" },
          ticks: { color: "#94a3b8", maxTicksLimit: 10 }
        },
        y: {
          grid: { color: "rgba(255, 255, 255, 0.05)" },
          ticks: { color: "#94a3b8" }
        }
      }
    }
  });
}

// Range Switcher
function setupRangeButtons() {
  const rangeBtns = document.querySelectorAll(".btn-range");
  rangeBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      rangeBtns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      currentRange = btn.dataset.range;
      loadHistory(currentRange);
    });
  });
}

// Load Parquet History Data
async function loadHistory(range) {
  try {
    const res = await fetch(`/api/devices/${currentHandle}/history?range=${range}`);
    const data = await res.json();

    valKwh.textContent = data.total_kwh.toFixed(2);
    valCost.textContent = data.total_cost_eur.toFixed(2);

    const labels = [];
    const values = [];

    data.points.forEach(pt => {
      const d = new Date(pt.ts);
      let label = "";
      if (range === "live" || range === "24h") {
        label = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      } else if (range === "7d" || range === "30d") {
        label = `${d.getDate()}.${d.getMonth() + 1}. ${d.getHours()}:00`;
      } else {
        label = d.toLocaleDateString();
      }
      labels.push(label);
      values.push(pt.watts);
    });

    chartInstance.data.labels = labels;
    chartInstance.data.datasets[0].data = values;
    chartInstance.update();
  } catch (err) {
    console.error("Error loading history:", err);
  }
}

// Pricing
async function loadPricing() {
  try {
    const res = await fetch("/api/pricing");
    const data = await res.json();
    document.getElementById("inputPrice").value = data.today_price.toFixed(4);
  } catch (err) {
    console.error("Error loading pricing:", err);
  }
}

document.getElementById("btnSavePrice").addEventListener("click", async () => {
  const price = parseFloat(document.getElementById("inputPrice").value);
  if (!price || price <= 0) return alert("Ungültiger Preis!");
  try {
    const res = await fetch("/api/pricing", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ price_eur_kwh: price })
    });
    if (res.ok) alert("✅ Strompreis gespeichert!");
    loadHistory(currentRange);
  } catch (err) {
    alert("Fehler: " + err.message);
  }
});

// Auto-On Failsafe Config
document.getElementById("btnSaveAutoOn").addEventListener("click", async () => {
  const sec = parseInt(document.getElementById("inputAutoOn").value) || 5;
  try {
    const res = await fetch(`/api/devices/${currentHandle}/config/auto-on`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delay_seconds: sec })
    });
    if (res.ok) alert(`✅ Hardware Auto-On Timer (${sec}s) im Shelly gespeichert!`);
  } catch (err) {
    alert("Fehler: " + err.message);
  }
});
