import os
import shutil
import tempfile
from datetime import datetime, date, timedelta
from pathlib import Path

# Setup temporary test directories
test_dir = Path(tempfile.mkdtemp(prefix="powermeter_test_"))
os.environ["POLL_INTERVAL_SEC"] = "1"
os.environ["DEFAULT_POWERCYCLE_DELAY_SEC"] = "5"
os.environ["AUTO_ON_FALLBACK_SEC"] = "5"

from storage import ParquetStorage
from pricing import PricingManager

def run_tests():
    print("🧪 Starte Parquet- & Powermeter-Tests...")

    # 1. Test Pricing
    price_file = test_dir / "prices.parquet"
    pm = PricingManager(file_path=price_file)
    today_str = date.today().isoformat()
    pm.set_price(0.3850, today_str)
    retrieved = pm.get_price(today_str)
    assert abs(retrieved - 0.3850) < 1e-5, f"Expected 0.3850, got {retrieved}"
    print("  ✅ PricingManager: Setzen & Abrufen erfolgreich.")

    # 2. Test Parquet Storage & 1s Raw Writing
    storage = ParquetStorage()
    storage.raw_dir = test_dir / "raw"
    storage.rollup_1m_dir = test_dir / "rollup_1m"
    storage.rollup_1h_dir = test_dir / "rollup_1h"

    storage.raw_dir.mkdir(parents=True, exist_ok=True)
    storage.rollup_1m_dir.mkdir(parents=True, exist_ok=True)
    storage.rollup_1h_dir.mkdir(parents=True, exist_ok=True)

    test_handle = "test-server-plug"
    target_date = "2026-09-20"
    base_ts = datetime(2026, 9, 20, 10, 0, 0)

    # 120 Sekunden an synthetischen Messdaten generieren (2 volle Minuten)
    records = []
    wh_counter = 5000.0
    for s in range(120):
        current_ts = base_ts + timedelta(seconds=s)
        watts = 120.0 + (s % 10)  # 120W - 129W
        wh_counter += watts / 3600.0
        records.append({
            "timestamp": current_ts,
            "apower": watts,
            "voltage": 230.5,
            "current": watts / 230.5,
            "aenergy_total": wh_counter,
            "temp_c": 41.2,
            "output": True
        })

    storage.write_raw_batch(test_handle, records)
    raw_file = storage.raw_dir / target_date / f"{test_handle}.parquet"
    assert raw_file.exists(), f"Raw file {raw_file} not found!"
    print(f"  ✅ Parquet Raw Batch: 120 Sekunden erfolgreich nach {raw_file.name} geschrieben ({raw_file.stat().st_size} Bytes).")

    # 3. Test 1-Minute Rollup
    success_1m = storage.run_1m_rollup(test_handle, target_date)
    assert success_1m, "1m Rollup fehlgeschlagen!"
    rollup_1m_file = storage.rollup_1m_dir / "2026-09" / f"{test_handle}.parquet"
    assert rollup_1m_file.exists(), f"Rollup 1m Datei {rollup_1m_file} not found!"
    print(f"  ✅ Parquet 1m Rollup: Erfolgreich verdichtet ({rollup_1m_file.stat().st_size} Bytes).")

    # 4. Test 1-Hour Permanent Rollup
    success_1h = storage.run_1h_rollup(test_handle, "2026-09")
    assert success_1h, "1h Rollup fehlgeschlagen!"
    rollup_1h_file = storage.rollup_1h_dir / "2026" / f"{test_handle}.parquet"
    assert rollup_1h_file.exists(), f"Rollup 1h Datei {rollup_1h_file} not found!"
    print(f"  ✅ Parquet 1h Rollup: Erfolgreich für alle Ewigkeit archiviert ({rollup_1h_file.stat().st_size} Bytes).")

    # 5. Test History Query
    history = storage.query_history(test_handle, range_type="24h")
    assert history["handle"] == test_handle
    assert len(history["points"]) > 0
    print(f"  ✅ History Query (24h): {len(history['points'])} Datenpunkte, Peak: {history['peak_watts']}W, Total: {history['total_kwh']} kWh.")

    # Cleanup test dir
    shutil.rmtree(test_dir, ignore_errors=True)
    print("\n🎉 Alle Parquet & Storage Unit-Tests erfolgreich bestanden!")

if __name__ == "__main__":
    run_tests()
