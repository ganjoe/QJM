import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env if present
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
ROLLUP_1M_DIR = DATA_DIR / "rollup_1m"
ROLLUP_1H_DIR = DATA_DIR / "rollup_1h"

# Ensure storage directories exist
RAW_DIR.mkdir(parents=True, exist_ok=True)
ROLLUP_1M_DIR.mkdir(parents=True, exist_ok=True)
ROLLUP_1H_DIR.mkdir(parents=True, exist_ok=True)

# Device Configuration
SERVER_PLUG_HANDLE = os.getenv("SERVER_PLUG_HANDLE", "server-plug")
DEVICES_FILE = BASE_DIR / "devices.json"

# Timing & Retention Settings (Anti-Magic-Numbers: All configurable)
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "1"))
DEFAULT_POWERCYCLE_DELAY_SEC = int(os.getenv("DEFAULT_POWERCYCLE_DELAY_SEC", "5"))
AUTO_ON_FALLBACK_SEC = int(os.getenv("AUTO_ON_FALLBACK_SEC", "5"))
BUFFER_FLUSH_INTERVAL_SEC = int(os.getenv("BUFFER_FLUSH_INTERVAL_SEC", "300"))
RAW_RETENTION_DAYS = int(os.getenv("RAW_RETENTION_DAYS", "2"))
ROLLUP_1M_RETENTION_DAYS = int(os.getenv("ROLLUP_1M_RETENTION_DAYS", "30"))

# Pricing & Server Settings
DEFAULT_ELECTRICITY_PRICE = float(os.getenv("DEFAULT_ELECTRICITY_PRICE", "0.35"))
POWERMETER_PORT = int(os.getenv("POWERMETER_PORT", "8800"))
POWERMETER_HOST = os.getenv("POWERMETER_HOST", "0.0.0.0")

PRICES_FILE = DATA_DIR / "electricity_prices.parquet"
