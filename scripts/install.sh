#!/usr/bin/env bash
# Knowledge Engine - POSIX install
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d engine/.venv ]; then
  echo "Creating virtualenv at engine/.venv..."
  python3 -m venv engine/.venv
fi

# shellcheck disable=SC1091
. engine/.venv/bin/activate
python -m pip install --upgrade pip --quiet
python -m pip install -e ./engine --quiet
python -m pip install fastapi uvicorn httpx pydantic watchdog pytest --quiet

echo
echo "Install complete."
echo "Next steps:"
echo "  1. source engine/.venv/bin/activate"
echo "  2. knowledge-engine bootstrap"
echo "  3. knowledge-engine reindex"
echo "  4. ./scripts/serve.sh"
