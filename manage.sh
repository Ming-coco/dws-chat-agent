#!/bin/zsh
set -euo pipefail

APP_DIR="${0:A:h}"
LABEL="${DWS_CHAT_AGENT_LAUNCHD_LABEL:-com.inoryirony.dws-chat-agent}"
DOMAIN="gui/$(id -u)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
INSTALLED_PLIST="$LAUNCH_AGENTS_DIR/$LABEL.plist"
PYTHON_BIN="${DWS_CHAT_AGENT_PYTHON:-$(command -v python3)}"

render_plist() {
  mkdir -p "$APP_DIR/state" "$LAUNCH_AGENTS_DIR"
  "$PYTHON_BIN" - "$INSTALLED_PLIST" "$LABEL" "$APP_DIR" "$PYTHON_BIN" "$PATH" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path, label, app_dir, python_bin, path_value = sys.argv[1:]
app_path = Path(app_dir)
payload = {
    "Label": label,
    "ProgramArguments": [
        python_bin,
        str(app_path / "dm_agent.py"),
        "--env-file",
        str(app_path / ".env"),
        "run",
    ],
    "WorkingDirectory": app_dir,
    "EnvironmentVariables": {
        "HOME": str(Path.home()),
        "PATH": path_value,
        "PYTHONUNBUFFERED": "1",
        "TZ": "Asia/Shanghai",
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "ProcessType": "Background",
    "StandardOutPath": str(app_path / "state" / "launchd.stdout.log"),
    "StandardErrorPath": str(app_path / "state" / "launchd.stderr.log"),
}
with Path(plist_path).open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=False)
PY
}

start_service() {
  render_plist
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL"
  fi
  for attempt in {1..60}; do
    if launchctl bootstrap "$DOMAIN" "$INSTALLED_PLIST" 2>/dev/null; then
      return
    fi
    sleep 0.25
  done
  launchctl bootstrap "$DOMAIN" "$INSTALLED_PLIST"
}

command_name="${1:-status}"

case "$command_name" in
  start|restart)
    start_service
    ;;
  stop)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl bootout "$DOMAIN/$LABEL"
    fi
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL"
    ;;
  logs)
    tail -n 100 \
      "$APP_DIR/state/agent.log" \
      "$APP_DIR/state/launchd.stderr.log" \
      "$APP_DIR/state/launchd.stdout.log" 2>/dev/null || true
    ;;
  doctor)
    exec "$PYTHON_BIN" "$APP_DIR/dm_agent.py" --env-file "$APP_DIR/.env" doctor
    ;;
  uninstall)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl bootout "$DOMAIN/$LABEL"
    fi
    rm -f "$INSTALLED_PLIST"
    ;;
  *)
    print -u2 "usage: $0 {start|stop|restart|status|logs|doctor|uninstall}"
    exit 2
    ;;
esac
