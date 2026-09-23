import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import (
    POWERMETER_PORT, POWERMETER_HOST, SERVER_PLUG_HANDLE,
    DEFAULT_POWERCYCLE_DELAY_SEC, AUTO_ON_FALLBACK_SEC, DAILY_SYNC_BACKFILL_DAYS
)
from poller import poller
from shelly_client import shelly_client
from storage import storage
from pricing import pricing_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("powermeter_main")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Pydantic Request Models
# ---------------------------------------------------------------------------
class DeviceCreateOrUpdate(BaseModel):
    handle: str = Field(..., description="Eindeutiges Handle, z. B. 'server-plug'")
    name: str = Field(..., description="Anzeigename")
    ip: str = Field(..., description="IP-Adresse des Shelly im lokalen Netz")
    is_server_plug: bool = Field(False, description="Markiert den Server-Plug für den QJM MCP-Server")
    enabled: bool = Field(True, description="Aktiviert/Deaktiviert das Polling")

class SwitchRequest(BaseModel):
    on: bool

class PowerCycleRequest(BaseModel):
    restart_delay_seconds: int = Field(DEFAULT_POWERCYCLE_DELAY_SEC, ge=1, le=86400, description="Wartezeit bis zum automatischen Wiedereinschalten in Sekunden (z. B. 5 für schnellen Reboot oder 28800 für 8h)")

class AutoOnConfigRequest(BaseModel):
    delay_seconds: int = Field(AUTO_ON_FALLBACK_SEC, ge=1, le=3600, description="Hardware-Notfall-Wiedereinschaltverzögerung auf dem Shelly")

class LedConfigRequest(BaseModel):
    mode: str = Field("power", description="'power' (Verbrauchsfarbe), 'switch' (Relaisstatus) oder 'off'")
    brightness: int = Field(100, ge=0, le=100)

class PriceUpdateRequest(BaseModel):
    price_eur_kwh: float = Field(..., gt=0.0)
    date: Optional[str] = Field(None, description="YYYY-MM-DD; falls leer, wird das heutige Datum verwendet")

class SystemActionRequest(BaseModel):
    action: str = Field("reboot", pattern="^(reboot|poweroff)$", description="'reboot' (OS-Neustart) oder 'poweroff' (OS herunterfahren)")
    delay_seconds: int = Field(3, ge=0, le=300, description="Wartezeit vor der OS-Aktion (Antwort geht vorher raus)")
    power_on_after_seconds: int = Field(0, ge=0, le=604800, description="Nur bei poweroff: 0 = aus bleiben, >0 = nach so vielen Sekunden autonom per Shelly wieder einschalten")

# ---------------------------------------------------------------------------
# FastAPI Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starte Shelly PowerMeter Service & Background Poller...")
    task = asyncio.create_task(poller.run_loop())

    async def _cleanup_schedules():
        for dev in poller.devices.values():
            ip = dev.get("ip")
            if ip:
                await shelly_client.cleanup_scheduled_power_cycles(ip)

    # Übrig gebliebene Auto-Power-On-Jobs vom Shelly entfernen (nach dem Boot).
    asyncio.create_task(_cleanup_schedules())

    async def _startup_daily_sync():
        await asyncio.sleep(10)  # Poller/Netz kurz Zeit geben
        await poller.backfill_daily_stats(DAILY_SYNC_BACKFILL_DAYS)

    asyncio.create_task(_startup_daily_sync())
    yield
    logger.info("Beende Shelly PowerMeter Service...")
    poller.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await shelly_client.aclose()

app = FastAPI(
    title="Shelly PowerMeter & Control Hub",
    description="Echtzeit-Verbrauchsüberwachung, Parquet Time-Series Logging & autarke Server-Power-Cycle-Steuerung",
    version="2.0.0",
    lifespan=lifespan
)

