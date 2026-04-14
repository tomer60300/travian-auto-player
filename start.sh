#!/usr/bin/env bash
# Travian Auto Player — one-click setup and start
# Usage: ./start.sh

set -e
cd "$(dirname "$0")"

echo ""
echo "  ============================================"
echo "   Travian Auto Player — Setup & Start"
echo "  ============================================"
echo ""

# 1. Install Python dependencies
echo "[1/3] Installing Python dependencies..."
pip install -e ".[web]" --quiet 2>/dev/null || pip install -e ".[web]" --quiet --user
echo "       Done."

# 2. Build frontend
echo "[2/3] Building frontend..."
cd frontend
[ -d node_modules ] || npm install --silent
npm run build --silent
cd ..
echo "       Done."

# 3. Start server
echo "[3/3] Starting server on http://localhost:8001"
echo ""
echo "  ============================================"
echo "   Open http://localhost:8001 in your browser"
echo "  ============================================"
echo ""
python -m uvicorn travian_api.web.app:app --host 0.0.0.0 --port 8001
