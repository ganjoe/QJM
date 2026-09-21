import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config import (
    POWERMETER_PORT, POWERMETER_HOST, SERVER_PLUG_HANDLE,
    DEFAULT_POWERCYCLE_DELAY_SEC, AUTO_ON_FALLBACK_SEC
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
    restart_delay_seconds: int = Field(DEFAULT_POWERCYCLE_DELAY_SEC, description="Wartezeit bis zum automatischen Wiedereinschalten in Sekunden (z. B. 5 für schnellen Reboot oder 28800 für 8h)")

class AutoOnConfigRequest(BaseModel):
    delay_seconds: int = Field(AUTO_ON_FALLBACK_SEC, description="Hardware-Notfall-Wiedereinschaltverzögerung auf dem Shelly")

class LedConfigRequest(BaseModel):
    mode: str = Field("power", description="'power' (Verbrauchsfarbe), 'switch' (Relaisstatus) oder 'off'")
    brightness: int = Field(100, ge=0, le=100)

class PriceUpdateRequest(BaseModel):
    price_eur_kwh: float = Field(..., gt=0.0)
    date: Optional[str] = Field(None, description="YYYY-MM-DD; falls leer, wird das heutige Datum verwendet")

# ---------------------------------------------------------------------------
# FastAPI Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starte Shelly PowerMeter Service & Background Poller...")
    task = asyncio.create_task(poller.run_loop())
    yield
    logger.info("Beende Shelly PowerMeter Service...")
    poller.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(
    title="Shelly PowerMeter & Control Hub",
    description="Echtzeit-Verbrauchsüberwachung, Parquet Time-Series Logging & autarke Server-Power-Cycle-Steuerung",
    version="2.0.0",
    lifespan=lifespan
)

# ---------------------------------------------------------------------------
# API Routes: Devices & Control
# ---------------------------------------------------------------------------
@app.get("/api/devices")
async def list_devices():
    return poller.get_devices()

@app.post("/api/devices")
async def save_device(dev: DeviceCreateOrUpdate):
    poller.devices[dev.handle] = {
        "handle": dev.handle,
        "name": dev.name,
        "ip": dev.ip,
        "is_server_plug": dev.is_server_plug,
        "model": "Shelly Plus Plug S",
        "enabled": dev.enabled
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
    reading = poller.get_latest(handle)
    if not reading:
        raise HTTPException(status_code=404, detail=f"No data for device '{handle}'")
    return {
        "handle": handle,
        "device": poller.devices.get(handle, {}),
        "metrics": reading
    }

@app.post("/api/devices/{handle}/switch")
async def set_switch(handle: str, req: SwitchRequest):
    dev = poller.devices.get(handle)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    success = await shelly_client.set_switch(dev["ip"], req.on)
    if not success:
        raise HTTPException(status_code=502, detail=f"Failed to communicate with Shelly at {dev['ip']}")
    return {"status": "success", "handle": handle, "output": req.on}

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
    success = await shelly_client.configure_auto_on(dev["ip"], req.delay_seconds)
    if not success:
        raise HTTPException(status_code=502, detail=f"Failed to configure Auto-On on Shelly at {dev['ip']}")
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
# WebSockets: Real-time Live Stream
# ---------------------------------------------------------------------------
@app.websocket("/ws/live/{handle}")
async def websocket_live(websocket: WebSocket, handle: str):
    await websocket.accept()
    try:
        while True:
            reading = poller.get_latest(handle)
            if reading:
                # Schnelle JSON-Projektion
                payload = {
                    "ts": reading["timestamp"].isoformat() if hasattr(reading["timestamp"], "isoformat") else str(reading["timestamp"]),
                    "watts": reading["apower"],
                    "voltage": reading["voltage"],
                    "current": reading["current"],
                    "aenergy_total": reading["aenergy_total"],
                    "temp_c": reading["temp_c"],
                    "output": reading["output"],
                    "online": reading["online"]
                }
                await websocket.send_json(payload)
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WebSocket closed: {e}")

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
    uvicorn.run("main:app", host=POWERMETER_HOST, port=POWERMETER_PORT, reload=False)