# ---------------------------------------------------------------------------
# Live-Projektion (REST & SSE teilen sich dieselbe Berechnung)
# ---------------------------------------------------------------------------
def _live_payload(handle: str) -> Optional[Dict[str, Any]]:
    """Aktueller Messwert inkl. Verbrauch/Kosten des RAM-Fensters (letzte ~1h)."""
    reading = poller.get_latest(handle)
    if not reading:
        return None
    totals = storage.summarize_live_buffer(poller.get_live_buffer(handle))
    ts = reading.get("timestamp")
    # Naive lokale Zeit mit UTC-Offset versehen, damit auch Remote-Browser korrekt parsen.
    ts_iso = ts.astimezone().isoformat() if hasattr(ts, "astimezone") else str(ts)
    apower = float(reading.get("apower", 0.0) or 0.0)
    return {
        "ts": ts_iso,
        # Aliase für bestehende Consumer (MCP-Tool, ältere Clients)
        "timestamp": ts_iso,
        "watts": apower,
        "apower": apower,
        "voltage": float(reading.get("voltage", 0.0) or 0.0),
        "current": float(reading.get("current", 0.0) or 0.0),
        "aenergy_total": float(reading.get("aenergy_total", 0.0) or 0.0),
        "kwh": totals["total_kwh"],
        "cost_eur": totals["total_cost_eur"],
        "price_eur_kwh": pricing_manager.get_price(),
        "temp_c": float(reading.get("temp_c", 0.0) or 0.0),
        "output": bool(reading.get("output", False)),
        "online": bool(reading.get("online", False)),
    }

# ---------------------------------------------------------------------------
# API Routes: Devices & Control
# ---------------------------------------------------------------------------
@app.get("/api/devices")
async def list_devices():
    return poller.get_devices()

@app.post("/api/devices")
async def save_device(dev: DeviceCreateOrUpdate):
    existing = poller.devices.get(dev.handle, {})
    poller.devices[dev.handle] = {
        "handle": dev.handle,
        "name": dev.name,
        "ip": dev.ip,
        "is_server_plug": dev.is_server_plug,
        "model": "Shelly Plus Plug S",
        "enabled": dev.enabled,
        "auto_on_delay": int(existing.get("auto_on_delay", AUTO_ON_FALLBACK_SEC)),
    }
    if dev.handle not in poller.live_buffers:
        from collections import deque
        poller.live_buffers[dev.handle] = deque(maxlen=3600)
    if dev.handle not in poller.flush_buffers:
        poller.flush_buffers[dev.handle] = []
    poller.save_devices()
    return {"status": "saved", "handle": dev.handle}

@app.delete("/api/devices/{handle}")
async def delete_device(handle: str):
    if handle in poller.devices:
        del poller.devices[handle]
        poller.save_devices()
        return {"status": "deleted", "handle": handle}
    raise HTTPException(status_code=404, detail="Device not found")

@app.get("/api/devices/{handle}/live")
async def get_device_live(handle: str):
    payload = _live_payload(handle)
    if not payload:
        raise HTTPException(status_code=404, detail=f"No data for device '{handle}'")
    return {
        "handle": handle,
        "device": poller.devices.get(handle, {}),
        "metrics": payload
    }

@app.get("/api/devices/{handle}/stream")
async def stream_device(handle: str):
    """
    Server-Sent-Events-Stream (1 Hz). Bewusst als SSE umgesetzt, damit der
    Live-Stream auch ohne die optionale websockets/wsproto-Abhängigkeit von
    uvicorn funktioniert.
    """
    if handle not in poller.devices:
        raise HTTPException(status_code=404, detail="Device not found")

    async def event_generator():
        last_ts: Optional[str] = None
        while True:
            payload = _live_payload(handle)
            if payload and payload["ts"] != last_ts:
                last_ts = payload["ts"]
                yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )

@app.post("/api/devices/{handle}/switch")
async def set_switch(handle: str, req: SwitchRequest):
    dev = poller.devices.get(handle)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    delay = int(dev.get("auto_on_delay", AUTO_ON_FALLBACK_SEC))
    success = await shelly_client.set_switch_with_failsafe(dev["ip"], req.on, auto_on_delay=delay)
    if not success:
        raise HTTPException(status_code=502, detail=f"Failed to communicate with Shelly at {dev['ip']}")
    return {"status": "success", "handle": handle, "output": req.on, "auto_on_delay": delay}

