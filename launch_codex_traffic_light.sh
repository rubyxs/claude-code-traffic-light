#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "Missing virtualenv Python at $VENV_PYTHON" >&2
  exit 1
fi

cd "$SCRIPT_DIR"
exec "$VENV_PYTHON" "$SCRIPT_DIR/traffic_light.py"
