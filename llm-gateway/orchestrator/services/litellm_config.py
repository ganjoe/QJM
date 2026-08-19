import os
import yaml
import logging

logger = logging.getLogger("orchestrator.litellm_config")

class LiteLLMConfigManager:
    def __init__(self, config_path: str = None):
        self.config_path = config_path or os.getenv("LITELLM_CONFIG_PATH", "/litellm-config/config.yaml")

    def read_config(self) -> dict:
        if not os.path.exists(self.config_path):
            logger.error(f"Config file not found at {self.config_path}")
            return {"model_list": []}
        try:
            with open(self.config_path, "r") as f:
                return yaml.safe_load(f) or {"model_list": []}
        except Exception as e:
            logger.error(f"Failed to read config {self.config_path}: {e}")
            return {"model_list": []}

    def update_model_target(self, tier_name: str, new_model_id: str, new_api_base: str = None) -> bool:
        config = self.read_config()
        updated = False
        for item in config.get("model_list", []):
            if item.get("model_name") == tier_name:
                params = item.get("litellm_params", {})
                params["model"] = new_model_id
                if new_api_base:
                    params["api_base"] = new_api_base
                item["litellm_params"] = params
                updated = True

        if updated:
            return self._write_config(config)
        return False

    def add_route(self, tier_name: str, target_model: str, api_base: str = None) -> bool:
        config = self.read_config()
        if "model_list" not in config:
            config["model_list"] = []
            
        # Check if exists, then update instead
        for item in config["model_list"]:
            if item.get("model_name") == tier_name:
                return self.update_model_target(tier_name, target_model, api_base)
                
        # Create new route
        params = {"model": target_model}
        if api_base:
            params["api_base"] = api_base
            
        config["model_list"].append({
            "model_name": tier_name,
            "litellm_params": params
        })
        
        return self._write_config(config)
        
    def delete_route(self, tier_name: str) -> bool:
        config = self.read_config()
        if "model_list" not in config:
            return False
            
        original_length = len(config["model_list"])
        config["model_list"] = [item for item in config["model_list"] if item.get("model_name") != tier_name]
        
        if len(config["model_list"]) < original_length:
            return self._write_config(config)
        return False
        
    def _write_config(self, config: dict) -> bool:
        try:
            with open(self.config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            logger.info(f"Updated config {self.config_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to write updated config to {self.config_path}: {e}")
            return False

litellm_config_manager = LiteLLMConfigManager()
