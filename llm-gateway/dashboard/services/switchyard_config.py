import os
import logging
from typing import Any, Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

try:
    import tomli_w
except ImportError:
    tomli_w = None

logger = logging.getLogger("orchestrator.switchyard_config")

DEFAULT_ROUTES_TOML = """schema_version = 1

[llm_clients.lmstudio]
format = "openai_chat"
base_url = "http://host.docker.internal:1234/v1"

[llm_clients.gemini]
format = "openai_chat"
base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
api_key_env = "GEMINI_API_KEY"

[llm_clients.ollama]
format = "openai_chat"
base_url = "http://host.docker.internal:11434/v1"

[targets.local_model]
id = "default"
llm_client = "lmstudio"

[targets.gemini_flash]
id = "gemini-2.5-flash"
llm_client = "gemini"

[targets.gemini_pro]
id = "gemini-2.5-pro"
llm_client = "gemini"

[targets.ollama_embed]
id = "nomic-embed-text"
llm_client = "ollama"

[routes.local]
id = "local"
type = "passthrough"
target = "local_model"

[routes.gemini_flash]
id = "gemini-2.5-flash"
type = "passthrough"
target = "gemini_flash"

[routes.gemini_pro]
id = "gemini-2.5-pro"
type = "passthrough"
target = "gemini_pro"

[routes.nomic_embed]
id = "nomic-embed-text"
type = "passthrough"
target = "ollama_embed"

[routes.embeddings_alias]
id = "embeddings"
type = "passthrough"
target = "ollama_embed"

[routes.fast]
id = "fast"
type = "passthrough"
target = "gemini_flash"

[routes.reasoning]
id = "reasoning"
type = "passthrough"
target = "gemini_pro"

[routes.auto]
id = "auto"
type = "llm_classifier"
mode = "capability"
classifier_target = "gemini_flash"
weak_target = "local_model"
strong_target = "gemini_pro"
base_threshold = 0.5
threshold_step = 0.1
"""


class SwitchyardConfigManager:
    """Manages the routes.toml configuration file for NVIDIA NeMo Switchyard."""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.getenv(
            "SWITCHYARD_CONFIG_PATH", "/switchyard-config/routes.toml"
        )

    def read_config(self) -> dict[str, Any]:
        """Reads and parses the routes.toml configuration file."""
        if not os.path.exists(self.config_path):
            logger.warning("Config file not found at %s. Creating default.", self.config_path)
            self._write_raw_toml(DEFAULT_ROUTES_TOML)

        try:
            with open(self.config_path, "rb") as f:
                return tomllib.load(f)
        except Exception as e:
            logger.error("Failed to parse config %s: %s", self.config_path, e)
            return {}

    def get_routes(self) -> list[dict[str, Any]]:
        """Returns a list of all configured routes."""
        config = self.read_config()
        routes_dict = config.get("routes", {})
        routes_list = []
        for key, details in routes_dict.items():
            routes_list.append({
                "key": key,
                "id": details.get("id", key),
                "type": details.get("type", "passthrough"),
                "target": details.get("target"),
                "classifier_target": details.get("classifier_target"),
                "weak_target": details.get("weak_target"),
                "strong_target": details.get("strong_target"),
            })
        return routes_list

    def get_targets(self) -> list[dict[str, Any]]:
        """Returns a list of all configured targets (models)."""
        config = self.read_config()
        targets_dict = config.get("targets", {})
        targets_list = []
        for key, details in targets_dict.items():
            targets_list.append({
                "key": key,
                "id": details.get("id", key),
                "llm_client": details.get("llm_client"),
            })
        return targets_list

    def get_clients(self) -> list[dict[str, Any]]:
        """Returns a list of all configured LLM provider clients."""
        config = self.read_config()
        clients_dict = config.get("llm_clients", {})
        clients_list = []
        for key, details in clients_dict.items():
            clients_list.append({
                "key": key,
                "format": details.get("format"),
                "base_url": details.get("base_url"),
                "api_key_env": details.get("api_key_env"),
            })
        return clients_list

    def update_route_target(self, route_id: str, new_target: str) -> bool:
        """Updates the target of an existing passthrough route."""
        config = self.read_config()
        routes = config.setdefault("routes", {})

        target_key = None
        for key, r in routes.items():
            if r.get("id") == route_id or key == route_id:
                target_key = key
                break

        if target_key:
            routes[target_key]["target"] = new_target
            return self._write_config(config)

        # If not found, create new passthrough route
        routes[route_id] = {
            "id": route_id,
            "type": "passthrough",
            "target": new_target,
        }
        return self._write_config(config)

    def delete_route(self, route_id: str) -> bool:
        """Deletes a route by id or key."""
        config = self.read_config()
        routes = config.get("routes", {})
        target_key = None
        for key, r in routes.items():
            if r.get("id") == route_id or key == route_id:
                target_key = key
                break

        if target_key and target_key in routes:
            del routes[target_key]
            return self._write_config(config)
        return False

    def _write_config(self, config: dict[str, Any]) -> bool:
        """Serializes and writes the config dict to routes.toml."""
        if tomli_w is not None:
            try:
                os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
                with open(self.config_path, "wb") as f:
                    tomli_w.dump(config, f)
                logger.info("Successfully wrote updated Switchyard config to %s", self.config_path)
                return True
            except Exception as e:
                logger.error("Failed to write toml config %s: %s", self.config_path, e)
                return False
        else:
            logger.error("tomli_w library is not installed, cannot dump TOML")
            return False

    def _write_raw_toml(self, content: str) -> bool:
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                f.write(content)
            return True
        except Exception as e:
            logger.error("Failed to write raw toml to %s: %s", self.config_path, e)
            return False


switchyard_config_manager = SwitchyardConfigManager()
