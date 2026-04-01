#!/usr/bin/env bash
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.tradehaltalerts.plist"
LABEL="com.tradehaltalerts"
DOMAIN="gui/$(id -u)/${LABEL}"
STATE_PATH="$HOME/.tradehaltalerts_state.json"
DETAILS_PORT="${HALT_ALERTS_DETAILS_PORT:-8787}"

usage() {
  echo "Usage: $0 {start|stop|restart|status|debug-status|toggle|pause|resume|open|enable|disable}"
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
    agent_state="stopped"
    if launchctl print "$DOMAIN" >/dev/null 2>&1; then
      agent_state="running"
    fi
    paused="unknown"
    last_poll="n/a"
    pending="0"
    recent="0"
    if [ -f "$STATE_PATH" ]; then
      paused=$(python3 - <<'PY'
import json
from pathlib import Path
path = Path.home() / '.tradehaltalerts_state.json'
data = json.loads(path.read_text())
print("paused" if data.get("paused") else "active")
PY
)
      last_poll=$(python3 - <<'PY'
import json
from datetime import datetime
from pathlib import Path
path = Path.home() / '.tradehaltalerts_state.json'
data = json.loads(path.read_text())
last = data.get("last_poll", 0)
print(datetime.fromtimestamp(last).isoformat() if last else "n/a")
PY
)
      pending=$(python3 - <<'PY'
import json
from pathlib import Path
path = Path.home() / '.tradehaltalerts_state.json'
data = json.loads(path.read_text())
print(len(data.get("pending_resumes", [])))
PY
)
      recent=$(python3 - <<'PY'
import json
from pathlib import Path
path = Path.home() / '.tradehaltalerts_state.json'
data = json.loads(path.read_text())
print(len(data.get("recent_alerts", [])))
PY
)
    fi
    echo "Agent: $agent_state"
    echo "Alerts: $paused"
    echo "Last poll: $last_poll"
    echo "Pending resumes: $pending"
    echo "Recent alerts: $recent"
    echo "Alert Center: http://127.0.0.1:${DETAILS_PORT}/"
    ;;
  debug-status)
    ensure_plist
    launchctl print "$DOMAIN" | head -n 25
    ;;
  toggle)
    python3 - <<'PY'
import json
from pathlib import Path
path = Path.home() / '.tradehaltalerts_state.json'
data = {"seen_ids": [], "pending_resumes": [], "halt_counts": {}, "last_poll": 0, "recent_alerts": [], "paused": False}
if path.exists():
    try:
        data.update(json.loads(path.read_text()))
    except Exception:
        pass
data["paused"] = not bool(data.get("paused"))
path.write_text(json.dumps(data, indent=2, sort_keys=True))
print("Alerts paused" if data["paused"] else "Alerts resumed")
PY
    ;;
  pause)
    python3 - <<'PY'
import json
from pathlib import Path
path = Path.home() / '.tradehaltalerts_state.json'
data = {"seen_ids": [], "pending_resumes": [], "halt_counts": {}, "last_poll": 0, "recent_alerts": [], "paused": True}
if path.exists():
    try:
        data.update(json.loads(path.read_text()))
    except Exception:
        pass
data["paused"] = True
path.write_text(json.dumps(data, indent=2, sort_keys=True))
print("Alerts paused")
PY
    ;;
  resume)
    python3 - <<'PY'
import json
from pathlib import Path
path = Path.home() / '.tradehaltalerts_state.json'
data = {"seen_ids": [], "pending_resumes": [], "halt_counts": {}, "last_poll": 0, "recent_alerts": [], "paused": False}
if path.exists():
    try:
        data.update(json.loads(path.read_text()))
    except Exception:
        pass
data["paused"] = False
path.write_text(json.dumps(data, indent=2, sort_keys=True))
print("Alerts resumed")
PY
    ;;
  open)
    open "http://127.0.0.1:${DETAILS_PORT}/"
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
