#!/usr/bin/env sh
# Travian Auto Player - one-click setup and start (Linux/Mac)
# Usage: ./start.sh from the project root
set -e

cd "$(dirname "$0")"

echo ""
echo " ============================================"
echo "  Travian Auto Player - Setup & Start"
echo " ============================================"
echo ""

# Preflight: require Python and Node on PATH
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: 'python3' is not on PATH. Install Python 3.11+ from https://www.python.org/downloads/"
    exit 1
fi
if ! command -v npm >/dev/null 2>&1 || ! command -v node >/dev/null 2>&1; then
    echo "ERROR: Node.js is not on PATH. Install Node.js 20.19+ or 22.12+ from https://nodejs.org/"
    exit 1
fi

# Vite 8 (the locked frontend toolchain) requires Node ^20.19.0 or >=22.12.0.
# The check must match that range exactly: a major-only check lets Node
# 20.0-20.18 and Node 21 pass preflight and then fail halfway through the
# frontend build.
NODE_VERSION=$(node -v | sed 's/^v//')
NODE_MAJOR=$(echo "$NODE_VERSION" | cut -d. -f1)
NODE_MINOR=$(echo "$NODE_VERSION" | cut -d. -f2)
NODE_OK=1
[ "$NODE_MAJOR" -lt 20 ] && NODE_OK=0
[ "$NODE_MAJOR" -eq 20 ] && [ "$NODE_MINOR" -lt 19 ] && NODE_OK=0
[ "$NODE_MAJOR" -eq 21 ] && NODE_OK=0
[ "$NODE_MAJOR" -eq 22 ] && [ "$NODE_MINOR" -lt 12 ] && NODE_OK=0
if [ "$NODE_OK" -eq 0 ]; then
    echo "ERROR: Node $NODE_VERSION cannot build the frontend. It needs Node 20.19+ or 22.12+."
    exit 1
fi

# 1. Create/activate venv and install Python dependencies
echo "[1/3] Installing Python dependencies..."
if [ ! -x ".venv/bin/python" ]; then
    echo "       Creating virtual environment at .venv ..."
    python3 -m venv .venv
fi
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[web]"
echo "       Done."

# 2. Build frontend
echo "[2/3] Building frontend..."
cd frontend
# Always sync npm packages: after a pull, an existing node_modules can be
# stale against the new package-lock.json, and npm install is fast when
# everything is already current.
echo "       Installing npm packages..."
npm install
npm run build
cd ..
echo "       Done."

# 3. Start server
echo "[3/3] Starting server on http://localhost:8001"
echo ""
echo " ============================================"
echo "  Open http://localhost:8001 in your browser"
echo " ============================================"
echo ""
python -m uvicorn travian_api.web.app:app --host 0.0.0.0 --port 8001
