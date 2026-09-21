import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, date, timedelta
from typing import Dict, List, Any, Optional

from config import (
    DEVICES_FILE, POLL_INTERVAL_SEC, BUFFER_FLUSH_INTERVAL_SEC,
    SERVER_PLUG_HANDLE, DEFAULT_POWERCYCLE_DELAY_SEC, AUTO_ON_FALLBACK_SEC
)
from shelly_client import shelly_client
from storage import storage

logger = logging.getLogger("poller")

class DevicePoller:
    def __init__(self):
        self.devices: Dict[str, Dict[str, Any]] = {}
        # In-Memory Ring-Puffer für Live-Charts (letzte 3.600 Sekunden = 1 Stunde)
        self.live_buffers: Dict[str, deque] = {}
        # Sammelpuffer für den periodischen Parquet-Flush
        self.flush_buffers: Dict[str, List[Dict[str, Any]]] = {}
        self.latest_readings: Dict[str, Dict[str, Any]] = {}
        self._running = False
        self._last_flush = time.time()
        self._last_daily_rollup = date.today() - timedelta(days=1)
        self.load_devices()

    def load_devices(self):
        if DEVICES_FILE.exists():
            try:
                with open(DEVICES_FILE, "r") as f:
                    dev_list = json.load(f)
                    for d in dev_list:
                        handle = d["handle"]
                        self.devices[handle] = d
                        if handle not in self.live_buffers:
                            self.live_buffers[handle] = deque(maxlen=3600)
                        if handle not in self.flush_buffers:
                            self.flush_buffers[handle] = []
            except Exception as e:
                logger.error(f"Failed to load {DEVICES_FILE}: {e}")
        else:
            # Standardmäßiges Server-Plug Device anlegen
            default_dev = {
                "handle": SERVER_PLUG_HANDLE,
                "name": "QJM Server Steckdose",
                "ip": "10.20.0.100",
                "is_server_plug": True,
                "model": "Shelly Plus Plug S",
                "enabled": True
            }
            self.devices[SERVER_PLUG_HANDLE] = default_dev
            self.live_buffers[SERVER_PLUG_HANDLE] = deque(maxlen=3600)
            self.flush_buffers[SERVER_PLUG_HANDLE] = []
            self.save_devices()

    def save_devices(self):
        try:
            with open(DEVICES_FILE, "w") as f:
                json.dump(list(self.devices.values()), f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save {DEVICES_FILE}: {e}")

    def get_devices(self) -> List[Dict[str, Any]]:
        result = []
        for handle, d in self.devices.items():
            latest = self.latest_readings.get(handle, {})
            result.append({
                **d,
                "latest": latest,
                "online": latest.get("online", False)
            })
        return result

    def get_latest(self, handle: str) -> Optional[Dict[str, Any]]:
        return self.latest_readings.get(handle)

    def get_live_buffer(self, handle: str) -> List[Dict[str, Any]]:
        if handle in self.live_buffers:
            return list(self.live_buffers[handle])
        return []

    def flush_all(self):
        """Schreibt alle im RAM gepufferten Daten nach Parquet."""
        for handle, batch in self.flush_buffers.items():
            if batch:
                storage.write_raw_batch(handle, batch)
                batch.clear()
        self._last_flush = time.time()

    async def run_loop(self):
        self._running = True
        logger.info(f"Starting poller daemon (poll_interval={POLL_INTERVAL_SEC}s, flush_interval={BUFFER_FLUSH_INTERVAL_SEC}s)")

        while self._running:
            start_ts = time.time()
            now_dt = datetime.now()

            # 1. Alle aktivierten Geräte abfragen
            for handle, dev in list(self.devices.items()):
                if not dev.get("enabled", True):
                    continue

                ip = dev.get("ip")
                if not ip:
                    continue

                # Status abrufen
                status = await shelly_client.get_switch_status(ip)
                if status:
                    ae_dict = status.get("aenergy", {})
                    temp_dict = status.get("temperature", {})

                    reading = {
                        "timestamp": now_dt,
                        "apower": float(status.get("apower", 0.0)),
                        "voltage": float(status.get("voltage", 0.0)),
                        "current": float(status.get("current", 0.0)),
                        "aenergy_total": float(ae_dict.get("total", 0.0)),
                        "temp_c": float(temp_dict.get("tC", 0.0)),
                        "output": bool(status.get("output", True)),
                        "online": True
                    }
                else:
                    reading = {
                        "timestamp": now_dt,
                        "apower": 0.0,
                        "voltage": 0.0,
                        "current": 0.0,
                        "aenergy_total": 0.0,
                        "temp_c": 0.0,
                        "output": False,
                        "online": False
                    }

                self.latest_readings[handle] = reading
                self.live_buffers[handle].append(reading)
                # Für Parquet nur loggen, wenn online oder explizit bekannt
                if reading["online"]:
                    self.flush_buffers[handle].append(reading)

            # 2. Prüfen, ob Puffer nach Parquet geflusht werden soll
            if time.time() - self._last_flush >= BUFFER_FLUSH_INTERVAL_SEC:
                self.flush_all()

            # 3. Nächtlicher Rollup & Cleanup (einmal täglich kurz nach Mitternacht)
            current_day = now_dt.date()
            if current_day > self._last_daily_rollup and now_dt.hour >= 0 and now_dt.minute >= 5:
                yesterday_str = (current_day - timedelta(days=1)).isoformat()
                logger.info(f"Triggering daily rollups for {yesterday_str}")
                for handle in self.devices.keys():
                    storage.run_1m_rollup(handle, yesterday_str)
                    storage.cleanup_old_data(handle)
                self._last_daily_rollup = current_day

            # Schlafzeit berechnen für exakten 1s-Takt
            elapsed = time.time() - start_ts
            sleep_time = max(0.05, POLL_INTERVAL_SEC - elapsed)
            await asyncio.sleep(sleep_time)

    def stop(self):
        self._running = False
        self.flush_all()

poller = DevicePoller()
