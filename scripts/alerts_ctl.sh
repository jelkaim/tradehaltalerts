#!/usr/bin/env bash
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.tradehaltalerts.plist"
LABEL="com.tradehaltalerts"
DOMAIN="gui/$(id -u)/${LABEL}"

usage() {
  echo "Usage: $0 {start|stop|restart|status|enable|disable}"
}

ensure_plist() {
  if [ ! -f "$PLIST" ]; then
    echo "LaunchAgent plist not found at $PLIST"
    echo "Run: mkdir -p ~/Library/LaunchAgents && cp /Users/jelk/trade-halt-alerts/macos/com.tradehaltalerts.plist ~/Library/LaunchAgents/"
    exit 1
  fi
}

cmd=${1:-}
if [ -z "$cmd" ]; then
  usage
  exit 1
fi

case "$cmd" in
  start)
    ensure_plist
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl kickstart -k "$DOMAIN"
    echo "Alerts started"
    ;;
  stop)
    ensure_plist
    launchctl bootout "gui/$(id -u)" "$PLIST"
    echo "Alerts stopped"
    ;;
  restart)
    ensure_plist
    launchctl bootout "gui/$(id -u)" "$PLIST" || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl kickstart -k "$DOMAIN"
    echo "Alerts restarted"
    ;;
  status)
    ensure_plist
    launchctl print "$DOMAIN" | head -n 25
    ;;
  enable)
    ensure_plist
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "LaunchAgent enabled"
    ;;
  disable)
    ensure_plist
    launchctl bootout "gui/$(id -u)" "$PLIST"
    echo "LaunchAgent disabled"
    ;;
  *)
    usage
    exit 1
    ;;
esac
