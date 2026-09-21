import logging
from typing import Optional, Dict, Any
import httpx

logger = logging.getLogger("shelly_client")

class ShellyClient:
    def __init__(self, timeout: float = 3.0):
        self.timeout = timeout

    async def _rpc(self, ip: str, method: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        url = f"http://{ip}/rpc/{method}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                if params:
                    # In Shelly RPC, GET with query parameters is the cleanest and fastest method
                    query_params = {}
                    for k, v in params.items():
                        if isinstance(v, bool):
                            query_params[k] = "true" if v else "false"
                        elif isinstance(v, (dict, list)):
                            import json
                            query_params[k] = json.dumps(v)
                        else:
                            query_params[k] = str(v)
                    response = await client.get(url, params=query_params)
                else:
                    response = await client.get(url)
                
                if response.status_code == 200:
                    data = response.json()
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
        """
        logger.info(f"Triggering hardware power-cycle on {ip} (toggle_after={restart_delay_seconds}s)")
        res = await self._rpc(ip, "Switch.Set", {
            "id": switch_id,
            "on": False,
            "toggle_after": restart_delay_seconds
        })
        return res is not None

    async def configure_auto_on(self, ip: str, delay_seconds: int = 5, switch_id: int = 0) -> bool:
        """
        Konfiguriert den Failsafe-Wiedereinschalt-Timer im Flash des Shelly.
        Wenn das Relais (aus beliebigem Grund) ausgeschaltet wird, schaltet es sich
        nach delay_seconds automatisch wieder ein.
        """
        logger.info(f"Configuring hardware Auto-On fallback on {ip} (delay={delay_seconds}s)")
        config = {
            "id": switch_id,
            "config": {
                "auto_on": True,
                "auto_on_delay": delay_seconds
            }
        }
        res = await self._rpc(ip, "Switch.SetConfig", config)
        return res is not None

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
