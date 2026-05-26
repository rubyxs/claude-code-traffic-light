#!/bin/bash
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.ruby.codex-traffic-light.plist"
APP_DIR="$HOME/.codex/traffic_light"
LABEL="com.ruby.codex-traffic-light"

echo "== Codex Traffic Light uninstall =="

launchctl bootout "gui/$(id -u)" "$PLIST_DEST" >/dev/null 2>&1 || true
rm -f "$PLIST_DEST"
rm -f "$APP_DIR/launch_codex_traffic_light.sh"
rm -f "$APP_DIR/traffic_light.py"

echo "Removed launch agent: $LABEL"
echo "Virtualenv and project files were left in place."
