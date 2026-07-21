#!/bin/zsh
set -euo pipefail

APP_DIR="${0:A:h:h}"
LABEL="${DWS_CHAT_AGENT_LAUNCHD_LABEL:-com.inoryirony.dws-chat-agent}"
DOMAIN="gui/$(id -u)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
INSTALLED_PLIST="$LAUNCH_AGENTS_DIR/$LABEL.plist"
PYTHON_BIN="${DWS_CHAT_AGENT_PYTHON:-}"
if [[ -z "$PYTHON_BIN" && -f "$INSTALLED_PLIST" ]]; then
  INSTALLED_PYTHON="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$INSTALLED_PLIST" 2>/dev/null || true)"
  [[ -x "$INSTALLED_PYTHON" ]] && PYTHON_BIN="$INSTALLED_PYTHON"
fi
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

render_plist() {
  mkdir -p "$APP_DIR/state" "$LAUNCH_AGENTS_DIR"
  "$PYTHON_BIN" - "$INSTALLED_PLIST" "$LABEL" "$APP_DIR" "$PYTHON_BIN" "$PATH" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path, label, app_dir, python_bin, path_value = sys.argv[1:]
app_path = Path(app_dir)
template = app_path / "deploy" / "macos" / "dws-chat-agent.plist.template"
with template.open("rb") as handle:
    payload = plistlib.load(handle)
replacements = {
    "__LABEL__": label,
    "__PYTHON__": python_bin,
    "__APP_DIR__": app_dir,
    "__HOME__": str(Path.home()),
    "__PATH__": path_value,
}
def render(value):
    if isinstance(value, str):
        for source, target in replacements.items():
            value = value.replace(source, target)
        return value
    if isinstance(value, list):
        return [render(item) for item in value]
    if isinstance(value, dict):
        return {key: render(item) for key, item in value.items()}
    return value
payload = render(payload)
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
