import os
import shutil
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc

from config import (
    RAW_DIR, ROLLUP_1M_DIR, ROLLUP_1H_DIR,
    RAW_RETENTION_DAYS, ROLLUP_1M_RETENTION_DAYS
)
from pricing import pricing_manager

logger = logging.getLogger("storage")

# Schema für 1-Sekunden-Rohdaten
RAW_SCHEMA = pa.schema([
    ("timestamp", pa.timestamp("us")),
    ("apower", pa.float32()),
    ("voltage", pa.float32()),
    ("current", pa.float32()),
    ("aenergy_total", pa.float64()),
    ("temp_c", pa.float32()),
    ("output", pa.bool_())
])

# Schema für 1-Minuten-Rollup (30 Tage Haltedauer)
ROLLUP_1M_SCHEMA = pa.schema([
    ("timestamp", pa.timestamp("us")),
    ("avg_power", pa.float32()),
    ("min_power", pa.float32()),
    ("max_power", pa.float32()),
    ("avg_voltage", pa.float32()),
    ("avg_current", pa.float32()),
    ("delta_wh", pa.float32()),
    ("samples", pa.int32())
])

# Schema für 1-Stunden-Rollup (Für alle Ewigkeit)
ROLLUP_1H_SCHEMA = pa.schema([
    ("timestamp", pa.timestamp("us")),
    ("avg_power", pa.float32()),
    ("peak_power", pa.float32()),
    ("min_power", pa.float32()),
    ("delta_wh", pa.float32()),
    ("kwh_total", pa.float32()),
    ("electricity_price", pa.float32()),
    ("cost_eur", pa.float32()),
    ("samples", pa.int32())
])

