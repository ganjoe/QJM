import logging
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional
import pyarrow as pa
import pyarrow.parquet as pq

from config import PRICES_FILE, DEFAULT_ELECTRICITY_PRICE

logger = logging.getLogger("pricing")

PRICE_SCHEMA = pa.schema([
    ("date", pa.string()),
    ("price_eur_kwh", pa.float64()),
    ("updated_at", pa.string())
])

class PricingManager:
    def __init__(self, file_path: Path = PRICES_FILE):
        self.file_path = file_path
        self._cache: Dict[str, float] = {}
        self._load()

    def _load(self):
        if not self.file_path.exists():
            return
        try:
            table = pq.read_table(self.file_path)
            dates = table["date"].to_pylist()
            prices = table["price_eur_kwh"].to_pylist()
            for d, p in zip(dates, prices):
                self._cache[d] = float(p)
        except Exception as e:
            logger.error(f"Failed to load electricity prices from {self.file_path}: {e}")

    def _save(self):
        try:
            dates = []
            prices = []
            updated = []
            now_iso = datetime.now().isoformat()
            for d, p in sorted(self._cache.items()):
                dates.append(d)
                prices.append(float(p))
                updated.append(now_iso)

            table = pa.Table.from_arrays([
                pa.array(dates, type=pa.string()),
                pa.array(prices, type=pa.float64()),
                pa.array(updated, type=pa.string())
            ], schema=PRICE_SCHEMA)

            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, self.file_path, compression="ZSTD")
        except Exception as e:
            logger.error(f"Failed to save electricity prices to {self.file_path}: {e}")

    def get_price(self, target_date: Optional[str] = None) -> float:
        """Gibt den Strompreis für ein bestimmtes Datum (YYYY-MM-DD) oder heute zurück."""
        if not target_date:
            target_date = date.today().isoformat()
        if target_date in self._cache:
            return self._cache[target_date]
        
        # Fallback auf neuesten hinterlegten Preis oder Default
        if self._cache:
            latest_date = max(self._cache.keys())
            return self._cache[latest_date]
        return DEFAULT_ELECTRICITY_PRICE

    def set_price(self, price_eur_kwh: float, target_date: Optional[str] = None) -> float:
        """Setzt den Strompreis für ein bestimmtes Datum."""
        if not target_date:
            target_date = date.today().isoformat()
        self._cache[target_date] = round(float(price_eur_kwh), 4)
        self._save()
        logger.info(f"Updated electricity price for {target_date}: {price_eur_kwh:.4f} €/kWh")
        return self._cache[target_date]

    def list_prices(self) -> List[Dict[str, float]]:
        today_iso = date.today().isoformat()
        # Ensure today is present in cache
        if today_iso not in self._cache:
            self._cache[today_iso] = self.get_price(today_iso)
            self._save()

        return [{"date": d, "price_eur_kwh": p} for d, p in sorted(self._cache.items(), reverse=True)]

pricing_manager = PricingManager()
