#!/usr/bin/env bash
# =============================================================================
# dsh_url.sh - fertige Web-UI-URL inkl. Token fuer eine der beiden Installationen
#
# Portschema (App intern, Proxy extern):
#     0.1.5:  127.0.0.1:3090  ->  0.0.0.0:3091
#     0.1.7:  127.0.0.1:3092  ->  0.0.0.0:3093
#
# Aufruf:  dsh_url.sh [015|017] [host]
#   Default: 015, Host aus DSH_URL_HOST oder 10.20.0.23
# =============================================================================
set -Eeuo pipefail

WHICH="015"
if [ $# -gt 0 ]; then
  case "$1" in
    015|017) WHICH="$1"; shift ;;
  esac
fi

HOST=""
if [ $# -gt 0 ] && [ -n "$1" ]; then HOST="$1"; fi
if [ -z "$HOST" ]; then HOST="$(printenv DSH_URL_HOST 2>/dev/null || true)"; fi
if [ -z "$HOST" ]; then HOST="10.20.0.23"; fi

case "$WHICH" in
  015) UNIT="dsh-native.service";     EXTERN="3091"; INTERN="3090"; LABEL="0.1.5 (Produktion)" ;;
  017) UNIT="dsh-native-017.service"; EXTERN="3093"; INTERN="3092"; LABEL="0.1.7-rc.2 (Testkanal)" ;;
esac

TOKEN="$(journalctl --user -u "$UNIT" -e --no-pager 2>/dev/null | grep -o 'token=[A-Za-z0-9_-]*' | tail -1 | cut -d= -f2)"
if [ -z "$TOKEN" ]; then
  echo "FEHLER: kein Token im Journal von $UNIT gefunden." >&2
  echo "        Laeuft der Dienst?  systemctl --user status $UNIT" >&2
  exit 1
fi

CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:$EXTERN/" 2>/dev/null || echo 000)"
echo "http://$HOST:$EXTERN/?token=$TOKEN"
echo "   ($LABEL | intern 127.0.0.1:$INTERN, Proxy extern :$EXTERN, Antwort: $CODE)"