class ParquetStorage:
    def __init__(self):
        self.raw_dir = RAW_DIR
        self.rollup_1m_dir = ROLLUP_1M_DIR
        self.rollup_1h_dir = ROLLUP_1H_DIR

    def write_raw_batch(self, handle: str, records: List[Dict[str, Any]]):
        """
        Schreibt einen Batch von 1s-Rohdaten in die tägliche Parquet-Datei:
        data/raw/{YYYY-MM-DD}/{handle}.parquet
        """
        if not records:
            return

        # Gruppiere nach Tag (falls über Mitternacht geflusht wird)
        records_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for r in records:
            dt = r["timestamp"]
            if isinstance(dt, (int, float)):
                dt = datetime.fromtimestamp(dt)
            d_str = dt.strftime("%Y-%m-%d")
            records_by_date.setdefault(d_str, []).append(r)

        for d_str, day_records in records_by_date.items():
            day_dir = self.raw_dir / d_str
            day_dir.mkdir(parents=True, exist_ok=True)
            file_path = day_dir / f"{handle}.parquet"

            timestamps = []
            apower = []
            voltage = []
            current = []
            aenergy = []
            temp = []
            output = []

            for r in day_records:
                ts = r["timestamp"]
                if isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts)
                timestamps.append(ts)
                apower.append(float(r.get("apower", 0.0)))
                voltage.append(float(r.get("voltage", 0.0)))
                current.append(float(r.get("current", 0.0)))
                aenergy.append(float(r.get("aenergy_total", 0.0)))
                temp.append(float(r.get("temp_c", 0.0)))
                output.append(bool(r.get("output", True)))

            new_table = pa.Table.from_arrays([
                pa.array(timestamps, type=pa.timestamp("us")),
                pa.array(apower, type=pa.float32()),
                pa.array(voltage, type=pa.float32()),
                pa.array(current, type=pa.float32()),
                pa.array(aenergy, type=pa.float64()),
                pa.array(temp, type=pa.float32()),
                pa.array(output, type=pa.bool_())
            ], schema=RAW_SCHEMA)

            if file_path.exists():
                try:
                    existing_table = pq.read_table(file_path)
                    combined_table = pa.concat_tables([existing_table, new_table])
                except Exception as e:
                    logger.warning(f"Error reading existing raw table {file_path}, replacing: {e}")
                    combined_table = new_table
            else:
                combined_table = new_table

            pq.write_table(combined_table, file_path, compression="ZSTD")
            logger.debug(f"Flushed {len(day_records)} raw records to {file_path}")

    def run_1m_rollup(self, handle: str, target_date_str: str) -> bool:
        """
        Aggregiert die 1s-Rohdaten eines Tages zu 1-Minuten-Intervallen (1.440 Zeilen)
        und speichert sie in data/rollup_1m/{YYYY-MM}/{handle}.parquet.
        """
        raw_file = self.raw_dir / target_date_str / f"{handle}.parquet"
        if not raw_file.exists():
            return False

        try:
            table = pq.read_table(raw_file)
            if table.num_rows == 0:
                return False

            # Konvertiere in Python-Strukturen für schnelle Aggregation
            ts_list = table["timestamp"].to_pylist()
            p_list = table["apower"].to_pylist()
            v_list = table["voltage"].to_pylist()
            c_list = table["current"].to_pylist()
            ae_list = table["aenergy_total"].to_pylist()

            # Bucketing nach Minute
            minutes: Dict[datetime, Dict[str, Any]] = {}
            for ts, p, v, c, ae in zip(ts_list, p_list, v_list, c_list, ae_list):
                minute_bucket = ts.replace(second=0, microsecond=0)
                if minute_bucket not in minutes:
                    minutes[minute_bucket] = {
                        "p": [], "v": [], "c": [], "ae": []
                    }
                minutes[minute_bucket]["p"].append(p)
                minutes[minute_bucket]["v"].append(v)
                minutes[minute_bucket]["c"].append(c)
                minutes[minute_bucket]["ae"].append(ae)

            out_ts, out_avg_p, out_min_p, out_max_p = [], [], [], []
            out_avg_v, out_avg_c, out_delta_wh, out_samples = [], [], [], []

            for m_ts in sorted(minutes.keys()):
                data = minutes[m_ts]
                plist = data["p"]
                aelist = data["ae"]
                cnt = len(plist)

                avg_p = sum(plist) / cnt if cnt else 0.0
                min_p = min(plist) if cnt else 0.0
                max_p = max(plist) if cnt else 0.0
                avg_v = sum(data["v"]) / cnt if cnt else 0.0
                avg_c = sum(data["c"]) / cnt if cnt else 0.0

                # Delta Wh: Wenn Hardware-Zähler vorhanden, Differenz max - min; sonst Durchschnittswatt * Zeit
                delta_wh = 0.0
                if len(aelist) > 1 and max(aelist) >= min(aelist):
                    delta_wh = max(aelist) - min(aelist)
                if delta_wh <= 0.0:
                    delta_wh = avg_p * (cnt / 3600.0)

                out_ts.append(m_ts)
                out_avg_p.append(avg_p)
                out_min_p.append(min_p)
                out_max_p.append(max_p)
                out_avg_v.append(avg_v)
                out_avg_c.append(avg_c)
                out_delta_wh.append(delta_wh)
                out_samples.append(cnt)

            rollup_table = pa.Table.from_arrays([
                pa.array(out_ts, type=pa.timestamp("us")),
                pa.array(out_avg_p, type=pa.float32()),
                pa.array(out_min_p, type=pa.float32()),
                pa.array(out_max_p, type=pa.float32()),
                pa.array(out_avg_v, type=pa.float32()),
                pa.array(out_avg_c, type=pa.float32()),
                pa.array(out_delta_wh, type=pa.float32()),
                pa.array(out_samples, type=pa.int32())
            ], schema=ROLLUP_1M_SCHEMA)

            # Monatsordner
            month_str = target_date_str[:7]
            month_dir = self.rollup_1m_dir / month_str
            month_dir.mkdir(parents=True, exist_ok=True)
            rollup_file = month_dir / f"{handle}.parquet"

            if rollup_file.exists():
                try:
                    existing = pq.read_table(rollup_file)
                    rollup_table = pa.concat_tables([existing, rollup_table])
                except Exception as e:
                    logger.warning(f"Error appending to 1m rollup {rollup_file}: {e}")

            pq.write_table(rollup_table, rollup_file, compression="ZSTD")
            logger.info(f"Generated 1m rollup for {handle} on {target_date_str} ({len(out_ts)} minutes)")
            return True
        except Exception as e:
            logger.error(f"Failed to create 1m rollup for {handle} on {target_date_str}: {e}")
            return False

    def run_1h_rollup(self, handle: str, month_str: str) -> bool:
        """
        Aggregiert die 1m-Rollups eines Monats zu 1-Stunden-Intervallen (24 Zeilen/Tag)
        für alle Ewigkeit mit dem hinterlegten Tagesstrompreis.
        Ablage: data/rollup_1h/{year}/{handle}.parquet
        """
        m1_file = self.rollup_1m_dir / month_str / f"{handle}.parquet"
        if not m1_file.exists():
            return False

        try:
            table = pq.read_table(m1_file)
            if table.num_rows == 0:
                return False

            ts_list = table["timestamp"].to_pylist()
            avg_p_list = table["avg_power"].to_pylist()
            max_p_list = table["max_power"].to_pylist()
            min_p_list = table["min_power"].to_pylist()
            delta_wh_list = table["delta_wh"].to_pylist()
            samples_list = table["samples"].to_pylist()

            # Bucketing nach Stunde
            hours: Dict[datetime, Dict[str, Any]] = {}
            for ts, ap, mx, mn, dwh, smp in zip(ts_list, avg_p_list, max_p_list, min_p_list, delta_wh_list, samples_list):
                h_ts = ts.replace(minute=0, second=0, microsecond=0)
                if h_ts not in hours:
                    hours[h_ts] = {
                        "ap": [], "mx": [], "mn": [], "dwh": 0.0, "smp": 0
                    }
                hours[h_ts]["ap"].append(ap)
                hours[h_ts]["mx"].append(mx)
                hours[h_ts]["mn"].append(mn)
                hours[h_ts]["dwh"] += dwh
                hours[h_ts]["smp"] += smp

            out_ts, out_avg_p, out_peak_p, out_min_p = [], [], [], []
            out_delta_wh, out_kwh_total, out_price, out_cost, out_samples = [], [], [], [], []

            for h_ts in sorted(hours.keys()):
                h_data = hours[h_ts]
                cnt = len(h_data["ap"])
                avg_p = sum(h_data["ap"]) / cnt if cnt else 0.0
                peak_p = max(h_data["mx"]) if cnt else 0.0
                min_p = min(h_data["mn"]) if cnt else 0.0
                total_wh = h_data["dwh"]
                kwh = total_wh / 1000.0

                day_str = h_ts.strftime("%Y-%m-%d")
                price = pricing_manager.get_price(day_str)
                cost = kwh * price

                out_ts.append(h_ts)
                out_avg_p.append(avg_p)
                out_peak_p.append(peak_p)
                out_min_p.append(min_p)
                out_delta_wh.append(total_wh)
                out_kwh_total.append(kwh)
                out_price.append(price)
                out_cost.append(cost)
                out_samples.append(h_data["smp"])

            table_1h = pa.Table.from_arrays([
                pa.array(out_ts, type=pa.timestamp("us")),
                pa.array(out_avg_p, type=pa.float32()),
                pa.array(out_peak_p, type=pa.float32()),
                pa.array(out_min_p, type=pa.float32()),
                pa.array(out_delta_wh, type=pa.float32()),
                pa.array(out_kwh_total, type=pa.float32()),
                pa.array(out_price, type=pa.float32()),
                pa.array(out_cost, type=pa.float32()),
                pa.array(out_samples, type=pa.int32())
            ], schema=ROLLUP_1H_SCHEMA)

            year_str = month_str[:4]
            year_dir = self.rollup_1h_dir / year_str
            year_dir.mkdir(parents=True, exist_ok=True)
            file_1h = year_dir / f"{handle}.parquet"

            if file_1h.exists():
                try:
                    existing = pq.read_table(file_1h)
                    table_1h = pa.concat_tables([existing, table_1h])
                except Exception as e:
                    logger.warning(f"Error appending to 1h rollup {file_1h}: {e}")

            pq.write_table(table_1h, file_1h, compression="ZSTD")
            logger.info(f"Generated 1h permanent rollup for {handle} on {month_str} ({len(out_ts)} hours)")
            return True
        except Exception as e:
            logger.error(f"Failed to create 1h rollup for {handle} on {month_str}: {e}")
            return False

    def cleanup_old_data(self, handle: str):
        """Bereinigt alte 1s-Rohdaten und 1m-Rollups gemäß Aufbewahrungsregeln."""
        today = date.today()

        # 1. 1s-Rohdaten bereinigen
        cutoff_raw = today - timedelta(days=RAW_RETENTION_DAYS)
        for day_dir in self.raw_dir.glob("????-??-??"):
            try:
                dir_date = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
                if dir_date < cutoff_raw:
                    # Vor dem Löschen sicherstellen, dass 1m-Rollup existiert
                    self.run_1m_rollup(handle, day_dir.name)
                    shutil.rmtree(day_dir, ignore_errors=True)
                    logger.info(f"Cleaned up expired raw directory {day_dir}")
            except Exception as e:
                logger.error(f"Error processing raw cleanup for {day_dir}: {e}")

        # 2. 1m-Rollup bereinigen (nach 30 Tagen)
        cutoff_1m = today - timedelta(days=ROLLUP_1M_RETENTION_DAYS)
        cutoff_month = cutoff_1m.strftime("%Y-%m")
        for month_dir in self.rollup_1m_dir.glob("????-??"):
            try:
                if month_dir.name < cutoff_month:
                    # Vor dem Löschen sicherstellen, dass 1h-Rollup existiert
                    self.run_1h_rollup(handle, month_dir.name)
                    shutil.rmtree(month_dir, ignore_errors=True)
                    logger.info(f"Archived & cleaned up 1m rollup directory {month_dir}")
            except Exception as e:
                logger.error(f"Error processing 1m cleanup for {month_dir}: {e}")

    def query_history(self, handle: str, range_type: str = "24h", live_buffer: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Liest Verbrauchsdaten für das angeforderte Zeitfenster aus:
        'live' -> letzte 60 Minuten aus dem RAM-Puffer
        '24h'  -> 1s-Rohdaten von heute/gestern + Live-Puffer
        '7d'   -> 1m-Rollups der letzten 7 Tage
        '30d'  -> 1m-Rollups der letzten 30 Tage
        '1y'   -> 1h-Rollups der letzten 365 Tage
        'all'  -> 1h-Rollups für alle Ewigkeit
        """
        now = datetime.now()
        points: List[Dict[str, Any]] = []
        total_kwh = 0.0
        peak_watts = 0.0
        total_cost_eur = 0.0

        if range_type == "live" and live_buffer:
            for r in live_buffer:
                ts = r["timestamp"]
                iso_ts = datetime.fromtimestamp(ts).isoformat() if isinstance(ts, (int, float)) else ts.isoformat()
                p = float(r.get("apower", 0.0))
                peak_watts = max(peak_watts, p)
                points.append({
                    "ts": iso_ts,
                    "watts": round(p, 1),
                    "voltage": round(float(r.get("voltage", 0.0)), 1),
                    "current": round(float(r.get("current", 0.0)), 2)
                })
            # Sortieren
            points.sort(key=lambda x: x["ts"])
            return {
                "handle": handle,
                "range": range_type,
                "points": points,
                "total_kwh": 0.0,
                "peak_watts": round(peak_watts, 1),
                "total_cost_eur": 0.0
            }

        if range_type in ["24h", "today"]:
            # 1s Rohdaten von heute (und ggf. gestern) einlesen
            start_date = now.date() - timedelta(days=1)
            raw_files = [
                self.raw_dir / start_date.isoformat() / f"{handle}.parquet",
                self.raw_dir / now.date().isoformat() / f"{handle}.parquet"
            ]
            for f in raw_files:
                if f.exists():
                    try:
                        tbl = pq.read_table(f)
                        ts_col = tbl["timestamp"].to_pylist()
                        p_col = tbl["apower"].to_pylist()
                        v_col = tbl["voltage"].to_pylist()
                        c_col = tbl["current"].to_pylist()
                        ae_col = tbl["aenergy_total"].to_pylist()

                        # Schrittweite für Chart-Performance reduzieren (z.B. alle 10s ein Punkt)
                        step = max(1, len(ts_col) // 1000)
                        for i in range(0, len(ts_col), step):
                            dt = ts_col[i]
                            if dt >= (now - timedelta(hours=24)):
                                pw = float(p_col[i])
                                peak_watts = max(peak_watts, pw)
                                points.append({
                                    "ts": dt.isoformat(),
                                    "watts": round(pw, 1),
                                    "voltage": round(float(v_col[i]), 1),
                                    "current": round(float(c_col[i]), 2)
                                })
                        if len(ae_col) > 1 and max(ae_col) >= min(ae_col):
                            total_kwh += (max(ae_col) - min(ae_col)) / 1000.0
                    except Exception as e:
                        logger.error(f"Error reading 24h raw file {f}: {e}")

            # Live-Buffer mergen
            if live_buffer:
                for r in live_buffer[::10]:
                    ts = r["timestamp"]
                    dt = datetime.fromtimestamp(ts) if isinstance(ts, (int, float)) else ts
                    if dt >= (now - timedelta(hours=24)):
                        p = float(r.get("apower", 0.0))
                        peak_watts = max(peak_watts, p)
                        points.append({
                            "ts": dt.isoformat(),
                            "watts": round(p, 1),
                            "voltage": round(float(r.get("voltage", 0.0)), 1),
                            "current": round(float(r.get("current", 0.0)), 2)
                        })

            current_price = pricing_manager.get_price(now.date().isoformat())
            total_cost_eur = total_kwh * current_price

        elif range_type in ["7d", "30d"]:
            days = 7 if range_type == "7d" else 30
            cutoff = now - timedelta(days=days)
            # 1-Minuten-Rollups durchsuchen
            for m_dir in sorted(self.rollup_1m_dir.glob("????-??")):
                f = m_dir / f"{handle}.parquet"
                if f.exists():
                    try:
                        tbl = pq.read_table(f)
                        ts_col = tbl["timestamp"].to_pylist()
                        p_col = tbl["avg_power"].to_pylist()
                        peak_col = tbl["max_power"].to_pylist()
                        dwh_col = tbl["delta_wh"].to_pylist()

                        step = 5 if range_type == "7d" else 15
                        for i in range(0, len(ts_col), step):
                            dt = ts_col[i]
                            if dt >= cutoff:
                                pw = float(p_col[i])
                                peak_watts = max(peak_watts, float(peak_col[i]))
                                points.append({
                                    "ts": dt.isoformat(),
                                    "watts": round(pw, 1),
                                    "peak_watts": round(float(peak_col[i]), 1)
                                })
                        for dt, dwh in zip(ts_col, dwh_col):
                            if dt >= cutoff:
                                total_kwh += float(dwh) / 1000.0
                                p_day = pricing_manager.get_price(dt.strftime("%Y-%m-%d"))
                                total_cost_eur += (float(dwh) / 1000.0) * p_day
                    except Exception as e:
                        logger.error(f"Error reading 1m rollup {f}: {e}")

        else:  # 1y oder all (Stunden-Rollup)
            cutoff = (now - timedelta(days=365)) if range_type == "1y" else datetime.min
            for y_dir in sorted(self.rollup_1h_dir.glob("????")):
                f = y_dir / f"{handle}.parquet"
                if f.exists():
                    try:
                        tbl = pq.read_table(f)
                        ts_col = tbl["timestamp"].to_pylist()
                        p_col = tbl["avg_power"].to_pylist()
                        pk_col = tbl["peak_power"].to_pylist()
                        kwh_col = tbl["kwh_total"].to_pylist()
                        cost_col = tbl["cost_eur"].to_pylist()

                        for dt, p, pk, kwh, cst in zip(ts_col, p_col, pk_col, kwh_col, cost_col):
                            if dt >= cutoff:
                                peak_watts = max(peak_watts, float(pk))
                                total_kwh += float(kwh)
                                total_cost_eur += float(cst)
                                points.append({
                                    "ts": dt.isoformat(),
                                    "watts": round(float(p), 1),
                                    "peak_watts": round(float(pk), 1),
                                    "kwh": round(float(kwh), 3),
                                    "cost_eur": round(float(cst), 3)
                                })
                    except Exception as e:
                        logger.error(f"Error reading 1h rollup {f}: {e}")

        points.sort(key=lambda x: x["ts"])
        return {
            "handle": handle,
            "range": range_type,
            "points": points,
            "total_kwh": round(total_kwh, 3),
            "peak_watts": round(peak_watts, 1),
            "total_cost_eur": round(total_cost_eur, 2)
        }

storage = ParquetStorage()
