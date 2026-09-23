#!/usr/bin/env bash
# ==============================================================================
# Geordnetes Herunterfahren / Neustart des Servers über den PowerMeter-Service.
#   system-control.sh reboot                (OS-Neustart, Steckdose bleibt an)
#   system-control.sh poweroff              (OS aus, Steckdose bleibt an)
#   system-control.sh poweroff 10           (OS aus, nach 10 Min. automatisch an)
# ==============================================================================
set -euo pipefail
ACTION="${1:-reboot}"
MINUTES="${2:-0}"
API="${POWERMETER_API_URL:-http://127.0.0.1:8800}"
HANDLE="${TARGET_DEVICE_HANDLE:-server-plug}"
case "$ACTION" in
  reboot|poweroff) ;;
  *) echo "Usage: $0 reboot|poweroff [minuten_bis_power_on]"; exit 2 ;;
esac
POWER_ON_SECONDS=$(( MINUTES * 60 ))
echo "→ $ACTION via $API (Wiedereinschalten nach ${POWER_ON_SECONDS}s)"
curl -fsS -X POST "$API/api/devices/$HANDLE/system" \
  -H 'Content-Type: application/json' \
  -d "{\"action\":\"$ACTION\",\"power_on_after_seconds\":$POWER_ON_SECONDS}"
echo