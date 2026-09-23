import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
import httpx

logger = logging.getLogger("shelly_client")

class ShellyClient:
    def __init__(self, timeout: float = 3.0):
        self.timeout = timeout
        # Ein dauerhafter Client vermeidet pro Sekunde neue TCP-Verbindungen/Handshakes.
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self):
        """Schließt den zugrunde liegenden HTTP-Client (beim Shutdown)."""
        await self._client.aclose()

    async def _rpc(self, ip: str, method: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        url = f"http://{ip}/rpc/{method}"
        try:
            if params:
                # In Shelly RPC, GET with query parameters is the cleanest and fastest method
                query_params = {}
                for k, v in params.items():
                    if isinstance(v, bool):
                        query_params[k] = "true" if v else "false"
                    elif isinstance(v, (dict, list)):
                        import json
                        # Kompakt ohne Leerzeichen: httpx kodiert Leerzeichen als '+',
                        # woran der Query-Parser des Shelly scheitert (HTTP 500 / "id missing").
                        query_params[k] = json.dumps(v, separators=(",", ":"))
                    else:
                        query_params[k] = str(v)
                response = await self._client.get(url, params=query_params)
            else:
                response = await self._client.get(url)

            if response.status_code == 200:
                data = response.json()
                # Manche Firmware liefert RPC-Fehler als HTTP 200 mit {"code","message"}.
                if isinstance(data, dict) and "code" in data and "message" in data:
                    logger.warning(f"Shelly {ip} RPC error for {method}: {data}")
                    return None
                # If JSON-RPC envelope
                if "result" in data:
                    return data["result"]
                return data
            else:
                logger.warning(f"Shelly {ip} returned HTTP {response.status_code} for {method}: {response.text}")
                return None
        except httpx.ConnectError:
            logger.debug(f"Connection failed to Shelly at {ip} (offline or unreachable)")
            return None
        except httpx.TimeoutException:
            logger.debug(f"Timeout querying Shelly at {ip}")
            return None
        except Exception as e:
            logger.error(f"Error calling {method} on {ip}: {e}")
            return None

    async def get_device_info(self, ip: str) -> Optional[Dict[str, Any]]:
        return await self._rpc(ip, "Shelly.GetDeviceInfo")

    async def get_switch_status(self, ip: str, switch_id: int = 0) -> Optional[Dict[str, Any]]:
        return await self._rpc(ip, "Switch.GetStatus", {"id": switch_id})

    async def get_full_status(self, ip: str) -> Optional[Dict[str, Any]]:
        """Abfrage des Gesamtzustands inkl. WLAN-Signal (RSSI) und Switch-Daten."""
        return await self._rpc(ip, "Shelly.GetStatus")

    async def set_switch(self, ip: str, on: bool, switch_id: int = 0) -> bool:
        """Schaltet das Relais an oder aus."""
        res = await self._rpc(ip, "Switch.Set", {"id": switch_id, "on": on})
        return res is not None

    async def power_cycle(self, ip: str, restart_delay_seconds: int, switch_id: int = 0) -> bool:
        """
        Schaltet das Relais sofort AUS und nach restart_delay_seconds hardwaregestützt wieder AN.
        Der Timer läuft autonom auf dem ESP32-Chip des Shelly!

        Verifiziert auf Firmware 1.2.3 (Plug S Gen3): Ein parallel gesetztes
        auto_on (5 s) überstimmt toggle_after NICHT – das Relais bleibt für die
        volle Dauer aus und schaltet erst durch den Timer wieder ein. Ein
        vorübergehendes Deaktivieren des Failsafes ist daher nicht nötig.
        """
        logger.info(f"Triggering hardware power-cycle on {ip} (toggle_after={restart_delay_seconds}s)")
        res = await self._rpc(ip, "Switch.Set", {
            "id": switch_id,
            "on": False,
            "toggle_after": restart_delay_seconds
        })
        return res is not None

    async def configure_auto_on(self, ip: str, delay_seconds: int = 5, switch_id: int = 0, enabled: bool = True) -> bool:
        """
        Konfiguriert den Failsafe-Wiedereinschalt-Timer im Flash des Shelly.
        Mit enabled=False wird der Failsafe deaktiviert (für ein bewusstes Ausschalten).
        """
        logger.info(f"Configuring hardware Auto-On fallback on {ip} (enabled={enabled}, delay={delay_seconds}s)")
        config = {
            "id": switch_id,
            "config": {
                "auto_on": enabled,
                "auto_on_delay": delay_seconds
            }
        }
        res = await self._rpc(ip, "Switch.SetConfig", config)
        return res is not None

    async def set_switch_with_failsafe(self, ip: str, on: bool, auto_on_delay: int = 5, switch_id: int = 0) -> bool:
        """
        Bewusstes Schalten über Dashboard/API:
        - AUS: Failsafe zuerst deaktivieren, damit die Steckdose nicht nach
          auto_on_delay von selbst wieder anspringt.
        - AN: Relais einschalten und Failsafe wieder scharf stellen.
        Der autonome Power-Cycle (toggle_after) bleibt davon unberührt.
        """
        if on:
            ok = await self.set_switch(ip, True, switch_id)
            if ok:
                await self.configure_auto_on(ip, auto_on_delay, switch_id, enabled=True)
            return ok
        cfg_ok = await self.configure_auto_on(ip, auto_on_delay, switch_id, enabled=False)
        sw_ok = await self.set_switch(ip, False, switch_id)
        return cfg_ok and sw_ok

    async def _rpc_post(self, ip: str, method: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """POST mit JSON-Body. Wird u. a. für Schedule.Create benötigt (GET schlägt dort fehl)."""
        url = f"http://{ip}/rpc/{method}"
        try:
            response = await self._client.post(url, json=body)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict) and "code" in data and "message" in data:
                    logger.warning(f"Shelly {ip} RPC error for {method}: {data}")
                    return None
                if isinstance(data, dict) and "result" in data:
                    return data["result"]
                return data
            logger.warning(f"Shelly {ip} returned HTTP {response.status_code} for {method}: {response.text}")
            return None
        except Exception as e:
            logger.error(f"Error calling {method} on {ip}: {e}")
            return None

    async def list_schedules(self, ip: str) -> List[Dict[str, Any]]:
        data = await self._rpc(ip, "Schedule.List")
        if isinstance(data, dict):
            return data.get("jobs") or []
        return []

    async def delete_schedule(self, ip: str, job_id: int) -> bool:
        res = await self._rpc(ip, "Schedule.Delete", {"id": job_id})
        return res is not None

    async def schedule_power_cycle(self, ip: str, when: datetime, off_seconds: int = 15,
                                   switch_id: int = 0, tag: str = "powermeter-autoon") -> Optional[int]:
        """
        Plant im Shelly einen Einmal-Power-Cycle: zur lokalen Zeit 'when' wird die
        Steckdose aus- und nach off_seconds wieder eingeschaltet (BIOS bootet).
        Nutzt einen 6-Feld-Cron (Sekunde Minute Stunde Tag Monat Wochentag).
        """
        timespec = f"{when.second} {when.minute} {when.hour} {when.day} {when.month} *"
        body = {
            "enable": True,
            "timespec": timespec,
            "calls": [{
                "method": "Switch.Set",
                "params": {"id": switch_id, "on": False, "toggle_after": off_seconds, "tag": tag},
            }],
        }
        res = await self._rpc_post(ip, "Schedule.Create", body)
        if isinstance(res, dict) and "id" in res:
            logger.info(f"Scheduled power-on on {ip} at {when.isoformat()} (job {res['id']}, off {off_seconds}s)")
            return int(res["id"])
        return None

    async def cleanup_scheduled_power_cycles(self, ip: str, tag: str = "powermeter-autoon") -> int:
        """Löscht übrig gebliebene Auto-Power-On-Jobs (Aufruf beim Dienststart)."""
        removed = 0
        try:
            for job in await self.list_schedules(ip):
                calls = job.get("calls") or []
                if any(isinstance(c, dict) and (c.get("params") or {}).get("tag") == tag for c in calls):
                    if await self.delete_schedule(ip, int(job.get("id"))):
                        removed += 1
        except Exception as e:
            logger.debug(f"Schedule cleanup failed for {ip}: {e}")
        if removed:
            logger.info(f"Removed {removed} leftover power-on schedule(s) on {ip}")
        return removed

    async def set_led_mode(self, ip: str, mode: str = "power", brightness: int = 100) -> bool:
        """
        Konfiguriert den LED-Ring des Shelly Plus / Gen3 Plug S.
        mode: 'power' (Farbe nach Verbrauch), 'switch' (Relaisstatus), 'off' (deaktiviert)
        """
        ui_config = {
            "config": {
                "leds": {
                    "mode": mode,
                    "power": {"brightness": brightness},
                    "colors": {
                        "switch:0": {
                            "on": {"brightness": brightness},
                            "off": {"brightness": brightness}
                        }
                    }
                }
            }
        }
        # Versuche zuerst Gen3 PLUGS_UI, dann Gen2 PLUG_UI
        res = await self._rpc(ip, "PLUGS_UI.SetConfig", ui_config)
        if res is None:
            res = await self._rpc(ip, "PLUG_UI.SetConfig", ui_config)
        return res is not None

shelly_client = ShellyClient()
