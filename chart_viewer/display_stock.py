"""Thin CLI wrapper for DISPLAY_STOCK.

Usage:
    python display_stock.py DELL                           # Default preset (SMA 50/200 + BB 20)
    python display_stock.py DELL --preset trend_template   # Minervini 6-SMA layout
    python display_stock.py DELL --preset clean            # Candles only
    python display_stock.py DELL --preset momentum         # EMA 8/21

The heavy lifting (data fetching, overlay building, viewer communication)
is handled server-side by the DISPLAY_STOCK action in the Chart Server.
"""

import sys
import json
import urllib.request

CHART_SERVER_URL = "http://127.0.0.1:8766"

symbol = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
symbol = symbol.upper()

# Parse optional --preset argument
preset = "default"
for i, arg in enumerate(sys.argv):
    if arg == "--preset" and i + 1 < len(sys.argv):
        preset = sys.argv[i + 1]

print(f"[DISPLAY_STOCK] {symbol} with preset '{preset}'")
print(f"  -> Sending to Chart Server ({CHART_SERVER_URL})...")

cmd = {
    "action": "DISPLAY_STOCK",
    "symbol": symbol,
    "preset": preset,
    "limit": 1500,
}

try:
    req = urllib.request.Request(
        f"{CHART_SERVER_URL}/api/command",
        data=json.dumps(cmd).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode())

    if result.get("status") == "ok":
        print(f"  -> OK: {result.get('bars', '?')} bars, {result.get('overlays', '?')} overlays")
        print(f"  -> Window: {result.get('window_id', '?')}")
    else:
        print(f"  -> Error: {result.get('error', 'unknown')}")
        sys.exit(1)

except Exception as e:
    print(f"  -> FAILED: {e}")
    sys.exit(1)

print(f"\n--> {symbol} Chart wurde erfolgreich an deinen Windows Desktop Client gesendet!")
