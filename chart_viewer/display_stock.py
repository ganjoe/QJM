"""Utility script to fetch real Parquet chart data and display stock on Desktop Viewer."""

import sys
import os
import json
import urllib.request

symbol = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
symbol = symbol.upper()

print(f"[1/3] Fetching historical chart data with indicators for {symbol} from PCA-Service (port 8794)...")
url = f"http://127.0.0.1:8794/api/chartdata?symbol={symbol}&timeframe=1D&limit=300&features=true"

try:
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read().decode())
except Exception as e:
    print(f"ERROR: Could not reach PCA service: {e}")
    sys.exit(1)

if data.get("status") != "ok" or not data.get("data"):
    print(f"WARN: No data available for {symbol}")
    sys.exit(1)

cols = data.get("columns", [])
rows = data.get("data", [])
idx = {name: i for i, name in enumerate(cols)}

bars = []
for r in rows:
    t = int(r[idx["timestamp"]])
    bars.append({
        "t_open": t,
        "t_close": t + 86400,
        "open": float(r[idx["open"]]),
        "high": float(r[idx["high"]]),
        "low": float(r[idx["low"]]),
        "close": float(r[idx["close"]]),
        "volume": float(r[idx["volume"]] or 0),
    })

overlays = []
if "ma_sma_50" in idx:
    sma50 = [{"t": int(r[idx["timestamp"]]), "value": float(r[idx["ma_sma_50"]])} 
             for r in rows if r[idx["ma_sma_50"]] is not None]
    overlays.append({
        "overlay_id": "sma_50",
        "type": "line",
        "style": {"color": "#2962FF", "width": 2},
        "values": sma50
    })

if "ma_sma_200" in idx:
    sma200 = [{"t": int(r[idx["timestamp"]]), "value": float(r[idx["ma_sma_200"]])} 
              for r in rows if r[idx["ma_sma_200"]] is not None]
    overlays.append({
        "overlay_id": "sma_200",
        "type": "line",
        "style": {"color": "#FF9800", "width": 2},
        "values": sma200
    })

if "bb_20_upper" in idx and "bb_20_lower" in idx:
    bb = [{"t": int(r[idx["timestamp"]]), "value": float(r[idx["bb_20_upper"]]), "value2": float(r[idx["bb_20_lower"]])}
          for r in rows if r[idx["bb_20_upper"]] is not None and r[idx["bb_20_lower"]] is not None]
    overlays.append({
        "overlay_id": "bollinger_bands",
        "type": "band",
        "style": {"color": "#26A69A", "alpha": 30},
        "values": bb
    })

last_close = bars[-1]["close"]
annotations = [
    {
        "id": f"{symbol.lower()}_support",
        "type": "hline",
        "anchors": [{"price": round(last_close * 0.95, 2)}],
        "style": {"color": "#00E676", "width": 2},
        "label": f"Support @ {round(last_close * 0.95, 2)}"
    }
]

cmd = {
    "action": "OPEN_WINDOW",
    "window_id": f"win_{symbol.lower()}_1d",
    "symbol": symbol,
    "timeframe": {"unit": "D", "multiplier": 1},
    "sync_group_id": "stocks",
    "position": {"x": 100, "y": 100},
    "size": {"width": 1100, "height": 750},
    "bars": bars,
    "overlays": overlays,
    "annotations": annotations
}

print(f"[2/3] Sending OPEN_WINDOW command to Chart Viewer (port 8766)...")
req_cmd = urllib.request.Request(
    "http://127.0.0.1:8766/api/command",
    data=json.dumps(cmd).encode(),
    headers={"Content-Type": "application/json"}
)
with urllib.request.urlopen(req_cmd) as resp:
    res = json.loads(resp.read().decode())
    print("      Server Response:", res)

# Update Topbar with metrics
topbar_cmd = {
    "action": "SET_TOPBAR",
    "window_id": f"win_{symbol.lower()}_1d",
    "block_id": "info_block",
    "content": f"{symbol} | Last: ${last_close:.2f} | 50 SMA: ${bars[-1]['close']:.2f} | Bars: {len(bars)}"
}
print(f"[3/3] Setting Topbar metrics...")
req_tb = urllib.request.Request(
    "http://127.0.0.1:8766/api/command",
    data=json.dumps(topbar_cmd).encode(),
    headers={"Content-Type": "application/json"}
)
with urllib.request.urlopen(req_tb) as resp:
    print("      Topbar set successfully!")

print(f"\n--> {symbol} Chart wurde erfolgreich an deinen Windows Desktop Client gesendet!")