@app.post("/api/devices/{handle}/power-cycle")
async def power_cycle(handle: str, req: PowerCycleRequest):
    """
    Führt einen autonomen Power-Cycle durch.
    Schaltet den Shelly ab und lässt ihn nach restart_delay_seconds über den
    hardwareseitigen ESP32-Timer selbstständig wieder einschalten.
    """
    dev = poller.devices.get(handle)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    
    # Sicherstellen, dass der Puffer vor dem Abschalten gesichert wird
    poller.flush_all()

    success = await shelly_client.power_cycle(dev["ip"], req.restart_delay_seconds)
    if not success:
        raise HTTPException(status_code=502, detail=f"Failed to trigger power cycle on Shelly at {dev['ip']}")
    
    return {
        "status": "triggered",
        "handle": handle,
        "restart_delay_seconds": req.restart_delay_seconds,
        "message": f"Steckdose abgeschaltet. Wiedereinschalten erfolgt autonom in {req.restart_delay_seconds} Sekunden auf dem Shelly!"
    }

@app.post("/api/devices/{handle}/config/auto-on")
async def configure_auto_on(handle: str, req: AutoOnConfigRequest):
    dev = poller.devices.get(handle)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    success = await shelly_client.configure_auto_on(dev["ip"], req.delay_seconds, enabled=True)
    if not success:
        raise HTTPException(status_code=502, detail=f"Failed to configure Auto-On on Shelly at {dev['ip']}")
    dev["auto_on_delay"] = req.delay_seconds
    poller.save_devices()
    return {"status": "configured", "handle": handle, "auto_on_delay_seconds": req.delay_seconds}

@app.post("/api/devices/{handle}/config/led")
async def configure_led(handle: str, req: LedConfigRequest):
    dev = poller.devices.get(handle)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    success = await shelly_client.set_led_mode(dev["ip"], req.mode, req.brightness)
    if not success:
        raise HTTPException(status_code=502, detail=f"Failed to configure LED on Shelly at {dev['ip']}")
    return {"status": "configured", "handle": handle, "mode": req.mode}

