#!/bin/bash
set -e

echo "=== Codex Traffic Light build ==="

if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate

echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "Cleaning old build artifacts..."
rm -rf build dist *.spec

echo "Packaging app..."
pyinstaller \
  --name "CodexTrafficLight" \
  --windowed \
  --noconfirm \
  --clean \
  traffic_light.py

echo "Build complete."
echo "App location: dist/CodexTrafficLight.app"
