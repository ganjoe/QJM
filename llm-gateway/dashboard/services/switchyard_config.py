import os
import logging
from typing import Any, Optional
from services.docker_client import docker_manager

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

try:
    import tomli_w
except ImportError:
    tomli_w = None

logger = logging.getLogger("dashboard.switchyard_config")


class SwitchyardConfigManager:
    """Manages the routes.toml configuration file and control operations for NVIDIA NeMo Switchyard."""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.getenv(
            "SWITCHYARD_CONFIG_PATH", "/switchyard-config/routes.toml"
        )

    def read_config(self) -> dict[str, Any]:
        """Reads and parses the routes.toml configuration file."""
        if not os.path.exists(self.config_path):
            logger.warning("Config file not found at %s.", self.config_path)
            return {}

        try:
            with open(self.config_path, "rb") as f:
                return tomllib.load(f)
        except Exception as e:
            logger.error("Failed to parse config %s: %s", self.config_path, e)
            return {}

    def read_raw_toml(self) -> str:
        """Returns the raw TOML string."""
        if not os.path.exists(self.config_path):
            return ""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error("Failed to read raw toml %s: %s", self.config_path, e)
            return ""

    def validate_and_write_raw_toml(self, content: str) -> tuple[bool, str]:
        """Validates TOML syntax and writes raw file."""
        try:
            # Parse to validate syntax
            parsed = tomllib.loads(content)
            if not isinstance(parsed, dict):
                return False, "Invalid TOML structure: Root must be a table."
        except Exception as e:
            return False, f"TOML Syntax Error: {str(e)}"

        success = self._write_raw_toml(content)
        if success:
            return True, "Config successfully saved."
        return False, "Failed to write file to disk."

    # --- Client Management ---
    def get_clients(self) -> list[dict[str, Any]]:
        config = self.read_config()
        clients_dict = config.get("llm_clients", {})
        res = []
        for key, details in clients_dict.items():
            res.append({
                "key": key,
                "format": details.get("format", "openai_chat"),
                "base_url": details.get("base_url", ""),
                "api_key_env": details.get("api_key_env", ""),
            })
        return res

    def save_client(self, key: str, data: dict[str, Any]) -> bool:
        config = self.read_config()
        clients = config.setdefault("llm_clients", {})
        clients[key] = {
            "format": data.get("format", "openai_chat"),
            "base_url": data.get("base_url", ""),
        }
        if data.get("api_key_env"):
            clients[key]["api_key_env"] = data["api_key_env"]
        return self._write_config(config)

    def delete_client(self, key: str) -> bool:
        config = self.read_config()
        clients = config.get("llm_clients", {})
        if key in clients:
            del clients[key]
            return self._write_config(config)
        return False

    # --- Target Management ---
    def get_targets(self) -> list[dict[str, Any]]:
        config = self.read_config()
        targets_dict = config.get("targets", {})
        res = []
        for key, details in targets_dict.items():
            res.append({
                "key": key,
                "id": details.get("id", key),
                "llm_client": details.get("llm_client", ""),
            })
        return res

    def save_target(self, key: str, data: dict[str, Any]) -> bool:
        config = self.read_config()
        targets = config.setdefault("targets", {})
        targets[key] = {
            "id": data.get("id", key),
            "llm_client": data.get("llm_client", ""),
        }
        return self._write_config(config)

    def delete_target(self, key: str) -> bool:
        config = self.read_config()
        targets = config.get("targets", {})
        if key in targets:
            del targets[key]
            return self._write_config(config)
        return False

    # --- Route Management ---
    def get_routes(self) -> list[dict[str, Any]]:
        config = self.read_config()
        routes_dict = config.get("routes", {})
        res = []
        for key, details in routes_dict.items():
            r = {
                "key": key,
                "id": details.get("id", key),
                "type": details.get("type", "passthrough"),
            }
            if r["type"] == "passthrough":
                r["target"] = details.get("target", "")
            elif r["type"] == "llm_classifier":
                r["mode"] = details.get("mode", "capability")
                r["classifier_target"] = details.get("classifier_target", "")
                r["weak_target"] = details.get("weak_target", "")
                r["strong_target"] = details.get("strong_target", "")
                r["base_threshold"] = details.get("base_threshold", 0.5)
                r["threshold_step"] = details.get("threshold_step", 0.1)
            elif r["type"] == "fallback":
                r["targets"] = details.get("targets", [])
            elif r["type"] in ("weighted", "load_balance"):
                r["weights"] = details.get("weights", {})
            res.append(r)
        return res

    def save_route(self, key: str, data: dict[str, Any]) -> bool:
        config = self.read_config()
        routes = config.setdefault("routes", {})
        
        rtype = data.get("type", "passthrough")
        entry: dict[str, Any] = {
            "id": data.get("id", key),
            "type": rtype,
        }

        if rtype == "passthrough":
            entry["target"] = data.get("target", "")
        elif rtype == "llm_classifier":
            entry["mode"] = data.get("mode", "capability")
            entry["classifier_target"] = data.get("classifier_target", "")
            entry["weak_target"] = data.get("weak_target", "")
            entry["strong_target"] = data.get("strong_target", "")
            entry["base_threshold"] = float(data.get("base_threshold", 0.5))
            entry["threshold_step"] = float(data.get("threshold_step", 0.1))
        elif rtype == "fallback":
            targets = data.get("targets", [])
            if isinstance(targets, str):
                targets = [t.strip() for t in targets.split(",") if t.strip()]
            entry["targets"] = targets
        elif rtype in ("weighted", "load_balance"):
            entry["weights"] = data.get("weights", {})

        routes[key] = entry
        return self._write_config(config)

    def delete_route(self, key: str) -> bool:
        config = self.read_config()
        routes = config.get("routes", {})
        if key in routes:
            del routes[key]
            return self._write_config(config)
        # Search by id
        for k, v in list(routes.items()):
            if v.get("id") == key:
                del routes[k]
                return self._write_config(config)
        return False

    async def reload_switchyard(self) -> dict[str, Any]:
        """Restarts the switchyard container."""
        try:
            success, msg = await docker_manager.restart_container("llm-gw-switchyard")
            return {"success": success, "message": msg}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # --- Internal Persistence ---
    def _write_config(self, config: dict[str, Any]) -> bool:
        if tomli_w is not None:
            try:
                os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
                with open(self.config_path, "wb") as f:
                    tomli_w.dump(config, f)
                logger.info("Saved config to %s", self.config_path)
                return True
            except Exception as e:
                logger.error("Failed to write toml config: %s", e)
                return False
        return False

    def _write_raw_toml(self, content: str) -> bool:
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                f.write(content)
            return True
        except Exception as e:
            logger.error("Failed to write raw toml: %s", e)
            return False


switchyard_config_manager = SwitchyardConfigManager()
