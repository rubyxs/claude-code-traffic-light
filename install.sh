#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/codex.traffic-light.plist.template"
LAUNCHER_SRC="$SCRIPT_DIR/launch_codex_traffic_light.sh"
APP_SRC="$SCRIPT_DIR/traffic_light.py"

APP_STATE_DIR="$HOME/.codex/traffic_light"
VENV_DIR="$APP_STATE_DIR/venv"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_DEST="$LAUNCH_AGENTS_DIR/com.ruby.codex-traffic-light.plist"
LAUNCHER_DEST="$APP_STATE_DIR/launch_codex_traffic_light.sh"
APP_DEST="$APP_STATE_DIR/traffic_light.py"
STDOUT_LOG="$APP_STATE_DIR/launchd.out.log"
STDERR_LOG="$APP_STATE_DIR/launchd.err.log"

echo "== Codex Traffic Light installer =="

mkdir -p "$APP_STATE_DIR"
mkdir -p "$LAUNCH_AGENTS_DIR"
: > "$STDOUT_LOG"
: > "$STDERR_LOG"

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment..."
  python3 -m venv "$VENV_DIR"
fi

echo "Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo "Copying runtime files..."
cp "$APP_SRC" "$APP_DEST"

echo "Preparing launcher..."
cp "$LAUNCHER_SRC" "$LAUNCHER_DEST"
chmod +x "$LAUNCHER_DEST"

echo "Writing launchd agent..."
sed \
  -e "s|__LAUNCHER__|$LAUNCHER_DEST|g" \
  -e "s|__WORKDIR__|$APP_STATE_DIR|g" \
  -e "s|__STDOUT__|$STDOUT_LOG|g" \
  -e "s|__STDERR__|$STDERR_LOG|g" \
  "$PLIST_SRC" > "$PLIST_DEST"

chmod 644 "$PLIST_DEST"

echo "Reloading launch agent..."
launchctl bootout "gui/$(id -u)" "$PLIST_DEST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
launchctl kickstart -k "gui/$(id -u)/com.ruby.codex-traffic-light"

echo
echo "Installed successfully."
echo "It now starts automatically after login/restart."
echo "Logs:"
echo "  $STDOUT_LOG"
echo "  $STDERR_LOG"
