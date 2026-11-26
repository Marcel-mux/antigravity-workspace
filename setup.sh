#!/usr/bin/env bash
set -euo pipefail

echo "=== Antigravity Discord Gateway Setup ==="

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 not found. Please install Python 3.10+." >&2
  exit 1
fi

if ! command -v agy >/dev/null 2>&1; then
  echo "Warning: 'agy' (Antigravity CLI) not found in PATH."
  echo "Make sure Google Antigravity CLI is installed and accessible before running the gateway."
fi

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment (.venv)..."
  python3 -m venv .venv
fi

echo "Installing Python dependencies..."
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install -r requirements.txt

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  echo "Created .env from .env.example."
fi

echo ""
echo "Setup complete! Next steps:"
echo "  1. Configure bot token & user IDs: ./.venv/bin/python gateway.py setup"
echo "  2. Start the gateway:              ./.venv/bin/python gateway.py start"
echo "  3. Optional systemd unit:          ./.venv/bin/python gateway.py service"