# ---------------------------------------------------------------------------
# API Routes: Geordnetes Herunterfahren / Neustart des Servers
# ---------------------------------------------------------------------------
async def _sudo_available(action: str) -> bool:
    """Prüft, ob die OS-Aktion ohne Passwort (sudoers) ausgeführt werden darf."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/sudo", "-n", "-l", "/usr/bin/systemctl", action,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        return (await proc.wait()) == 0
    except Exception:
        return False

async def _run_system_action(action: str, delay_seconds: int):
    """Führt nach Ablauf der Verzögerung die OS-Aktion aus (Antwort ist bereits raus)."""
    await asyncio.sleep(delay_seconds)
    try:
        poller.flush_all()
    except Exception as e:
        logger.error(f"Parquet-Flush vor '{action}' fehlgeschlagen: {e}")
    cmd = ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", action]
    logger.warning(f"Führe geordnete Systemaktion aus: {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(*cmd)
        rc = await proc.wait()
        if rc != 0:
            logger.error(f"Systemaktion '{action}' fehlgeschlagen (exit {rc})")
    except Exception as e:
        logger.error(f"Systemaktion '{action}' konnte nicht ausgeführt werden: {e}")

@app.post("/api/devices/{handle}/system")
async def system_action(handle: str, req: SystemActionRequest):
    """
    Fährt das Betriebssystem geordnet herunter oder startet es neu.
    Bei action='poweroff' kann mit power_on_after_seconds ein autonomer Power-On
    direkt im Shelly geplant werden (der Dienst ist danach ja offline).
    Benötigt eine sudoers-Regel für /usr/bin/systemctl reboot|poweroff.
    """
    dev = poller.devices.get(handle)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")

    # Vorab prüfen, damit der Button nicht still ins Leere läuft.
    if not await _sudo_available(req.action):
        raise HTTPException(
            status_code=500,
            detail=("sudoers-Regel fehlt. Bitte einmalig anlegen: "
                    "'daniel ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff' "
                    "in /etc/sudoers.d/powermeter (chmod 0440)."),
        )

    power_on_at: Optional[str] = None
    if req.action == "poweroff" and req.power_on_after_seconds > 0:
        when = datetime.now() + timedelta(seconds=req.power_on_after_seconds)
        job_id = await shelly_client.schedule_power_cycle(dev["ip"], when)
        if job_id is None:
            raise HTTPException(status_code=502, detail="Konnte den Power-On im Shelly nicht planen")
        power_on_at = when.isoformat()

    asyncio.create_task(_run_system_action(req.action, req.delay_seconds))
    message = f"OS-Aktion '{req.action}' wird in {req.delay_seconds}s ausgeführt."
    if power_on_at:
        message += f" Wiedereinschalten geplant für {power_on_at}."
    return {
        "status": "accepted",
        "handle": handle,
        "action": req.action,
        "delay_seconds": req.delay_seconds,
        "power_on_at": power_on_at,
        "message": message,
    }

# ---------------------------------------------------------------------------
# API Routes: History & Analytics (Parquet)
# ---------------------------------------------------------------------------
@app.get("/api/devices/{handle}/history")
async def get_history(handle: str, range: str = "24h"):
    """
    range: 'live' | '24h' | '7d' | '30d' | '1y' | 'all'
    """
    live_buf = poller.get_live_buffer(handle)
    return storage.query_history(handle=handle, range_type=range, live_buffer=live_buf)

# ---------------------------------------------------------------------------
# API Routes: Tägliche Supabase-Aggregation
# ---------------------------------------------------------------------------
class DailySyncRequest(BaseModel):
    date: Optional[str] = Field(None, description="YYYY-MM-DD; Standard: gestern")
    days: Optional[int] = Field(None, ge=1, le=60, description="Optional: N Tage rückwirkend synchronisieren")

@app.post("/api/sync/daily")
async def sync_daily(req: Optional[DailySyncRequest] = None):
    """Aggregiert Tageswerte (min/avg/max, Energie, Preis) und schreibt sie nach Supabase."""
    if req and req.days:
        await poller.backfill_daily_stats(req.days)
        return {"status": "ok", "mode": "backfill", "days": req.days}
    day = req.date if req and req.date else (datetime.now().date() - timedelta(days=1)).isoformat()
    results = {}
    for handle, dev in poller.devices.items():
        if dev.get("enabled", True):
            results[handle] = await poller.sync_daily_stats(handle, day)
    return {"status": "ok", "day": day, "results": results}

# ---------------------------------------------------------------------------
# API Routes: Electricity Pricing
# ---------------------------------------------------------------------------
@app.get("/api/pricing")
async def get_pricing():
    return {
        "prices": pricing_manager.list_prices(),
        "today_price": pricing_manager.get_price()
    }

@app.post("/api/pricing")
async def set_pricing(req: PriceUpdateRequest):
    updated_price = pricing_manager.set_price(req.price_eur_kwh, req.date)
    return {"status": "updated", "date": req.date or "today", "price_eur_kwh": updated_price}

# ---------------------------------------------------------------------------
# Static Web Dashboard Mount
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
async def serve_dashboard():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "PowerMeter API online. Web Dashboard in /static/index.html wird initialisiert."}

if __name__ == "__main__":
    import uvicorn
    # Ohne dieses Timeout blockieren offene SSE-Streams den Shutdown, bis systemd
    # den Prozess nach TimeoutStopSec (Standard 90s) hart killt.
    uvicorn.run(
        "main:app",
        host=POWERMETER_HOST,
        port=POWERMETER_PORT,
        reload=False,
        timeout_graceful_shutdown=5,
    )
